// Live terminal: model position state, the advice it gives, and recommendations.
// Port of service/server/tests/test_terminal.py, with the same inputs and numbers.

import { beforeEach, describe, expect, it } from "vitest";

import { DataError, configure, getChart, getTerminal } from "../src/core/api";
import * as terminal from "../src/core/terminal";
import type { CalledCoin } from "../src/core/terminal";
import type { Position } from "../src/core/types";
import { candleRows, makeUpstream } from "./helpers/upstream";

const shortEth = (over: Partial<CalledCoin> = {}): CalledCoin => ({
  coin: "ETH", name: "Ether", side: "short", leverage: 3, entry: 2000.0,
  stop_price: 2180.0, target_price: 1500.0, liquidation_price: 2620.0, normal_week_pct: 9.0, ...over,
});

const longBtc = (over: Partial<CalledCoin> = {}): CalledCoin => ({
  coin: "BTC", name: "Bitcoin", side: "long", leverage: 3, entry: 100.0,
  stop_price: 94.0, target_price: 118.0, liquidation_price: 68.0, normal_week_pct: 6.0, ...over,
});

const WEEK: terminal.AdviceWeek & Parameters<typeof terminal.recommendation>[2] = {
  week_of: "2026-11-09", next_call: "2026-11-16", preview: null,
  coins: [
    { coin: "ETH", name: "Ether", side: "short", skipped: false, signal: "short", mood_greedy: false,
      four_week_change_pct: -16.0, leverage: 3 },
    { coin: "BTC", name: "Bitcoin", side: null, skipped: true, signal: "short", mood_greedy: false,
      four_week_change_pct: -10.0, leverage: null },
  ],
};

const at = (day: number, hour = 12) => Date.UTC(2026, 10, day, hour);

describe("position state", () => {
  it("open short in profit", () => {
    const p = terminal.positionState(shortEth(), [{ high: 2050.0, low: 1780.0 }], 1800.0, 9.0);
    expect(p.status).toBe("open");
    expect(p.move_pct).toBeCloseTo(10.0, 7);
    expect(p.pnl_usd).toBeCloseTo(9.0 * 3 * (0.10 - 0.0009), 7);
    expect(p.stop_distance_pct).toBeCloseTo((2180 - 1800) / 1800 * 100, 7);
    expect(p.stop_room_left).toBe(1.5);                       // well clear of the stop
    expect(p.target_distance_pct).toBeCloseTo((1800 - 1500) / 1800 * 100, 7);
  });

  it("short is stopped when a day trades through the stop", () => {
    const days = [{ high: 2100.0, low: 1950.0 }, { high: 2200.0, low: 2000.0 }];
    const p = terminal.positionState(shortEth(), days, 2050.0, 9.0);
    expect(p.status).toBe("stopped");
    expect(p.move_pct).toBeCloseTo(-9.0, 7);                  // closed at the stop, not at today's price
    expect(p.pnl_usd).toBeLessThan(0);
    expect(p).not.toHaveProperty("stop_distance_pct");
  });

  it("long stop, liquidation and target", () => {
    const stopped = terminal.positionState(longBtc(), [{ high: 101.0, low: 93.0 }], 97.0, 9.0);
    expect(stopped.status).toBe("stopped");
    const gone = terminal.positionState(longBtc({ stop_price: null }), [{ high: 101.0, low: 60.0 }], 70.0, 9.0);
    expect([gone.status, gone.pnl_usd]).toEqual(["liquidated", -9.0]);
    const took = terminal.positionState(longBtc(), [{ high: 119.0, low: 99.0 }], 110.0, 9.0);
    expect(took.status).toBe("target");                       // closed at the target, not at today's price
    expect(took.pnl_usd).toBeCloseTo(9.0 * 3 * (0.18 - 0.0009), 7);
    expect(took).not.toHaveProperty("stop_distance_pct");
    expect(took).not.toHaveProperty("target_distance_pct");
    // a day that touches both the stop and the target counts as the stop
    const both = terminal.positionState(longBtc(), [{ high: 119.0, low: 93.0 }], 110.0, 9.0);
    expect(both.status).toBe("stopped");
  });

  it("short takes profit at its target", () => {
    const days = [{ high: 2050.0, low: 1990.0 }, { high: 2000.0, low: 1790.0 }];
    const p = terminal.positionState(shortEth({ target_price: 1820.0 }), days, 1900.0, 9.0);
    expect(p.status).toBe("target");
    expect(p.move_pct).toBeCloseTo(9.0, 7);                   // banked at the target despite the bounce
    expect(p.pnl_usd).toBeCloseTo(9.0 * 3 * (0.09 - 0.0009), 7);
  });

  it("near the stop", () => {
    const p = terminal.positionState(longBtc(), [{ high: 101.0, low: 95.0 }], 95.0, 9.0);
    expect(p.status).toBe("open");
    expect(p.stop_room_left as number).toBeLessThan(terminal.NEAR_STOP);
  });
});

