// The forex board. The figures are worked out by the app's one server
// function (api/fx.ts), because the central bank's rates cannot be read
// straight from a browser. If the function is missing or down, only this
// board is empty.

import { DataError, onReset, runtime } from "./runtime";
import type { ForexPair } from "./types";

const KEEP_MS = 3600 * 1000;           // the rates change once a working day
const RETRY_AFTER_MS = 5 * 60 * 1000;  // after a failure, leave the function alone for a while

let held: { at: number; pairs: ForexPair[] } | null = null;
let failedAt = -Infinity;
let loading: Promise<ForexPair[]> | null = null;

onReset(() => {
  held = null;
  failedAt = -Infinity;
  loading = null;
});

const unavailable = () => new DataError("Exchange-rate history is unavailable right now.");

/** This week's picture for every pair (forex_weekly.overview()["pairs"]). */
export function pairs(): Promise<ForexPair[]> {
  const now = runtime.now();
  if (held && now - held.at < KEEP_MS) return Promise.resolve(held.pairs);
  if (now - failedAt < RETRY_AFTER_MS) return Promise.reject(unavailable());
  if (loading) return loading;
  const request = (async () => {
    try {
      const response = await runtime.fetch(runtime.fxUrl);
      if (!response.ok) throw unavailable();
      const board = await response.json() as { pairs?: ForexPair[] };
      if (!Array.isArray(board.pairs)) throw unavailable();
      held = { at: now, pairs: board.pairs };
      return board.pairs;
    } catch {
      failedAt = now;
      throw unavailable();
    }
  })().finally(() => {
    if (loading === request) loading = null;
  });
  loading = request;
  return request;
}
