// Advanced terminal: the dense panels are computed from live rows and saved tables.
// Port of service/server/tests/test_terminal_adv.py, with the same inputs and numbers.

import { beforeEach, describe, expect, it } from "vitest";

import { DataError, configure, getAdvanced, getBook } from "../src/core/api";
import { settled, want } from "../src/core/history";
import * as md from "../src/core/marketData";
import * as terminalAdv from "../src/core/terminalAdv";
import type { CallsConfig, Market, Position, SideConfig, SignalRow } from "../src/core/types";
import { FEE_PER_SIDE } from "../src/core/weeklyCalls";
import reference from "./fixtures/reference.json";
import { INTERVALS, candleRows, makeUpstream } from "./helpers/upstream";

function market(symbol: string, cls: Market["class"], change: number | null, volume = 1_000_000.0,
                over: Partial<Market> = {}): Market {
  return { symbol, label: symbol.split(":").pop() as string, class: cls, price: 100.0, day_change_pct: change,
           prev_day_price: change === null ? null : 100.0 / (1 + change / 100),
           day_volume_usd: volume, funding_hourly: 0.0000125, open_interest_usd: 5_000_000.0, max_leverage: 20,
           ...over };
}

const live = (...rows: Market[]) => new Map(rows.map((r) => [r.symbol, r]));

function side(allowed: boolean, suggested: number, liquidated: Record<number, number>,
              right: [number, number] = [0.6, 0.55]): SideConfig {
  return {
    allowed, suggested_leverage: suggested,
    first_half: { right: right[0], avg_pct: 2.0, coin_weeks: 50 },
    second_half: { right: right[1], avg_pct: 1.0, coin_weeks: 60 },
    by_leverage: Object.fromEntries(Object.entries(liquidated).map(([lev, rate]) =>
      [`${lev}x`, { liquidated: rate, avg_per_week: 0.01 * Number(lev), weeks: 0, ahead: 0 }])),
  };
}

const CONFIG: CallsConfig = {
  BTC: { long: side(true, 3, { 1: 0.0, 3: 0.0, 10: 0.16 }),
         short: side(false, 3, { 1: 0.0, 3: 0.01, 10: 0.2 }, [0.52, 0.48]) },
  ETH: { long: side(true, 3, { 1: 0.0, 3: 0.01, 10: 0.32 }),
         short: side(true, 1, { 1: 0.0, 3: 0.05 }) },
};

describe("movers and breadth", () => {
  it("movers rank within a class and skip thin markets", () => {
    const rows = live(market("A", "crypto", 5.0), market("B", "crypto", 2.0), market("C", "crypto", -1.0),
                      market("D", "crypto", -4.0), market("E", "crypto", 9.0, 10_000.0),
                      market("xyz:N", "stocks", 3.0));
    const out = terminalAdv.movers(rows);
    expect(out.crypto.up.map((m) => m.symbol)).toEqual(["A", "B"]);
    expect(out.crypto.down.map((m) => m.symbol)).toEqual(["D", "C"]);
    expect(out.crypto.count).toBe(4);                        // the thin market is not counted
  });

  it("a market never shows as both up and down", () => {
    const out = terminalAdv.movers(live(market("xyz:EUR", "forex", 0.2), market("xyz:JPY", "forex", 0.1)));
    expect(out.forex.up.length).toBe(2);
    expect(out.forex.down).toEqual([]);
    expect(out.stocks).toEqual({ up: [], down: [], count: 0 });
  });

  it("breadth counts small moves as flat", () => {
    const rows = live(market("A", "crypto", 1.0), market("B", "crypto", 0.05), market("C", "crypto", -0.3),
                      market("D", "crypto", null));
    expect(terminalAdv.breadth(rows).crypto).toEqual({ up: 1, down: 1, flat: 1 });
  });
});