describe("advice", () => {
  const advice = (position: Position, when = at(11), week: terminal.AdviceWeek = WEEK) =>
    terminal.advice([position], week, when);
  const keys = (items: Array<{ key: string }>) => items.map((i) => i.key);

  it("a long in profit is held because longs have no target", () => {
    const p = terminal.positionState(longBtc({ target_price: null }), [{ high: 111.0, low: 99.0 }], 110.0, 9.0);
    const top = terminal.advice([p], { ...WEEK, coins: [] }, at(11))[0];
    expect([top.level, top.key]).toEqual(["good", "profit:BTC"]);
    expect(top.text).toContain("Hold to Monday");
    expect(top.text).toContain("no take profit on longs");
  });

  it("take profit hit closes the trade", () => {
    const p = terminal.positionState(shortEth({ target_price: 1820.0 }), [{ high: 2000.0, low: 1790.0 }], 1900.0, 9.0);
    const items = advice(p);
    expect([items[0].level, items[0].key]).toEqual(["good", "target:ETH"]);
    expect(items[0].text).toContain("take profit");
    expect(items[0].text).toContain("Do not re-enter");
    expect(keys(items)).not.toContain("close-soon");         // nothing left open to close
  });

  it("a short on track is told where its take profit is", () => {
    const p = terminal.positionState(shortEth({ target_price: 1820.0 }), [{ high: 2010.0, low: 1950.0 }], 1960.0, 9.0);
    const top = advice(p)[0];
    expect(top.key).toBe("hold:ETH");
    expect(top.text).toContain("Take profit at 1,820");
  });

  it("a call too small for the exchange is flagged", () => {
    const small = [{ coin: "SOL", name: "Solana", side: "short" as const, leverage: 1, position_usd: 4.5,
                     min_weekly_amount: 40.0 }];
    const items = terminal.advice([], { ...WEEK, coins: [] }, at(11), small);
    expect([items[0].level, items[0].key]).toEqual(["warn", "small:SOL"]);
    expect(items[0].text).toContain("$4.50");
    expect(items[0].text).toContain("at least $40");
  });

  it("stop hit is the most urgent", () => {
    const p = terminal.positionState(shortEth(), [{ high: 2200.0, low: 2000.0 }], 2050.0, 9.0);
    const items = advice(p);
    expect([items[0].level, items[0].key]).toEqual(["critical", "stop:ETH"]);
    expect(items[0].text).toContain("Do not re-enter");
    expect(items[0].text).toContain("hit its stop at 2,180 (-$2.45)");
    expect(keys(items)).not.toContain("close-soon");         // nothing left open to close
  });

  it("near stop warns to leave it", () => {
    const p = terminal.positionState(longBtc(), [{ high: 101.0, low: 95.0 }], 95.0, 9.0);
    const top = terminal.advice([p], { ...WEEK, coins: [] }, at(10))[0];
    expect([top.level, top.key]).toEqual(["warn", "near:BTC"]);
    expect(top.text).toContain("Leave the stop where it is");
    expect(top.text).toContain("Price 95.00 is 1.1% from the stop at 94.00.");
  });

  it("small loss is no action", () => {
    const p = terminal.positionState(shortEth(), [{ high: 2060.0, low: 1990.0 }], 2040.0, 9.0);
    const top = advice(p)[0];
    expect(top.key).toBe("down:ETH");
    expect(top.text).toContain("No action");
  });

  it("close reminder, late entry and stay out", () => {
    const p = terminal.positionState(shortEth(), [{ high: 2050.0, low: 1900.0 }], 1950.0, 9.0);
    const items = advice(p, at(15, 21));                      // three hours before Monday
    expect(keys(items)).toContain("close-soon");
    const soon = items.find((i) => i.key === "close-soon") as (typeof items)[number];
    expect(soon.level).toBe("warn");
    expect(soon.text).toContain("in 3h 00m");
    expect(keys(items)).toContain("late");
    expect(keys(items)).toContain("out:BTC");
    const early = keys(advice(p, at(9, 6)));                  // Monday morning: nothing to nag about
    expect(early).not.toContain("close-soon");
    expect(early).not.toContain("late");
  });

  it("preview appears near Monday", () => {
    const week = { ...WEEK, preview: { coins: [{ coin: "ETH", name: "Ether", side: "short" as const, leverage: 3 }] } };
    const p = terminal.positionState(shortEth(), [], 1990.0, 9.0);
    const late = terminal.advice([p], week, at(15, 12));
    expect((late.find((i) => i.key === "preview") as (typeof late)[number]).text).toContain("short Ether at 3x");
    expect(keys(terminal.advice([p], week, at(10)))).not.toContain("preview");
  });
});

