// Closed price bars, kept so they are downloaded once.
//
// The trend rule and the worksheet read 5000 four-hour bars per market, and
// the weekly calls read 330 daily bars per coin. A closed bar never changes,
// so the bars are kept in memory and in the browser's IndexedDB; a later
// visit asks the exchange only for the bars since the last one stored, and
// nothing at all until a new bar has closed.

import { candleWeight, info } from "./hyperliquid";
import { DataError, onReset, runtime } from "./runtime";

export type BarInterval = "4h" | "1d";
export const BAR_MS: Record<BarInterval, number> = { "4h": 4 * 3600 * 1000, "1d": 24 * 3600 * 1000 };

/** Closed bars, oldest first, as plain number arrays. */
export interface Bars {
  t: Float64Array;
  open: Float64Array;
  high: Float64Array;
  low: Float64Array;
  close: Float64Array;
}

/**
 * What is written to storage: the bars, the open time of the newest bar the
 * exchange was asked about, and when it was asked.
 */
export interface StoredBars extends Bars {
  through: number;
  checkedAt: number;
}

/** A candle as Hyperliquid sends it: times as numbers, prices as text. */
export interface RawCandle {
  t: number;
  o: string;
  h: string;
  l: string;
  c: string;
  v: string;
}

interface Series {
  key: string;
  symbol: string;
  interval: BarInterval;
  count: number;
  bars: StoredBars | null;
  hydrating: Promise<void> | null;
  hydrated: boolean;
  loading: Promise<void> | null;
  tailOnly: boolean;
  error: DataError | null;
  failedAt: number;
}

export type HistoryStatus =
  | { state: "ready"; bars: Bars }
  | { state: "pending" }
  | { state: "failed"; error: DataError };

const RETRY_AFTER_MS = 60_000;         // leave a market that failed to load alone for a minute
const SETTLE_MS = 60_000;              // how long after its close a bar is given to become final
const STORAGE_VERSION = "v1";

let series = new Map<string, Series>();
let generation = 0;

onReset(() => {
  generation += 1;
  series = new Map();
});

function seriesFor(symbol: string, interval: BarInterval, count: number): Series {
  const key = `${STORAGE_VERSION}:${interval}:${count}:${symbol}`;
  let found = series.get(key);
  if (!found) {
    found = { key, symbol, interval, count, bars: null, hydrating: null, hydrated: false, loading: null,
              tailOnly: false, error: null, failedAt: 0 };
    series.set(key, found);
  }
  return found;
}

/** Open time of the bar that closed most recently. */
function lastClosedOpen(interval: BarInterval, now: number): number {
  const ms = BAR_MS[interval];
  return Math.floor(now / ms) * ms - ms;
}

function isFresh(s: Series, now: number): boolean {
  if (s.bars === null || s.bars.through < lastClosedOpen(s.interval, now)) return false;
  // A bar fetched in the first minute after it closed is fetched once more when that
  // minute is up, in case the exchange was still adding its last trades.
  const settledAt = s.bars.through + BAR_MS[s.interval] + SETTLE_MS;
  return !(s.bars.checkedAt < settledAt && now >= settledAt);
}

function usable(value: unknown): value is StoredBars {
  const bars = value as StoredBars | null;
  if (!bars || typeof bars.through !== "number" || typeof bars.checkedAt !== "number") return false;
  const columns = [bars.t, bars.open, bars.high, bars.low, bars.close];
  return columns.every((column) => column instanceof Float64Array && column.length === bars.t.length);
}

function hydrate(s: Series): Promise<void> {
  if (s.hydrated) return Promise.resolve();
  if (!s.hydrating) {
    const mine = generation;
    const storage = runtime.storage;
    const work = (async () => {
      if (!storage) return;
      try {
        const stored = await storage.get(s.key);
        if (mine === generation && usable(stored) && !s.bars) s.bars = stored;
      } catch { /* storage is a convenience: carry on without it */ }
    })().finally(() => {
      s.hydrated = true;
      if (s.hydrating === work) s.hydrating = null;
    });
    s.hydrating = work;
  }
  return s.hydrating;
}

