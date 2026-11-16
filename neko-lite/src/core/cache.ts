// Keep an answer for a while, and let callers who ask at the same moment
// share one request instead of each sending their own.

import { onReset, runtime } from "./runtime";

const entries = new Map<string, { at: number; value: unknown }>();
const inFlight = new Map<string, Promise<unknown>>();
let generation = 0;

onReset(() => {
  generation += 1;
  entries.clear();
  inFlight.clear();
});

/** The value under `key` if it is younger than `ttlSeconds`, otherwise load it afresh. */
export function cached<T>(key: string, ttlSeconds: number, load: () => Promise<T>): Promise<T> {
  const now = runtime.now();
  const hit = entries.get(key);
  if (hit && now - hit.at < ttlSeconds * 1000) return Promise.resolve(hit.value as T);
  const running = inFlight.get(key);
  if (running) return running as Promise<T>;
  const mine = generation;
  const request = load().then((value) => {
    if (mine === generation) entries.set(key, { at: now, value });
    return value;
  }).finally(() => {
    if (inFlight.get(key) === request) inFlight.delete(key);
  });
  inFlight.set(key, request);
  return request;
}