describe("the sentences, word for word as Python writes them", () => {
  const texts = (row: CalledCoin, days: Array<{ high: number; low: number }>, price: number, when: number,
                 week: terminal.AdviceWeek = WEEK) =>
    terminal.advice([terminal.positionState(row, days, price, 9.0)], week, when).map((i) => `${i.key} | ${i.text}`);

  it("down, on track, the close reminder and liquidation", () => {
    expect(texts(shortEth(), [{ high: 2060.0, low: 1990.0 }], 2040.0, at(11))[0]).toBe(
      "down:ETH | The short Ether is down 2.0% (-$0.56), 6.9% from the stop. No action: the stop is the exit.");
    const soon = texts(shortEth(), [{ high: 2050.0, low: 1900.0 }], 1950.0, at(15, 21));
    expect(soon).toEqual([
      "close-soon | Close Ether at 00:00 UTC Monday, in 3h 00m. Then take the new call.",
      "hold:ETH | The short Ether is up 2.5% (+$0.65). No action. Take profit at 1,500, 23.1% away. Stop at 2,180.",
      "late | These trades were opened at Monday's open. Entering part-way through the week was not tested, " +
        "so if you are not already in, skip to the next call.",
      "out:BTC | The rule points short, but going short on Bitcoin did not hold up in testing.",
    ]);
    const earlier = texts(shortEth(), [{ high: 2050.0, low: 1900.0 }], 1950.0, Date.UTC(2026, 10, 15, 2, 17));
    expect(earlier[1]).toBe("close-soon | Close Ether at 00:00 UTC Monday, in 21h 43m. Then take the new call.");
    expect(texts(longBtc({ stop_price: null }), [{ high: 101.0, low: 60.0 }], 70.0, at(11))[0]).toBe(
      "liq:BTC | The long Bitcoin was liquidated near 68.00. That share is gone. Do not re-enter this week.");
    expect(texts(longBtc({ stop_price: null }), [{ high: 103.0, low: 99.0 }], 102.5, at(11))[0]).toBe(
      "hold:BTC | The long Bitcoin is up 2.5% (+$0.65). No action.");
    expect(texts(longBtc(), [{ high: 103.0, low: 99.0 }], 102.5, at(11))[0]).toBe(
      "hold:BTC | The long Bitcoin is up 2.5% (+$0.65). No action. Take profit at 118.00, 15.1% away. Stop at 94.00.");
  });

  it("take profit hit, and in profit with no take profit", () => {
    expect(texts(shortEth({ target_price: 1820.0 }), [{ high: 2000.0, low: 1790.0 }], 1900.0, at(11))[0]).toBe(
      "target:ETH | The short Ether reached its take profit at 1,820 (+$2.41). It is closed for the week. " +
      "Do not re-enter; the next call is Monday.");
    expect(texts(longBtc(), [{ high: 119.0, low: 99.0 }], 110.0, at(11))[0]).toBe(
      "target:BTC | The long Bitcoin reached its take profit at 118.00 (+$4.84). It is closed for the week. " +
      "Do not re-enter; the next call is Monday.");
    const bare = { ...WEEK, coins: [] };
    expect(texts(longBtc({ target_price: null }), [{ high: 111.0, low: 99.0 }], 110.0, at(11), bare)[0]).toBe(
      "profit:BTC | The long Bitcoin is up 10.0% (+$2.68), more than a normal week. Hold to Monday. " +
      "There is no take profit on longs, because in testing every nearer target made longs worse: rises tend to keep going.");
    expect(texts(shortEth({ target_price: null }), [{ high: 2050.0, low: 1780.0 }], 1800.0, at(11), bare)[0]).toBe(
      "profit:ETH | The short Ether is up 10.0% (+$2.68), more than a normal week. Hold to Monday. " +
      "No take profit is set for this side.");
    // with a take profit still ahead, a short in profit is simply on track
    expect(texts(shortEth(), [{ high: 2050.0, low: 1780.0 }], 1800.0, at(11))[0]).toBe(
      "hold:ETH | The short Ether is up 10.0% (+$2.68). No action. Take profit at 1,500, 16.7% away. Stop at 2,180.");
  });

  it("too small to place, with the amounts rounded as Python rounds them", () => {
    const small = [
      { coin: "SOL", name: "Solana", side: "short" as const, leverage: 1, position_usd: 4.5, min_weekly_amount: 40.0 },
      { coin: "ETH", name: "Ether", side: "short" as const, leverage: 3, position_usd: 7.5,
        min_weekly_amount: 13.333333333333334 },
      { coin: "BTC", name: "Bitcoin", side: "long" as const, leverage: 3, position_usd: 9.375, min_weekly_amount: 12.5 },
    ];
    const lines = terminal.advice([], { ...WEEK, coins: [] }, at(11), small)
      .map((i) => `${i.level} ${i.key} | ${i.title} | ${i.text}`);
    expect(lines).toEqual([
      "warn small:SOL | Solana: too small to place | The call is short Solana at 1x, but your share makes a $4.50 " +
        "position and the exchange's smallest order is $10. Set the weekly amount to at least $40, or skip Solana.",
      "warn small:ETH | Ether: too small to place | The call is short Ether at 3x, but your share makes a $7.50 " +
        "position and the exchange's smallest order is $10. Set the weekly amount to at least $13, or skip Ether.",
      "warn small:BTC | Bitcoin: too small to place | The call is long Bitcoin at 3x, but your share makes a $9.38 " +
        "position and the exchange's smallest order is $10. Set the weekly amount to at least $12, or skip Bitcoin.",
    ]);
  });
});

