// What the core needs from the outside world: a way to fetch, a clock and a
// place to keep price history between visits. All three can be swapped, so
// the same code runs in the browser and, with recorded data, under test.

import type { StoredBars } from "./history";

/** A data source could not be read. `message` is written for the person using the app. */
export class DataError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "DataError";
  }
}

/** Where closed price bars are kept between visits (IndexedDB in the browser). */
export interface BarStorage {
  get(key: string): Promise<StoredBars | null>;
  put(key: string, bars: StoredBars): Promise<void>;
}

export interface Runtime {
  fetch: typeof fetch;
  /** Milliseconds since 1970, UTC. */
  now: () => number;
  /** null keeps price history in memory only. */
  storage: BarStorage | null;
  /** Hyperliquid request weight this browser may spend per minute (the exchange allows 1200 per IP). */
  weightPerMinute: number;
  /** Where the forex board's rates come from (the one server function). */
  fxUrl: string;
}

export const HL_URL = "https://api.hyperliquid.xyz/info";
export const FNG_URL = "https://api.alternative.me/fng/";

export const runtime: Runtime = {
  fetch: (input, init) => globalThis.fetch(input, init),
  now: () => Date.now(),
  storage: null,
  weightPerMinute: 1100,
  fxUrl: "/api/fx",
};

const resets: Array<() => void> = [];

/** A module registers how to forget what it has cached. */
export function onReset(reset: () => void): void {
  resets.push(reset);
}

/** Swap the outside world (tests, or a page wiring up IndexedDB) and forget everything cached. */
export function configure(options: Partial<Runtime>): void {
  Object.assign(runtime, options);
  for (const reset of resets) reset();
}

/** The promise's value if it arrives within `ms`, otherwise the fallback (the work carries on regardless). */
export function within<T>(work: Promise<T>, ms: number, fallback: T): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => resolve(fallback), ms);
    work.then((value) => {
      clearTimeout(timer);
      resolve(value);
    }, (error) => {
      clearTimeout(timer);
      reject(error);
    });
  });
}

/** Run a panel's calculation; one failing panel must never take the page down. */
export async function soft<T>(work: () => T | Promise<T>, fallback: T): Promise<T> {
  try {
    return await work();
  } catch {
    return fallback;
  }
}
