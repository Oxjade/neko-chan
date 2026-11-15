// Requests to Hyperliquid's public API, kept inside the exchange's allowance.
//
// The exchange lets one IP address spend 1200 "weight" a minute. A browser
// tab shares that with everything else on the same connection, so requests
// queue here: the ones the screen is waiting for go first, and background
// loading of long price histories takes what is left, a few at a time, and
// waits and tries again when the exchange says slow down.

import { DataError, HL_URL, onReset, runtime } from "./runtime";

const WINDOW_MS = 60_000;
const BACKGROUND_SHARE = 0.9;        // background loading always leaves a tenth of the allowance for the live screen
const MAX_BACKGROUND = 3;            // long histories downloading at once
const RETRY_DELAYS_MS = [2_000, 5_000, 10_000, 20_000, 30_000];
const WAKE_MS = 250;

/** Weight of a candle request that returns about `candles` rows. */
export function candleWeight(candles: number): number {
  return 20 + Math.ceil(Math.max(0, candles) / 60);
}

export interface RequestOptions {
  /** What the request is expected to cost against the exchange's allowance. */
  weight: number;
  /** What it did cost, once the answer is in (a young market returns fewer candles than asked for). */
  actualWeight?: (data: unknown) => number;
  /** Background requests wait their turn and are retried; the screen is not waiting for them. */
  background?: boolean;
  timeoutMs?: number;
}

interface Failure {
  kind: string;
  retryable: boolean;
  limited: boolean;
}

interface Job {
  payload: unknown;
  weight: number;
  actualWeight?: (data: unknown) => number;
  background: boolean;
  timeoutMs: number;
  attempt: number;
  notBefore: number;
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
}

let waiting: Job[] = [];
let spent: Array<{ at: number; weight: number }> = [];
let activeBackground = 0;
let pausedUntil = 0;
let wake: ReturnType<typeof setTimeout> | null = null;
let generation = 0;

onReset(() => {
  generation += 1;
  for (const job of waiting) job.reject(new DataError("Market data is unavailable right now (Cancelled)."));
  waiting = [];
  spent = [];
  activeBackground = 0;
  pausedUntil = 0;
  if (wake) clearTimeout(wake);
  wake = null;
});

/** Ask Hyperliquid's info endpoint. Rejects with a DataError the screen can show. */
export function info<T>(payload: unknown, options: RequestOptions): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    waiting.push({
      payload, weight: options.weight, actualWeight: options.actualWeight,
      background: Boolean(options.background),
      timeoutMs: options.timeoutMs ?? (options.background ? 60_000 : 20_000),
      attempt: 0, notBefore: 0,
      resolve: resolve as (value: unknown) => void, reject,
    });
    pump();
  });
}

/** True while long downloads are queued or running; the live screen then asks a little less often. */
export function backgroundBusy(): boolean {
  return activeBackground > 0 || waiting.some((job) => job.background);
}

/** Weight spent in the last minute (for the network notes in the README and tests). */
export function weightSpent(): number {
  prune(Date.now());
  return spent.reduce((sum, entry) => sum + entry.weight, 0);
}

function prune(now: number): void {
  while (spent.length && now - spent[0].at >= WINDOW_MS) spent.shift();
}

function pump(): void {
  if (wake) {
    clearTimeout(wake);
    wake = null;
  }
  const now = Date.now();
  prune(now);
  let used = spent.reduce((sum, entry) => sum + entry.weight, 0);
  const limit = runtime.weightPerMinute;
  const ordered = [...waiting.filter((job) => !job.background), ...waiting.filter((job) => job.background)];
  for (const job of ordered) {
    if (job.notBefore > now) continue;
    if (job.background) {
      if (activeBackground >= MAX_BACKGROUND || now < pausedUntil) continue;
      if (used + job.weight > limit * BACKGROUND_SHARE) continue;
    } else if (used + job.weight > limit && used > 0) {
      continue;
    }
    waiting.splice(waiting.indexOf(job), 1);
    used += job.weight;
    const entry = { at: now, weight: job.weight };
    spent.push(entry);
    void run(job, entry);
  }
  if (waiting.length && !wake) wake = setTimeout(pump, WAKE_MS);
}

async function run(job: Job, entry: { weight: number }): Promise<void> {
  const mine = generation;
  if (job.background) activeBackground += 1;
  try {
    const data = await send(job.payload, job.timeoutMs);
    if (job.actualWeight) entry.weight = job.actualWeight(data);
    job.resolve(data);
  } catch (error) {
    const failure = error as Failure;
    const delay = RETRY_DELAYS_MS[job.attempt];
    if (failure.limited) {
      // The exchange is counting more than we are (another tab, another device): hold back.
      pausedUntil = Date.now() + (delay ?? 30_000);
      spent.push({ at: Date.now(), weight: Math.min(runtime.weightPerMinute, 1200) / 2 });
    }
    const retry = job.background && failure.retryable && delay !== undefined
      && runtime.weightPerMinute !== Infinity && mine === generation;
    if (retry) {
      job.attempt += 1;
      job.notBefore = Date.now() + delay;
      waiting.push(job);
    } else {
      job.reject(new DataError(`Market data is unavailable right now (${failure.kind}).`));
    }
  } finally {
    if (mine === generation) {
      if (job.background) activeBackground -= 1;
      pump();
    }
  }
}

async function send(payload: unknown, timeoutMs: number): Promise<unknown> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    let response: Response;
    try {
      response = await runtime.fetch(HL_URL, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload), signal: controller.signal,
      });
    } catch {
      throw fail(controller.signal.aborted ? "Timeout" : "ConnectionError", true);
    }
    if (!response.ok) {
      throw fail("HTTPError", response.status === 429 || response.status >= 500, response.status === 429);
    }
    try {
      return await response.json();
    } catch {
      throw fail(controller.signal.aborted ? "Timeout" : "JSONDecodeError", true);
    }
  } finally {
    clearTimeout(timer);
  }
}

function fail(kind: string, retryable: boolean, limited = false): Failure {
  return { kind, retryable, limited };
}
