// Price history is downloaded once: kept in memory and in storage, topped up
// with only the newest bars, and never asked for again until a bar has closed.

import { beforeEach, describe, expect, it } from "vitest";

import { configure, getAdvanced, getTerminal } from "../src/core/api";
import { HISTORY_BARS, historyStatus, settled, want } from "../src/core/history";
import * as md from "../src/core/marketData";
import { WORKSHEET } from "../src/core/terminalAdv";
import { INTERVALS, makeUpstream, memoryStorage, type Upstream } from "./helpers/upstream";

const H4 = INTERVALS["4h"];
const NOW = Date.UTC(2026, 10, 11, 9, 30);
const MARKETS = ["BTC", "ETH", "xyz:NVDA"];

const bars = (symbol: string) => {
  const status = historyStatus(symbol);
  if (status.state !== "ready") throw new Error(`${symbol} is ${status.state}`);
  return status.bars;
};

async function load(symbols: readonly string[]): Promise<void> {
  want(symbols);
  await settled();
}

describe("price history between visits", () => {
  let up: Upstream;
  let storage: ReturnType<typeof memoryStorage>;
  // A new page load: memory is empty again, storage is not.
  const visit = () => configure({ fetch: up.fetch, now: () => up.now, storage, weightPerMinute: Infinity });

  beforeEach(() => {
    up = makeUpstream(NOW);
    storage = memoryStorage();
    visit();
  });

  it("downloads each market once and stores it as plain number arrays", async () => {
    await load(MARKETS);
    expect(up.candleRequests("4h").length).toBe(3);
    expect(storage.held.size).toBe(3);
    const stored = [...storage.held.values()][0];
    expect(stored.close).toBeInstanceOf(Float64Array);
    expect(stored.t.length).toBe(HISTORY_BARS);
    const request = up.candleRequests("4h")[0].payload?.req;
    expect(request).toEqual({ coin: "BTC", interval: "4h", startTime: NOW - HISTORY_BARS * H4, endTime: NOW });
  });

  it("a second visit in the same four hours asks the exchange for no history at all", async () => {
    await load(MARKETS);
    const before = MARKETS.map((symbol) => Array.from(bars(symbol).close));
    visit();
    await load(MARKETS);
    expect(up.candleRequests("4h").length).toBe(3);
    expect(MARKETS.map((symbol) => Array.from(bars(symbol).close))).toEqual(before);
  });

  it("a later visit downloads only the bars since the last one stored, and ends up with what a full download gives", async () => {
    await load(MARKETS);
    up.now += 9 * 3600 * 1000;                               // nine hours later: two more bars have closed
    visit();
    const rows = up.rowsReturned;
    await load(MARKETS);
    const tails = up.candleRequests("4h").slice(3);
    expect(tails.length).toBe(3);
    expect(up.rowsReturned - rows).toBeLessThanOrEqual(3 * 4);       // a handful of rows, not 15,000
    for (const tail of tails) expect(tail.payload?.req?.startTime).toBeGreaterThan(up.now - 4 * H4);
    const topped = MARKETS.map((symbol) => ({ t: Array.from(bars(symbol).t), close: Array.from(bars(symbol).close) }));

    // the same moment with nothing stored
    storage = memoryStorage();
    visit();
    await load(MARKETS);
    const fresh = MARKETS.map((symbol) => ({ t: Array.from(bars(symbol).t), close: Array.from(bars(symbol).close) }));
    expect(topped).toEqual(fresh);
    expect(topped[0].t.length).toBe(HISTORY_BARS);           // the two oldest bars dropped out of the window
  });

  it("starts over when what is stored is older than the whole window", async () => {
    await load(["BTC"]);
    up.now += (HISTORY_BARS + 10) * H4;
    visit();
    await load(["BTC"]);
    const again = up.candleRequests("4h")[1].payload?.req;
    expect(again?.startTime).toBe(up.now - HISTORY_BARS * H4);
    expect(bars("BTC").t.length).toBe(HISTORY_BARS);
  });

  it("serves only closed bars", async () => {
    await load(["BTC"]);
    const t = bars("BTC").t;
    expect(t[t.length - 1] + H4).toBeLessThanOrEqual(up.now);
    expect(t[t.length - 1] + 2 * H4).toBeGreaterThan(up.now);
  });

  it("a market listed too recently has no trend yet, and says why", async () => {
    up.listedSince.set("xyz:NVDA", NOW - 100 * H4);
    await load(["xyz:NVDA"]);
    const status = md.candles("xyz:NVDA");
    expect(status.state).toBe("failed");
    expect(status.state === "failed" && status.error.message).toBe("NVDA has too little price history for a signal yet.");
    const snap = await getTerminal(18);
    await settled();
    const nvda = (await getTerminal(18)).markets.find((m) => m.symbol === "xyz:NVDA");
    expect(nvda?.note).toBe("NVDA has too little price history for a signal yet.");
    expect(nvda?.spark).toEqual([]);
    expect(snap.markets.length).toBe(8);
  });
});