describe("risk", () => {
  const position = (over: Partial<Position> = {}): Position => ({
    coin: "ETH", name: "Ether", side: "short", status: "open", leverage: 3, share: 4.5,
    position_usd: 13.5, pnl_usd: 1.2, pnl_pct: 0, move_pct: 0, entry: 2000.0, last: 1800.0, stop: 2180.0,
    target: null, liquidation: 2620.0, stop_distance_pct: 21.1, normal_week_pct: 9.0, ...over,
  });

  it("open position exposure and loss at the stop", () => {
    const out = terminalAdv.risk([position()], 18.0);
    const row = out.rows[0];
    expect(row.loss_at_stop_usd).toBeCloseTo(4.5 * 3 * (0.09 + 2 * FEE_PER_SIDE), 7);
    expect(row.liquidation_distance_pct).toBeCloseTo((2620 - 1800) / 1800 * 100, 7);
    expect(out.exposure_usd).toBeCloseTo(13.5, 7);
    expect(out.exposure_of_stake).toBeCloseTo(0.75, 7);
    expect(out.worst_case_usd).toBeCloseTo(-row.loss_at_stop_usd, 7);
    expect(out.in_cash_usd).toBeCloseTo(13.5, 7);
  });

  it("loss at the stop is capped at the stake", () => {
    const far = terminalAdv.risk([position({ leverage: 20, stop: 2400.0 })], 18.0).rows[0];
    expect(far.loss_at_stop_usd).toBe(4.5);
    const noStop = terminalAdv.risk([position({ stop: null })], 18.0).rows[0];
    expect(noStop.loss_at_stop_usd).toBe(4.5);
  });

  it("closed positions carry no exposure", () => {
    const out = terminalAdv.risk([position({ status: "stopped", pnl_usd: -1.2 })], 18.0);
    expect(out.exposure_usd).toBe(0.0);
    expect(out.worst_case_usd).toBeCloseTo(0.0, 12);
    expect(out.in_cash_usd).toBe(18.0);
    expect(out.rows[0].liquidation_distance_pct).toBeNull();
    expect(terminalAdv.risk([], 18.0).rows).toEqual([]);
  });
});

describe("saved tables", () => {
  it("leverage table lists only called sides", () => {
    const out = terminalAdv.leverageTable(CONFIG);
    expect(out.levels).toEqual([1, 3, 10]);
    expect(out.rows.map((r) => [r.coin, r.side])).toEqual([["BTC", "long"], ["ETH", "long"], ["ETH", "short"]]);
    const [btc, , ethShort] = out.rows;
    expect([btc.suggested, btc.liquidated["10"], btc.avg["3"]]).toEqual([3, 0.16, 0.03]);
    expect(ethShort.liquidated["10"]).toBeNull();            // a level that was never tested
  });

  it("signal matrix marks active and skipped sides", () => {
    const week = { coins: [{ coin: "BTC", side: null, signal: "short" as const },
                           { coin: "ETH", side: "short" as const, signal: "short" as const }] };
    const rows = new Map(terminalAdv.signalMatrix(week, CONFIG).map((r) => [`${r.coin} ${r.side}`, r]));
    expect(rows.size).toBe(4);
    const skipped = rows.get("BTC short") as SignalRow;
    expect([skipped.allowed, skipped.active, skipped.pointed]).toEqual([false, false, true]);
    const active = rows.get("ETH short") as SignalRow;
    expect([active.allowed, active.active, active.leverage]).toEqual([true, true, 1]);
    expect([active.right_first, active.right_second, active.weeks]).toEqual([0.6, 0.55, 110]);
  });

  it("funding is annualised and sorted by size", () => {
    const rows = live(market("BTC", "crypto", 1.0, 1e6, { funding_hourly: 0.00001 }),
                      market("SUI", "crypto", 1.0, 1e6, { funding_hourly: -0.00002 }),
                      market("xyz:NVDA", "stocks", 1.0, 1e6, { funding_hourly: 0.001 }));
    const out = terminalAdv.funding(rows);
    expect(out.map((r) => r.symbol)).toEqual(["SUI", "BTC"]);
    expect(out[0].funding_apr).toBeCloseTo(-0.00002 * 24 * 365 * 100, 7);
  });
});