function toBars(rows: RawCandle[], through: number, checkedAt: number): StoredBars {
  const n = rows.length;
  const out: StoredBars = { t: new Float64Array(n), open: new Float64Array(n), high: new Float64Array(n),
                            low: new Float64Array(n), close: new Float64Array(n), through, checkedAt };
  rows.forEach((row, i) => {
    out.t[i] = Number(row.t);
    out.open[i] = Number(row.o);
    out.high[i] = Number(row.h);
    out.low[i] = Number(row.l);
    out.close[i] = Number(row.c);
  });
  return out;
}

function join(older: Bars, upTo: number, newer: StoredBars): StoredBars {
  let keep = 0;
  while (keep < older.t.length && older.t[keep] < upTo) keep += 1;
  const merge = (a: Float64Array, b: Float64Array) => {
    const out = new Float64Array(keep + b.length);
    out.set(a.subarray(0, keep), 0);
    out.set(b, keep);
    return out;
  };
  return { t: merge(older.t, newer.t), open: merge(older.open, newer.open), high: merge(older.high, newer.high),
           low: merge(older.low, newer.low), close: merge(older.close, newer.close),
           through: newer.through, checkedAt: newer.checkedAt };
}

/** The bars a fresh request would return now: Hyperliquid includes the bar that contains the start time. */
function inWindow(bars: Bars, interval: BarInterval, count: number, now: number): Bars {
  const ms = BAR_MS[interval];
  const start = now - count * ms;
  let first = 0;
  while (first < bars.t.length && bars.t[first] + ms <= start) first += 1;
  let end = bars.t.length;
  while (end > first && bars.t[end - 1] + ms > now) end -= 1;       // never a bar that is still forming
  if (first === 0 && end === bars.t.length) return bars;
  return { t: bars.t.subarray(first, end), open: bars.open.subarray(first, end),
           high: bars.high.subarray(first, end), low: bars.low.subarray(first, end),
           close: bars.close.subarray(first, end) };
}

const rowsWeight = (rows: unknown) => candleWeight(Array.isArray(rows) ? rows.length : 0);

async function fetchBars(s: Series, background: boolean): Promise<void> {
  const mine = generation;
  const now = runtime.now();
  const ms = BAR_MS[s.interval];
  const start = now - s.count * ms;
  const stored = s.bars;
  const last = stored && stored.t.length ? stored.t[stored.t.length - 1] : null;
  // When the newest stored bar is still inside the window, ask only for what came after it.
  const from = last !== null && last + ms > start ? last : start;
  const rows = await info<RawCandle[]>(
    { type: "candleSnapshot", req: { coin: s.symbol, interval: s.interval, startTime: from, endTime: now } },
    { weight: candleWeight((now - from) / ms + 1), background, actualWeight: rowsWeight });
  if (mine !== generation) return;
  if (!Array.isArray(rows)) throw new DataError("Market data is unavailable right now (ValueError).");
  const closed = rows.filter((row) => Number(row.t) + ms <= now);
  let next = toBars(closed, lastClosedOpen(s.interval, now), now);
  if (from !== start && stored) next = join(stored, closed.length ? Number(closed[0].t) : Infinity, next);
  const kept = inWindow(next, s.interval, s.count, now);
  s.bars = { t: kept.t.slice(), open: kept.open.slice(), high: kept.high.slice(), low: kept.low.slice(),
             close: kept.close.slice(), through: next.through, checkedAt: now };
  s.error = null;
  const storage = runtime.storage;
  if (storage) void storage.put(s.key, s.bars).catch(() => { /* storage full or blocked: memory still has it */ });
}

/** True when the stored bars only need the newest few added, not a whole download. */
function needsOnlyTail(s: Series): boolean {
  const ms = BAR_MS[s.interval];
  const last = s.bars && s.bars.t.length ? s.bars.t[s.bars.t.length - 1] : null;
  return last !== null && last + ms > runtime.now() - s.count * ms;
}