describe("recommendation", () => {
  it("called, skipped and uncovered markets", () => {
    const called = terminal.recommendation("ETH", { trend: "down" }, WEEK);
    expect([called.verdict, called.strength, called.of]).toEqual(["SHORT", 3, 3]);
    expect(called.lean).toBe(-1.0);
    const skipped = terminal.recommendation("BTC", { trend: "up" }, WEEK);
    expect(skipped.verdict).toBe("STAY OUT");
    expect(skipped.lean).toBeCloseTo(-1 / 3, 7);
    const other = terminal.recommendation("xyz:NVDA", { trend: "up" }, WEEK);
    expect(other.verdict).toBe("NO CALL");
    expect(other.detail).toContain("No rule tested has an edge");
  });
});

describe("chart", () => {
  const NOW = Date.UTC(2026, 10, 15, 21, 30);
  let up = makeUpstream(NOW);

  beforeEach(() => {
    up = makeUpstream(NOW);
    configure({ fetch: up.fetch, now: () => up.now, storage: null, weightPerMinute: Infinity });
  });

  it("averages line up with the candles", async () => {
    up.fixed.set("ETH:1d", candleRows(NOW, "1d", Array.from({ length: 300 }, (_, i) =>
      [100 + i, 101 + i, 99 + i, 100 + i] as [number, number, number, number])));
    const out = await terminal.chart("ETH", "1d");
    expect(out.candles.length).toBe(terminal.CHART_BARS["1d"]);
    const fast = out.emas["10d"] as number[];
    const slow = out.emas["40d"] as number[];
    expect(fast.length).toBe(out.candles.length);
    expect(slow.length).toBe(out.candles.length);
    expect(slow[slow.length - 1]).toBeLessThan(fast[fast.length - 1]);     // rising prices: fast above slow
    expect(fast[fast.length - 1]).toBeLessThan(out.candles[out.candles.length - 1].c);
  });

  it("short history pads instead of failing", async () => {
    up.fixed.set("ETH:1d", candleRows(NOW, "1d", Array.from({ length: 30 }, () =>
      [1.0, 1.0, 1.0, 1.0] as [number, number, number, number])));
    const out = await terminal.chart("ETH", "1d");
    expect(out.candles.length).toBe(30);
    expect(out.emas).not.toHaveProperty("40d");
    expect((out.emas["10d"] as number[]).length).toBe(30);
  });

  it("refreshes a chart with only the newest candles, and gives what a full request would", async () => {
    const first = await getChart("ETH", "4h");
    const full = up.candleRequests("4h").length;
    const rowsAfterFirst = up.rowsReturned;
    up.now += 5 * 60 * 1000;                                   // five minutes on: the forming candle has moved
    const again = await getChart("ETH", "4h");
    expect(up.candleRequests("4h").length).toBe(full + 1);
    expect(up.rowsReturned - rowsAfterFirst).toBeLessThanOrEqual(3);       // not the 960 of the first request
    expect(again.candles.length).toBe(first.candles.length);
    expect(again.candles[again.candles.length - 1].c).not.toBe(first.candles[first.candles.length - 1].c);
    // A cold start at the same moment downloads everything and must agree exactly.
    configure({});
    expect(await getChart("ETH", "4h")).toEqual(again);
    // Within twenty seconds nothing is asked at all.
    const before = up.sent.length;
    up.now += 5000;
    await terminal.chart("ETH", "4h");
    expect(up.sent.length).toBe(before);
  });
});

describe("what the server used to refuse", () => {
  const NOW = Date.UTC(2026, 10, 15, 21, 30);

  beforeEach(() => {
    const up = makeUpstream(NOW);
    configure({ fetch: up.fetch, now: () => up.now, storage: null, weightPerMinute: Infinity });
  });

  it("a weekly amount that is not a positive number", async () => {
    await expect(getTerminal(0)).rejects.toThrow("Enter the weekly amount in USD.");
    await expect(getTerminal(0)).rejects.toBeInstanceOf(DataError);
  });

  it("an unknown chart interval or market", async () => {
    await expect(getChart("ETH", "5m" as "1h")).rejects.toThrow("Choose 1h, 4h or 1d.");
    await expect(getChart("NOPE", "4h")).rejects.toThrow("That market is not available.");
    expect((await getChart("ETH", "1h")).interval).toBe("1h");
  });
});