describe("series", () => {
  const NOW = Date.UTC(2026, 10, 15, 21, 30);
  const MONTH = 30 * terminalAdv.BARS_PER_DAY;
  let up = makeUpstream(NOW);

  // Closed four-hour bars ending now, from a list of closes. The Python tests hand
  // the bars straight to the panel; here they come through the history loader, which
  // needs at least 242 bars, so short lists are padded at the old end with flat bars.
  const history = (closes: number[], pad = true) => {
    const all = pad && closes.length < md.MIN_BARS
      ? [...new Array<number>(md.MIN_BARS + 10 - closes.length).fill(closes[0]), ...closes] : closes;
    return candleRows(NOW, "4h", all.map((c) => [c, c, c, c] as [number, number, number, number]), true);
  };

  async function load(symbols: readonly string[]) {
    want(symbols);
    await settled();
  }

  beforeEach(() => {
    up = makeUpstream(NOW);
    configure({ fetch: up.fetch, now: () => up.now, storage: null, weightPerMinute: Infinity });
  });

  it("performance is rebased to 100", async () => {
    const ramp = Array.from({ length: MONTH + 1 }, (_, i) => 50 + (50 * i) / MONTH);
    for (const symbol of terminalAdv.PERFORMANCE) up.fixed.set(`${symbol}:4h`, history(ramp));
    await load(terminalAdv.PERFORMANCE);
    const out = terminalAdv.performance();
    expect(new Set(Object.keys(out))).toEqual(new Set(terminalAdv.PERFORMANCE.map(md.label)));
    const series = out.BTC;
    expect(series.length).toBe(61);
    expect(series[0]).toBeCloseTo(100.0, 7);
    expect(series[series.length - 1]).toBeCloseTo(200.0, 7);
  });

  it("performance skips markets with too little history", async () => {
    const few = Array.from({ length: 40 }, (_, i) => 1 + i / 39);
    for (const symbol of terminalAdv.PERFORMANCE) up.fixed.set(`${symbol}:4h`, history(few, false));
    up.failWith = (sent) => (sent.payload?.req?.coin === "BTC" ? 500 : null);
    await load(terminalAdv.PERFORMANCE);
    expect(terminalAdv.performance()).toEqual({});
  });

  it("correlation of markets that move together and apart", async () => {
    const base = reference.correlation.base;                  // the random walk the Python test draws
    for (const symbol of terminalAdv.CORRELATION) up.fixed.set(`${symbol}:4h`, history(base));
    up.fixed.set("ETH:4h", history(base.map((v) => v * 3)));  // same moves, different price
    up.fixed.set("SOL:4h", history(base.map((v) => 1 / v)));  // exactly opposite
    await load(terminalAdv.CORRELATION);
    const out = terminalAdv.correlation();
    const i = Object.fromEntries(out.labels.map((name, n) => [name, n]));
    expect(out.matrix[i.BTC][i.ETH]).toBeCloseTo(1.0, 7);
    expect(out.matrix[i.BTC][i.SOL]).toBeCloseTo(-1.0, 7);
    expect(out.bars).toBe(MONTH + 1);
  });

  it("correlation is empty without shared history", async () => {
    const few = Array.from({ length: 10 }, (_, i) => 1 + i / 9);
    for (const symbol of terminalAdv.CORRELATION) up.fixed.set(`${symbol}:4h`, history(few, false));
    await load(terminalAdv.CORRELATION);
    expect(terminalAdv.correlation()).toEqual({ labels: [], matrix: [] });
  });

  it("order book parses levels and limits depth", async () => {
    const real = up.fetch;
    const levels = (first: number, step: number, sz: string) =>
      Array.from({ length: 20 }, (_, i) => ({ px: String(first + step * i), sz, n: 1 }));
    configure({ fetch: (async (input: RequestInfo | URL, init?: RequestInit) => {
      const payload = JSON.parse(String(init?.body ?? "{}")) as { type?: string };
      if (payload.type !== "l2Book") return real(input, init);
      return new Response(JSON.stringify({ time: 5, levels: [levels(100, -1, "2"), levels(101, 1, "3")] }));
    }) as typeof fetch });
    const book = await md.orderBook("ETH", 5);
    expect([book.bids.length, book.asks.length]).toEqual([5, 5]);
    expect([book.bids[0], book.asks[0]]).toEqual([{ px: 100.0, sz: 2.0 }, { px: 101.0, sz: 3.0 }]);
    expect(book.time).toBe(5);
  });

  it("a market's history is asked for once, then only when a bar has closed", async () => {
    await load(["BTC"]);
    expect(up.candleRequests("4h").length).toBe(1);
    up.now += 30 * 60 * 1000;                                 // half an hour: still the same bar
    await load(["BTC"]);
    expect(up.candleRequests("4h").length).toBe(1);
    up.now += INTERVALS["4h"];                                // a bar has closed since
    const before = up.rowsReturned;
    await load(["BTC"]);
    expect(up.candleRequests("4h").length).toBe(2);
    expect(up.rowsReturned - before).toBeLessThanOrEqual(3);  // the last bar again, the new one, the forming one
  });
});

