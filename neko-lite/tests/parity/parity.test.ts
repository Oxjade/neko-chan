// The TypeScript core against the Python reference, on real recorded data.
//
// tests/parity/make_fixtures.py records every raw upstream response at one
// frozen moment and saves what the Python functions return from them. This
// test replays the same raw responses through the TypeScript core (its fetch
// and clock are swapped for the recording) and compares every field.
//
// Numbers must agree to 1e-9 (relative or absolute); everything else exactly.
// The fixtures are large and not committed; without them the test is skipped.

import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterAll, describe, expect, it } from "vitest";

import { GET as fxHandler } from "../../api/fx";
import { configure, getAdvanced, getBook, getChart, getScoreboard, getTerminal, getWeek } from "../../src/core/api";
import { settled } from "../../src/core/history";
import { FNG_URL, HL_URL } from "../../src/core/runtime";
import type { ChartInterval } from "../../src/core/types";

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), "fixtures");
const recorded = existsSync(join(FIXTURES, "meta.json"));
const TOLERANCE = 1e-9;

const canonical = (value: unknown): string => JSON.stringify(sortKeys(value));

function sortKeys(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortKeys);
  if (value && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
    return Object.fromEntries(entries.map(([key, v]) => [key, sortKeys(v)]));
  }
  return value;
}

interface Tally {
  fields: number;
  numbers: number;
  worst: number;
  worstAt: string;
  problems: string[];
}

const totals: Tally = { fields: 0, numbers: 0, worst: 0, worstAt: "", problems: [] };
// Numbers that agree within the tolerance but not to the last bit, by field (list positions removed).
const inexact = new Map<string, { count: number; worst: number }>();
const perResult: Array<{ name: string; fields: number; worst: number; worstAt: string; problems: number }> = [];

/** Walk both values together, counting every leaf and noting every disagreement. */
function compare(mine: unknown, theirs: unknown, path: string, tally: Tally): void {
  if (Array.isArray(theirs)) {
    if (!Array.isArray(mine)) {
      tally.problems.push(`${path}: expected a list, got ${JSON.stringify(mine)?.slice(0, 60)}`);
      return;
    }
    if (mine.length !== theirs.length) {
      tally.problems.push(`${path}: ${mine.length} items, Python has ${theirs.length}`);
    }
    for (let i = 0; i < Math.min(mine.length, theirs.length); i += 1) compare(mine[i], theirs[i], `${path}[${i}]`, tally);
    return;
  }
  if (theirs !== null && typeof theirs === "object") {
    if (mine === null || typeof mine !== "object" || Array.isArray(mine)) {
      tally.problems.push(`${path}: expected an object, got ${JSON.stringify(mine)?.slice(0, 60)}`);
      return;
    }
    const a = mine as Record<string, unknown>;
    const b = theirs as Record<string, unknown>;
    // undefined is how TypeScript says "absent"; JSON would drop it.
    const mineKeys = Object.keys(a).filter((key) => a[key] !== undefined);
    const theirKeys = Object.keys(b);
    for (const key of theirKeys) if (!mineKeys.includes(key)) tally.problems.push(`${path}.${key}: missing`);
    for (const key of mineKeys) if (!theirKeys.includes(key)) tally.problems.push(`${path}.${key}: not in Python`);
    if (mineKeys.join("|") !== theirKeys.join("|") && [...mineKeys].sort().join("|") === [...theirKeys].sort().join("|")) {
      tally.problems.push(`${path}: keys in a different order (${mineKeys.join(",")} / ${theirKeys.join(",")})`);
    }
    for (const key of theirKeys) if (mineKeys.includes(key)) compare(a[key], b[key], `${path}.${key}`, tally);
    return;
  }
  tally.fields += 1;
  if (typeof theirs === "number") {
    tally.numbers += 1;
    if (typeof mine !== "number" || Number.isNaN(mine)) {
      tally.problems.push(`${path}: ${JSON.stringify(mine)} is not the number ${theirs}`);
      return;
    }
    const gap = Math.abs(mine - theirs);
    const relative = gap === 0 ? 0 : gap / Math.max(Math.abs(mine), Math.abs(theirs));
    if (relative > tally.worst) {
      tally.worst = relative;
      tally.worstAt = `${path} (${mine} vs ${theirs})`;
    }
    if (relative > 0) {
      const field = path.replace(/\[\d+\]/g, "[]");
      const seen = inexact.get(field) ?? { count: 0, worst: 0 };
      inexact.set(field, { count: seen.count + 1, worst: Math.max(seen.worst, relative) });
    }
    if (relative > TOLERANCE && gap > TOLERANCE) tally.problems.push(`${path}: ${mine} vs ${theirs}`);
    return;
  }
  if (mine !== theirs) tally.problems.push(`${path}: ${JSON.stringify(mine)} vs ${JSON.stringify(theirs)}`);
}