describe("the screens while history is still downloading", () => {
  let up: Upstream;

  beforeEach(() => {
    up = makeUpstream(NOW);
    configure({ fetch: up.fetch, now: () => up.now, storage: null, weightPerMinute: Infinity });
  });

  it("the basic screen answers at once and fills in the trend when the history arrives", async () => {
    // Hold every long download back until the test lets it through.
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const real = up.fetch;
    configure({ fetch: (async (input: RequestInfo | URL, init?: RequestInit) => {
      const payload = JSON.parse(String(init?.body ?? "{}")) as { req?: { interval: string; startTime: number; endTime: number } };
      if (payload.req?.interval === "4h" && payload.req.endTime - payload.req.startTime > 2000 * H4) await gate;
      return real(input, init);
    }) as typeof fetch });

    const first = await getTerminal(18);
    expect(first.markets.length).toBe(8);
    expect(first.markets.every((m) => m.note.startsWith("Loading price history."))).toBe(true);
    expect(first.markets.every((m) => m.spark.length === 0 && m.price !== null)).toBe(true);
    expect(first.markets[0].recommendation.votes.map((v) => v.name)).toEqual(["Market mood", "Last four weeks"]);

    const advanced = await getAdvanced(18);
    expect(advanced.worksheet).toEqual([]);
    expect(advanced.pending).toBe(30);
    expect(advanced.performance).toEqual({});
    expect(advanced.movers.crypto).toBeDefined();            // panels that need no history are already there
    expect(advanced.scoreboard).not.toBeNull();

    release();
    await settled();
    const later = await getTerminal(18);
    expect(later.markets.every((m) => /^Trend (up|down) \d+d\. /.test(m.note))).toBe(true);
    expect(later.markets.every((m) => m.spark.length === 42)).toBe(true);
    expect(later.markets[0].recommendation.votes[0].name).toBe("10-day against 40-day average");
    const full = await getAdvanced(18);
    expect(full.pending).toBe(0);
    expect(full.worksheet.map((r) => r.symbol)).toEqual([...WORKSHEET]);
    expect(Object.keys(full.performance).length).toBe(7);
    expect(full.correlation.labels.length).toBe(8);
  });

  it("a market whose download fails is left out and not counted as loading", async () => {
    up.failWith = (sent) => (sent.payload?.req?.coin === "DOGE" && sent.payload.req.interval === "4h" ? 500 : null);
    await getAdvanced(18);
    await settled();
    const out = await getAdvanced(18);
    expect(out.pending).toBe(0);
    expect(out.worksheet.length).toBe(29);
    expect(out.worksheet.map((r) => r.symbol)).not.toContain("DOGE");
  });

  it("asks for the catalogue at most every ten seconds, however often the screen asks", async () => {
    await getAdvanced(18);
    await settled();
    const catalogues = () => up.sent.filter((s) => s.payload?.type === "metaAndAssetCtxs").length;
    const start = catalogues();
    for (let second = 1; second <= 60; second += 1) {
      up.now += 1000;
      if (second % 5 === 0) await getTerminal(18);           // the basic poll
      if (second % 20 === 0) await getAdvanced(18);          // the advanced poll
    }
    expect(catalogues() - start).toBeLessThanOrEqual(2 * 6);  // two calls (main and xyz) per refresh
    expect(catalogues() - start).toBeGreaterThanOrEqual(2 * 5);
  });
});