describe("snapshot", () => {
  const NOW = Date.UTC(2026, 10, 15, 21, 30);

  it("one broken panel does not take the page down", async () => {
    const up = makeUpstream(NOW);
    configure({ fetch: up.fetch, now: () => up.now, storage: null, weightPerMinute: Infinity });
    const rows = live(market("BTC", "crypto", 1.0), market("ETH", "crypto", -2.0));
    const week = { coins: [{ coin: "BTC", side: "long" as const, signal: "long" as const },
                           { coin: "ETH", side: null, signal: null }] };
    const broken = () => { throw new Error("source is down"); };
    const out = await terminalAdv.snapshot(18.0, {
      catalogue: async () => rows,
      terminal: async () => ({ positions: [] }),
      week: async () => week,
      config: () => CONFIG,
      scoreboard: broken, exits: broken, worksheet: broken, performance: broken, correlation: broken,
      forex: broken, moodHistory: broken,
    });
    expect(out.worksheet).toEqual([]);
    expect(out.performance).toEqual({});
    expect(out.correlation).toEqual({ labels: [], matrix: [] });
    expect(out.scoreboard).toBeNull();
    expect(out.exits).toBeNull();
    expect([out.forex, out.mood_history]).toEqual([[], []]);
    // the panels whose sources are up still arrive
    expect(out.breadth.crypto).toEqual({ up: 1, down: 1, flat: 0 });
    expect(out.signals.length).toBe(4);
    expect(out.risk.in_cash_usd).toBe(18.0);
    expect(out.markets).toBe(2);
  });

  it("the forex function being down empties only the forex board", async () => {
    const up = makeUpstream(NOW);
    up.forex = null;
    configure({ fetch: up.fetch, now: () => up.now, storage: null, weightPerMinute: Infinity });
    await getAdvanced(18);
    await settled();
    const out = await getAdvanced(18);
    expect(out.forex).toEqual([]);
    expect(out.worksheet.length).toBe(30);
    expect(out.pending).toBe(0);
    expect(out.mood_history.length).toBe(120);
    expect(out.scoreboard).not.toBeNull();
    // and the function is left alone for a while instead of being asked every twenty seconds
    const asked = up.sent.filter((s) => s.url === "/api/fx").length;
    await getAdvanced(18);
    expect(up.sent.filter((s) => s.url === "/api/fx").length).toBe(asked);
  });
});

describe("a slow forex function", () => {
  it("does not hold the other panels back; the board fills in on a later look", async () => {
    const up = makeUpstream(Date.UTC(2026, 10, 15, 21, 30));
    let answer: () => void = () => {};
    const slow = new Promise<void>((resolve) => { answer = resolve; });
    const real = up.fetch;
    configure({ fetch: (async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === "/api/fx") await slow;
      return real(input, init);
    }) as typeof fetch, now: () => up.now, storage: null, weightPerMinute: Infinity });
    const started = Date.now();
    const first = await getAdvanced(18);
    expect(Date.now() - started).toBeLessThan(4000);
    expect(first.forex).toEqual([]);
    expect(first.funding.length).toBeGreaterThan(0);
    answer();
    await settled();
    const later = await getAdvanced(18);
    expect(later.forex.map((p) => p.pair)).toEqual(["EURUSD"]);
    expect(up.sent.filter((s) => s.url === "/api/fx").length).toBe(1);    // the same request, not a second one
  });
});

describe("what the server used to refuse", () => {
  const NOW = Date.UTC(2026, 10, 15, 21, 30);
  let up = makeUpstream(NOW);

  beforeEach(() => {
    up = makeUpstream(NOW);
    configure({ fetch: up.fetch, now: () => up.now, storage: null, weightPerMinute: Infinity });
  });

  it("a bad weekly amount, an unknown market, and a source that is down", async () => {
    await expect(getAdvanced(-1)).rejects.toThrow("Enter the weekly amount in USD.");
    expect((await getBook("ETH")).symbol).toBe("ETH");
    await expect(getBook("NOPE")).rejects.toThrow("That market is not available.");
    configure({});
    up.failWith = (sent) => (sent.payload?.type === "metaAndAssetCtxs" ? 502 : null);
    await expect(getAdvanced(18)).rejects.toBeInstanceOf(DataError);
    await expect(getAdvanced(18)).rejects.toThrow("Market data is unavailable right now (HTTPError).");
  });
});