describe.skipIf(!recorded)("parity with the Python reference on recorded data", () => {
  const meta = recorded ? JSON.parse(readFileSync(join(FIXTURES, "meta.json"), "utf8")) as {
    now_ms: number; stake: number; charts: Array<[string, ChartInterval]>; books: string[];
  } : { now_ms: 0, stake: 18, charts: [], books: [] };
  const hl = new Map<string, string>();
  const http = new Map<string, string>();
  const asked: string[] = [];
  const unanswered: string[] = [];

  if (recorded) {
    const index = JSON.parse(readFileSync(join(FIXTURES, "raw/hl/index.json"), "utf8")) as
      Array<{ payload: unknown; file: string }>;
    for (const entry of index) hl.set(canonical(entry.payload), join(FIXTURES, "raw/hl", entry.file));
    const files = JSON.parse(readFileSync(join(FIXTURES, "raw/http.json"), "utf8")) as Record<string, string>;
    for (const [url, file] of Object.entries(files)) http.set(url, join(FIXTURES, "raw", file));
  }

  const ecbFetch: typeof fetch = async (input) => {
    const file = http.get(String(input));
    if (!file) return new Response("not recorded", { status: 404 });
    return new Response(new Uint8Array(readFileSync(file)));
  };

  // Answers only with what was recorded; anything else is a failed request.
  const replay: typeof fetch = async (input, init) => {
    const url = String(input);
    if (url === HL_URL) {
      const key = canonical(JSON.parse(String(init?.body)));
      asked.push(key);
      const file = hl.get(key);
      if (!file) {
        unanswered.push(key);
        return new Response("not recorded", { status: 500 });
      }
      return new Response(readFileSync(file), { headers: { "Content-Type": "application/json" } });
    }
    if (url.startsWith(FNG_URL)) {
      const file = http.get(url);
      if (!file) {
        unanswered.push(url);
        return new Response("not recorded", { status: 500 });
      }
      return new Response(readFileSync(file));
    }
    if (url === "/api/fx") {
      // The forex board is worked out by the server function; run it on the recorded ECB file.
      const real = globalThis.fetch;
      globalThis.fetch = ecbFetch;
      try {
        return await fxHandler(new Request("http://localhost/api/fx"));
      } finally {
        globalThis.fetch = real;
      }
    }
    unanswered.push(url);
    return new Response("not recorded", { status: 500 });
  };

  const expected = (name: string): unknown => JSON.parse(readFileSync(join(FIXTURES, "expected", `${name}.json`), "utf8"));

  function check(name: string, mine: unknown): void {
    const tally: Tally = { fields: 0, numbers: 0, worst: 0, worstAt: "", problems: [] };
    compare(JSON.parse(JSON.stringify(mine)), expected(name), name, tally);
    totals.fields += tally.fields;
    totals.numbers += tally.numbers;
    totals.problems.push(...tally.problems);
    if (tally.worst > totals.worst) {
      totals.worst = tally.worst;
      totals.worstAt = tally.worstAt;
    }
    perResult.push({ name, fields: tally.fields, worst: tally.worst, worstAt: tally.worstAt, problems: tally.problems.length });
    expect(tally.problems, `${name}: fields that differ from Python`).toEqual([]);
  }

  const start = () => configure({
    fetch: replay, now: () => meta.now_ms, storage: null, weightPerMinute: Infinity, fxUrl: "/api/fx",
  });

  afterAll(() => {
    if (!recorded) return;
    const lines = perResult.map((r) =>
      `  ${r.name.padEnd(22)} ${String(r.fields).padStart(6)} fields  largest gap ${r.worst.toExponential(2)}` +
      `${r.problems ? `  ${r.problems} DIFFER` : ""}`);
    console.log([
      `Parity at ${new Date(meta.now_ms).toISOString()}: ${totals.fields} fields compared ` +
      `(${totals.numbers} numbers), ${totals.problems.length} differ.`,
      `Largest relative gap ${totals.worst.toExponential(3)} at ${totals.worstAt || "(none)"}`,
      ...lines,
      `Hyperliquid requests made by the TypeScript core: ${new Set(asked).size} distinct ` +
      `(${asked.length} in all), ${unanswered.length} not in the recording.`,
      `Numbers equal within the tolerance but not to the last bit: ` +
      `${[...inexact.values()].reduce((sum, f) => sum + f.count, 0)} of ${totals.numbers}` +
      `${inexact.size ? ", in these fields:" : "."}`,
      ...[...inexact.entries()].map(([field, f]) => `  ${field}: ${f.count} numbers, largest gap ${f.worst.toExponential(2)}`),
      ...totals.problems.slice(0, 40).map((p) => `  differs: ${p}`),
    ].join("\n"));
  });

  it("makes the same week's calls", async () => {
    start();
    check("week", await getWeek());
  });

  it("scores the last weeks the same, at the suggested leverage and at 5x", async () => {
    start();
    check("scoreboard_0", await getScoreboard(meta.stake, 0));
    check("scoreboard_5", await getScoreboard(meta.stake, 5));
  });

  it("builds the same terminal and advanced screens once every history has loaded", async () => {
    start();
    let advanced = await getAdvanced(meta.stake);            // starts the history downloads
    await settled();
    advanced = await getAdvanced(meta.stake);
    expect(advanced.pending).toBe(0);
    check("terminal", await getTerminal(meta.stake));
    const { pending: _pending, ...panels } = advanced;        // `pending` is the one field Python does not have
    check("advanced", panels);
  }, 120_000);

  it("draws the same charts", async () => {
    start();
    for (const [symbol, interval] of meta.charts) {
      check(`chart_${symbol.replace(":", "_")}_${interval}`, await getChart(symbol, interval));
    }
  });

  it("reads the same order book", async () => {
    start();
    for (const symbol of meta.books) check(`book_${symbol.replace(":", "_")}`, await getBook(symbol));
  });

  it("asked only for responses that were recorded", () => {
    expect(unanswered).toEqual([]);
  });
});