/** Start loading if the stored bars are out of date. Returns at once; `s.loading` is the work. */
function refresh(s: Series, background: boolean): void {
  if (s.loading || isFresh(s, runtime.now())) return;
  s.tailOnly = needsOnlyTail(s);
  // A short catch-up is cheap, so it does not queue behind the long downloads.
  const work = fetchBars(s, background && !s.tailOnly).catch((error: unknown) => {
    s.error = error instanceof DataError ? error : new DataError("Market data is unavailable right now (Error).");
    s.failedAt = Date.now();
  }).finally(() => {
    if (s.loading === work) s.loading = null;
  });
  s.loading = work;
}

/** Closed bars for something the screen is waiting on. Rejects with a DataError if they cannot be had. */
export async function closedBars(symbol: string, interval: BarInterval, count: number): Promise<Bars> {
  const s = seriesFor(symbol, interval, count);
  await hydrate(s);
  if (!isFresh(s, runtime.now())) {
    refresh(s, false);
    await s.loading;
    if (!isFresh(s, runtime.now()) || !s.bars) {
      throw s.error ?? new DataError("Market data is unavailable right now (Error).");
    }
  }
  return inWindow(s.bars as StoredBars, interval, count, runtime.now());
}

// ---- long histories, loaded in the background ----------------------------------

export const HISTORY_BARS = 5000;

/** Make sure these markets' four-hour histories are loading or loaded. Returns at once. */
export function want(symbols: readonly string[]): void {
  for (const symbol of symbols) {
    const s = seriesFor(symbol, "4h", HISTORY_BARS);
    if (!s.hydrated) {
      void hydrate(s).then(() => { if (series.get(s.key) === s) want([symbol]); });
      continue;
    }
    if (s.error && Date.now() - s.failedAt < RETRY_AFTER_MS) continue;
    refresh(s, true);
  }
}

/**
 * Where a market's four-hour history stands right now. Never waits.
 *
 * For the few seconds after a bar closes, while the new bar is being fetched,
 * the bars already held are still served (they are all closed bars), so the
 * screen does not blank every four hours. Anything older counts as not loaded.
 */
export function historyStatus(symbol: string): HistoryStatus {
  const s = seriesFor(symbol, "4h", HISTORY_BARS);
  const now = runtime.now();
  if (s.hydrated && s.bars && s.bars.through >= lastClosedOpen("4h", now) - BAR_MS["4h"]) {
    return { state: "ready", bars: inWindow(s.bars, "4h", HISTORY_BARS, now) };
  }
  if (s.error && !s.loading) return { state: "failed", error: s.error };
  return { state: "pending" };
}

/**
 * Give stored histories a moment to arrive: read them from storage, top them
 * up with the newest bars, and wait (briefly) for that. Long first-time
 * downloads are neither started nor waited for here: `want` starts those,
 * once the screen has what it needs to draw.
 */
export async function settleQuick(symbols: readonly string[], patienceMs: number): Promise<void> {
  const mine = symbols.map((symbol) => seriesFor(symbol, "4h", HISTORY_BARS));
  await Promise.all(mine.map(hydrate));
  for (const s of mine) {
    if (needsOnlyTail(s) && !(s.error && Date.now() - s.failedAt < RETRY_AFTER_MS)) refresh(s, false);
  }
  const quick = mine.filter((s) => s.loading && s.tailOnly).map((s) => s.loading);
  if (!quick.length) return;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const patience = new Promise<void>((resolve) => { timer = setTimeout(resolve, patienceMs); });
  await Promise.race([Promise.all(quick), patience]);
  clearTimeout(timer);
}

/** Resolves when nothing is loading any more (used by tests and the parity check). */
export async function settled(): Promise<void> {
  for (;;) {
    const busy = [...series.values()].flatMap((s) => [s.hydrating, s.loading]).filter(Boolean);
    if (!busy.length) return;
    await Promise.all(busy);
  }
}
