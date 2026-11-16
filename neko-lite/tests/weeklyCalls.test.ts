// The week's mood and the weekly calls. Port of CryptoMoodTests and
// WeeklyCallsTests in service/server/tests/test_desk.py, with the same inputs
// and numbers. (The one-line summary for the daily email is not part of this app.)

import { beforeEach, describe, expect, it } from "vitest";

import { DataError, configure, getScoreboard, getWeek } from "../src/core/api";
import * as cryptoMood from "../src/core/cryptoMood";
import { isoDay, parseDay } from "../src/core/dates";
import type { CallsConfig, MoodReading, Side, SideConfig } from "../src/core/types";
import * as wc from "../src/core/weeklyCalls";
import { makeUpstream, priceAt } from "./helpers/upstream";

const day = parseDay;

describe("crypto mood", () => {
  it("rule directions", () => {
    expect(cryptoMood.ruleDirection(10)).toEqual({ with_mood: "short", extreme: "short" });
    expect(cryptoMood.ruleDirection(49)).toEqual({ with_mood: "short", extreme: null });
    expect(cryptoMood.ruleDirection(50)).toEqual({ with_mood: "long", extreme: null });
    expect(cryptoMood.ruleDirection(80)).toEqual({ with_mood: "long", extreme: "long" });
  });

  it("week uses Sunday's reading and carries the record", () => {
    const readings = [  // newest first; 2026-11-15 is a Sunday
      { day: day("2026-11-17"), value: 30, label: "Fear" },
      { day: day("2026-11-16"), value: 20, label: "Extreme Fear" },
      { day: day("2026-11-15"), value: 60, label: "Greed" },
      { day: day("2026-11-14"), value: 55, label: "Greed" },
      { day: day("2026-11-08"), value: 40, label: "Fear" },
    ];
    const week = cryptoMood.weekFrom(readings);
    expect(week.week_reading.value).toBe(60);
    expect(week.today.value).toBe(30);
    expect(week.rules.with_mood).toBe("long");
    expect(week.change_from_last_week).toBe(20);
    const ev = week.evidence as NonNullable<typeof week.evidence>;
    const withMood = ev.with_mood as NonNullable<typeof ev.with_mood>;
    const holding = ev.holding as NonNullable<typeof ev.holding>;
    expect(withMood.choose.accuracy).toBeGreaterThan(0.5);
    expect(withMood.check.avg_pct).toBeGreaterThan(holding.check.avg_pct);
  });

  it("on a Sunday the week is still last Sunday's", () => {
    const readings = [{ day: day("2026-11-15"), value: 10, label: "Extreme Fear" },   // today, a Sunday
                      { day: day("2026-11-14"), value: 12, label: "Extreme Fear" },
                      { day: day("2026-11-08"), value: 28, label: "Fear" },
                      { day: day("2026-11-01"), value: 45, label: "Fear" }];
    const week = cryptoMood.weekFrom(readings);
    expect(week.week_reading.date).toBe("2026-11-08");
    expect(week.today.value).toBe(10);
    expect(week.change_from_last_week).toBe(-17);
  });
});

// the shape research/scripts/crypto_long_short.py writes
const LEVELS = [1, 2, 3, 5, 7, 10, 20, 30];
const coinConfig = (coin: string): Record<Side, SideConfig> => ({
  long: {
    allowed: true, suggested_leverage: 3,
    first_half: { right: 0.6, avg_pct: 3.0, coin_weeks: 60 },
    second_half: { right: 0.55, avg_pct: 1.5, coin_weeks: 60 },
    by_leverage: Object.fromEntries(LEVELS.map((lev) =>
      [`${lev}x`, { ahead: 0.55, liquidated: lev >= 20 ? 0.7 : 0.0, avg_per_week: 0.05, weeks: 120 }])),
  },
  short: {
    allowed: coin !== "BTC", suggested_leverage: 3,
    first_half: { right: 0.52, avg_pct: 0.7, coin_weeks: 60 },
    second_half: { right: 0.48, avg_pct: 0.0, coin_weeks: 45 },
    by_leverage: Object.fromEntries(LEVELS.map((lev) =>
      [`${lev}x`, { ahead: 0.5, liquidated: 0.0, avg_per_week: 0.01, weeks: 100 }])),
  },
});
const CONFIG: CallsConfig = { BTC: coinConfig("BTC"), ETH: coinConfig("ETH") };
const EXITS: wc.ExitRule = { stop: 1.0, targets: { long: null, short: 1.0 }, take_profit: {},
                             with: null, holding: null, by_call: {} };
const NO_STOP: wc.ExitRule = { ...EXITS, stop: null };

const closes = (now: number, fourWeeksAgo: number): wc.SundayClose[] => {
  const start = day("2026-11-08");
  return [now, 1, 1, 1, fourWeeksAgo, 1].map((close, i) => ({ day: start - 7 * i, close }));
};

describe("weekly calls", () => {
  it("signal needs mood and four weeks to agree", () => {
    expect(wc.signal(60, 110, 100)).toBe("long");
    expect(wc.signal(40, 90, 100)).toBe("short");
    expect(wc.signal(60, 90, 100)).toBeNull();               // greedy but falling
    expect(wc.signal(40, 110, 100)).toBeNull();              // fearful but rising
    expect(wc.signal(50, 110, 100)).toBe("long");
    expect(wc.signal(49, 90, 100)).toBe("short");
  });

  it("liquidation distance", () => {
    expect(wc.liquidationDistance(1, 40)).toBe(1.0);
    expect(wc.liquidationDistance(3, 25)).toBeCloseTo(1 / 3 - 1 / 50, 7);
    expect(wc.liquidationDistance(30, 40)).toBeCloseTo(1 / 30 - 1 / 80, 7);
  });

  it("real evidence file has the expected shape", () => {
    const config = wc.callsConfig();
    for (const coin of Object.keys(config)) {
      for (const side of ["long", "short"] as const) {
        const c = config[coin][side];
        expect(c).toHaveProperty("allowed");
        if (c.allowed) {        // a called side always has a tested, rarely-liquidated leverage
          expect(c.by_leverage).toHaveProperty(`${c.suggested_leverage}x`);
          expect(c.by_leverage[`${c.suggested_leverage}x`].liquidated).toBeLessThanOrEqual(0.02);
        }
      }
    }
    expect(config.BTC.long.allowed).toBe(true);
    expect(config.BTC.short.allowed).toBe(false);
    expect(new Set(Object.keys(config))).toEqual(new Set(["BTC", "ETH", "SOL", "SUI"]));
  });

  it("preview uses today's mood and live price", () => {
    const mood = { today: { value: 10, label: "Extreme Fear", date: "2026-11-15" } };
    const live = new Map([["BTC", { price: 90.0 }], ["ETH", { price: 90.0 }]]);
    const rows = closes(1, 1);
    rows[3].close = 100.0;                                   // four weeks before the coming week's end
    const out = wc.preview(CONFIG, mood, live, { BTC: rows, ETH: rows }) as NonNullable<ReturnType<typeof wc.preview>>;
    const by = Object.fromEntries(out.coins.map((c) => [c.coin, c]));
    expect([by.ETH.side, by.ETH.leverage]).toEqual(["short", 3]);
    expect([by.BTC.side, by.BTC.skipped]).toEqual([null, true]);     // short Bitcoin is not called
    expect(by.ETH.four_week_change_pct).toBeCloseTo(-10.0, 7);
    expect(wc.preview(CONFIG, {}, live, { BTC: rows, ETH: rows })).toBeNull();
  });

  const week = (moodValue: number, label: string, closesByCoin: Record<string, wc.SundayClose[]>,
                normal = 0.05, mondayOpen: number | null = null, formingOpen: number | null = null) => {
    const mood: { week_reading: MoodReading } = { week_reading: { value: moodValue, label, date: "2026-11-08" } };
    const live = new Map([["BTC", { price: 100.0, max_leverage: 40 }], ["ETH", { price: 50.0, max_leverage: 25 }]]);
    const candles: wc.Days = new Map();
    if (mondayOpen) candles.set(day("2026-11-09"), { open: mondayOpen } as wc.Day);
    return wc.buildWeek({ config: CONFIG, exits: EXITS, mood, live, closes: closesByCoin,
                          candles: { BTC: candles, ETH: candles },
                          weekOpens: { BTC: formingOpen, ETH: formingOpen }, normalWeek: () => normal });
  };

  it("fearful and falling shorts Ether but skips Bitcoin", () => {
    const down = closes(90, 100);
    const out = week(28, "Fear", { BTC: down, ETH: down });
    expect(out.calls).toEqual([{ coin: "ETH", side: "short", leverage: 3 }]);
    const [btc, eth] = out.coins;
    expect(btc.skipped).toBe(true);
    expect(btc.side).toBeNull();
    expect(btc.signal).toBe("short");
    // a short is liquidated above the entry price, and its stop sits above too
    expect(eth.liquidation_price).toBeCloseTo(50.0 * (1 + 1 / 3 - 1 / 50), 7);
    expect(eth.stop_price).toBeCloseTo(50.0 * 1.05, 7);          // one normal week (5%)
    expect(eth.target_price).toBeCloseTo(50.0 * 0.95, 7);        // shorts take profit one normal week away
    expect(eth.stake_made_at_target).toBeCloseTo(3 * (0.05 - 2 * wc.FEE_PER_SIDE), 7);
    expect(eth.stake_lost_at_stop).toBeCloseTo(3 * (0.05 + 2 * wc.FEE_PER_SIDE), 7);
    // two coins share the weekly amount, so a 3x call needs 2 * 10 / 3 a week to reach a 10 USD order
    expect(eth.min_weekly_amount).toBeCloseTo(20 / 3, 7);
    expect(out.min_order_usd).toBe(10.0);
    expect(out.exits.targets).toEqual({ long: null, short: 1.0 });
    expect(eth.stop_price as number).toBeLessThan(eth.liquidation_price as number);
    expect([out.week_of, out.next_call]).toEqual(["2026-11-09", "2026-11-16"]);
  });

  it("levels use Monday's open once it exists", () => {
    const down = closes(90, 100);
    const eth = week(28, "Fear", { BTC: down, ETH: down }, 0.05, 60.0).coins[1];
    expect(eth.entry).toBe(60.0);
    expect(eth.stop_price).toBeCloseTo(63.0, 7);
  });

  it("entry is fixed at the week's open while Monday is still forming", () => {
    const down = closes(90, 100);
    // Monday has started but not closed: its candle is not among the finished ones yet
    let out = week(28, "Fear", { BTC: down, ETH: down }, 0.05, null, 58.0);
    const eth = out.coins[1];
    expect(eth.entry).toBe(58.0);                                // not the live price of 50
    expect(eth.stop_price).toBeCloseTo(58.0 * 1.05, 7);
    expect(eth.target_price).toBeCloseTo(58.0 * 0.95, 7);
    // once Monday has closed, the finished candle is used
    out = week(28, "Fear", { BTC: down, ETH: down }, 0.05, 60.0, 58.0);
    expect(out.coins[1].entry).toBe(60.0);
  });

  it("greedy and rising goes long and mixed stays out", () => {
    const up = closes(110, 100);
    const down = closes(90, 100);
    let out = week(70, "Greed", { BTC: up, ETH: down });
    expect(out.calls).toEqual([{ coin: "BTC", side: "long", leverage: 3 }]);
    expect(out.coins[0].liquidation_price as number).toBeLessThan(100.0);   // a long is liquidated below
    expect(out.coins[0].stop_price).toBeCloseTo(95.0, 7);                   // and stopped below
    expect(out.coins[0]).not.toHaveProperty("target_price");                // longs have no take profit
    expect(out.coins[1].skipped).toBe(false);                               // no signal at all
    out = week(70, "Greed", { BTC: down, ETH: down });
    expect(out.calls).toEqual([]);
  });

  /** Daily candles for one week opening at 100: (high, low, close) moves in percent. */
  const weekCandles = (monday: number, moves: Array<[number, number, number]>, before = 100.0,
                       monthAgo = 90.0): wc.Days => {
    const flat = (v: number) => ({ open: v, high: v, low: v, close: v });
    const candles: wc.Days = new Map([[monday - 1, flat(before)], [monday - 29, flat(monthAgo)]]);
    moves.forEach(([high, low, close], i) => {
      candles.set(monday + i, { open: 100.0, high: 100.0 * (1 + high / 100), low: 100.0 * (1 + low / 100),
                                close: 100.0 * (1 + close / 100) });
    });
    return candles;
  };
  const repeat = (move: [number, number, number], times: number) => new Array<[number, number, number]>(times).fill(move);
  const fixedNormal = () => 0.05;

  it("stop loss ends a week early", () => {
    const monday = day("2026-09-14");
    // dips 6% on day two, then recovers to end +4%
    const dip = weekCandles(monday, [[0.5, -0.5, 0.2], [0.5, -6.0, -1.0], ...repeat([0.5, -0.5, 0.2], 4), [4.5, -0.5, 4.0]]);
    const stopped = wc.weekResult(dip, monday, 60, 9.0, wc.SUGGESTED, 25, CONFIG.ETH, EXITS, fixedNormal);
    expect(stopped.stopped).toBe(true);
    expect(stopped.wiped).toBe(false);
    // loses the stop distance (5%) at 3x, with fees and two days of funding
    expect(stopped.result).toBeCloseTo(9.0 * 3 * (-0.05 - 2 * wc.FEE_PER_SIDE - 2 * wc.FUNDING_PER_DAY), 7);
    const held = wc.weekResult(dip, monday, 60, 9.0, wc.SUGGESTED, 25, CONFIG.ETH, NO_STOP, fixedNormal);
    expect(held.stopped).toBe(false);
    expect(held.result as number).toBeGreaterThan(0);
  });

  it("take profit closes a short but never a long", () => {
    const monday = day("2026-09-14");
    const eth = CONFIG.ETH;
    // falls 6% on day three, then bounces to end only 1% down: the short banked 5% at its target
    const bounce = weekCandles(monday, [...repeat([0.5, -0.5, -0.2], 2), [0.5, -6.0, -3.0], ...repeat([0.5, -0.5, -1.0], 4)],
                               100, 110);
    const short = wc.weekResult(bounce, monday, 40, 9.0, wc.SUGGESTED, 25, eth, EXITS, fixedNormal);
    expect(short.took_profit).toBe(true);
    expect(short.result).toBeCloseTo(9.0 * 3 * (0.05 - 2 * wc.FEE_PER_SIDE), 7);
    // a day that touches both the stop and the target counts as the stop
    const both = weekCandles(monday, [[5.5, -6.0, -3.0], ...repeat([0.5, -0.5, -1.0], 6)], 100, 110);
    const hit = wc.weekResult(both, monday, 40, 9.0, wc.SUGGESTED, 25, eth, EXITS, fixedNormal);
    expect([hit.stopped, hit.took_profit]).toEqual([true, false]);
    // a long that runs 8% keeps all of it: there is no target on longs
    const run = weekCandles(monday, [...repeat([0.5, -0.5, 0.2], 6), [8.5, -0.5, 8.0]]);
    const long = wc.weekResult(run, monday, 60, 9.0, wc.SUGGESTED, 25, eth, EXITS, fixedNormal);
    expect(long.took_profit).toBe(false);
    expect(long.result).toBeCloseTo(9.0 * 3 * (0.08 - 2 * wc.FEE_PER_SIDE - 7 * wc.FUNDING_PER_DAY), 7);
  });

  it("a position under the exchange minimum is not traded", () => {
    const monday = day("2026-09-14");
    const rise = weekCandles(monday, [...repeat([0.5, -0.5, 0.2], 6), [4.5, -0.5, 4.0]]);
    const small = wc.weekResult(rise, monday, 60, 3.0, wc.SUGGESTED, 25, CONFIG.ETH, EXITS, fixedNormal);   // 3 USD at 3x is 9 USD
    expect([small.side, small.too_small, small.result]).toEqual([null, true, 0.0]);
    const enough = wc.weekResult(rise, monday, 60, 3.4, wc.SUGGESTED, 25, CONFIG.ETH, EXITS, fixedNormal);  // 10.2 USD
    expect(enough.side).toBe("long");
  });

  it("week result long, short and liquidation", () => {
    const monday = day("2026-09-14");
    const rise = weekCandles(monday, [...repeat([0.5, -0.5, 0.2], 6), [4.5, -0.5, 4.0]]);             // ends +4%
    const fall = weekCandles(monday, [...repeat([0.5, -0.5, -0.2], 6), [0.5, -4.5, -4.0]], 100, 110);  // ends -4%
    const eth = CONFIG.ETH;
    // long at the suggested 3x: three times the move, less fees and funding
    const long = wc.weekResult(rise, monday, 60, 9.0, wc.SUGGESTED, 25, eth, NO_STOP);
    expect([long.side, long.leverage_used]).toEqual(["long", 3.0]);
    expect(long.result).toBeCloseTo(9.0 * 3 * (0.04 - 2 * wc.FEE_PER_SIDE - 7 * wc.FUNDING_PER_DAY), 7);
    // short profits from the fall and pays no funding
    const short = wc.weekResult(fall, monday, 40, 9.0, wc.SUGGESTED, 25, eth, NO_STOP);
    expect(short.side).toBe("short");
    expect(short.result).toBeCloseTo(9.0 * 3 * (0.04 - 2 * wc.FEE_PER_SIDE), 7);
    // shorting Bitcoin is not called: no trade, flagged as skipped
    const skipped = wc.weekResult(fall, monday, 40, 9.0, wc.SUGGESTED, 40, CONFIG.BTC, NO_STOP);
    expect([skipped.side, skipped.skipped, skipped.result]).toEqual([null, true, 0.0]);
    // mood and price disagree: no trade, not skipped
    const none = wc.weekResult(fall, monday, 60, 9.0, wc.SUGGESTED, 25, eth, NO_STOP);
    expect([none.side, none.skipped]).toEqual([null, false]);
    // a 3% spike against a 30x short liquidates it even though the week ends in its favour
    const spike = weekCandles(monday, [[3.0, -0.5, -0.2], ...repeat([0.5, -0.5, -0.2], 5), [0.5, -4.5, -4.0]], 100, 110);
    const wiped = wc.weekResult(spike, monday, 40, 9.0, 30, 40, CONFIG.ETH, NO_STOP);
    expect(wiped.wiped).toBe(true);
    expect(wiped.result).toBe(-9.0);
    expect(wc.weekResult(spike, monday, 40, 9.0, 3, 40, CONFIG.ETH, NO_STOP).wiped).toBe(false);
    // leverage above the exchange's limit is capped
    expect(wc.weekResult(rise, monday, 60, 9.0, 30, 25, eth, NO_STOP).leverage_used).toBe(25);
    // missing days: the week is not scored
    const shortWeek = new Map(rise);
    shortWeek.delete(monday);
    expect(wc.weekResult(shortWeek, monday, 60, 9.0, 1, 40, eth, NO_STOP).known).toBe(false);
  });

  it("scoreboard totals", () => {
    const mondays = [day("2026-09-14"), day("2026-09-07")];
    // The older week's last day is also the newer week's "Sunday before", so
    // build the newer week first and let the older week's real days win.
    const candles: wc.Days = new Map([
      ...weekCandles(mondays[0], [...repeat([0.5, -0.5, 0.2], 6), [4.5, -0.5, 4.0]]),     // +4%
      ...weekCandles(mondays[1], [...repeat([0.5, -0.5, 0.2], 6), [0.5, -3.5, -3.0]]),    // -3%
    ]);
    const moods = new Map(mondays.map((m) => [m - 1, 60]));
    const live = new Map([["BTC", { max_leverage: 40 }], ["ETH", { max_leverage: 25 }]]);
    const inputs: wc.ScoreboardInputs = { config: CONFIG, rule: NO_STOP, moods, live,
                                          candles: { BTC: candles, ETH: candles } };
    const board = wc.buildScoreboard(inputs, 20, 1, 2);
    const high = wc.buildScoreboard(inputs, 18, 30, 2);
    const suggested = wc.buildScoreboard(inputs, 18, wc.SUGGESTED, 2);
    const tiny = wc.buildScoreboard(inputs, 18, 1, 2);       // 9 USD a coin at 1x: under the exchange minimum
    expect(board.rows.map((r) => r.week_of)).toEqual(["2026-09-14", "2026-09-07"]);
    expect([board.ahead, board.behind, board.no_trade]).toEqual([1, 1, 0]);
    expect(board.rows[1].running_total).toBeCloseTo(board.rows[1].result, 7);
    expect(board.rows[0].running_total).toBeCloseTo(board.total, 7);
    expect(board.total).toBeCloseTo(20 * (0.04 - 0.03 - 4 * wc.FEE_PER_SIDE), 7);
    expect([tiny.total, tiny.no_trade]).toEqual([0.0, 2]);
    expect(tiny.rows[0].too_small).toEqual(["BTC", "ETH"]);
    expect(board.rows[0].too_small).toEqual([]);
    expect(board.min_order_usd).toBe(10.0);
    expect(board.rows[0].calls[0]).toEqual({ coin: "BTC", side: "long", leverage: 1.0 });
    expect(high.capped).toEqual({ ETH: 25 });
    expect(new Set(high.expect.filter((e) => e.side === "long").map((e) => `${e.coin} ${e.leverage}`)))
      .toEqual(new Set(["BTC 30", "ETH 25"]));
    expect(suggested.rows.every((r) => r.calls.every((c) => c.leverage === 3.0))).toBe(true);
  });

  it("mismatched weeks are refused", () => {
    const mood = { week_reading: { value: 70, label: "Greed", date: "2026-11-15" } };
    const live = new Map([["BTC", { price: 1.0, max_leverage: 40 }], ["ETH", { price: 1.0, max_leverage: 25 }]]);
    const rows = closes(110, 100);
    const build = () => wc.buildWeek({ config: CONFIG, exits: EXITS, mood, live, closes: { BTC: rows, ETH: rows },
                                       candles: {} });
    expect(build).toThrow(DataError);
    expect(build).toThrow("This week's mood and prices are out of step; try again shortly.");
  });

  it("normal week is the spread of recent weekly moves", () => {
    const sunday = day("2026-11-08");
    let price = 100.0;
    const candles: wc.Days = new Map();
    for (let k = wc.VOL_WEEKS; k >= 0; k -= 1) {              // alternate +2% and -2% weeks
      candles.set(sunday - 7 * k, { close: price } as wc.Day);
      price *= k % 2 ? 1.02 : 0.98;
    }
    const normal = wc.normalWeek(candles, sunday) as number;
    expect(Math.abs(normal - 0.02)).toBeLessThanOrEqual(0.002);
    candles.delete(sunday - 7 * 5);
    expect(wc.normalWeek(candles, sunday)).toBeNull();        // a missing week: no estimate
  });

  it("real exit rule file", () => {
    const rule = wc.exitRule();
    expect(rule.stop).toBe(1.0);
    const kept = rule.with as NonNullable<typeof rule.with>;
    const holding = rule.holding as NonNullable<typeof rule.holding>;
    expect(kept.worst).toBeGreaterThan(holding.worst);        // the stop caps the worst week
    expect(kept.avg).toBeGreaterThan(0);
  });

  it("real take profit file", () => {
    const rule = wc.exitRule();
    // shorts take profit one normal week away; no target was as good as the stop alone for longs
    expect(rule.targets).toEqual({ long: null, short: 1.0 });
    const short = rule.take_profit.short as NonNullable<typeof rule.take_profit.short>;
    const long = rule.take_profit.long as NonNullable<typeof rule.take_profit.long>;
    expect(short.with.avg).toBeGreaterThanOrEqual(short.stop_only.avg);
    expect(short.with.ahead).toBeGreaterThan(short.stop_only.ahead);
    expect(short.reached as number).toBeGreaterThan(0.2);     // a target has to be reachable
    expect((long.at_one_normal_week as NonNullable<typeof long.at_one_normal_week>).avg).toBeLessThan(long.stop_only.avg);
    expect(long.plans_as_good).toBe(0);
    expect(long.reached).toBeNull();
  });

  it("a missing take profit table means no targets", () => {
    const rule = wc.exitRule(wc.takeProfitRule(null));
    expect(rule.targets).toEqual({ long: null, short: null });
    expect(rule.stop).toBe(1.0);
  });
});

describe("the week from the data sources", () => {
  // A Wednesday: the week's call was fixed on Monday the 9th from Sunday the 8th's mood.
  const NOW = Date.UTC(2026, 10, 11, 9, 30);
  let up = makeUpstream(NOW);

  beforeEach(() => {
    up = makeUpstream(NOW);
    configure({ fetch: up.fetch, now: () => up.now, storage: null, weightPerMinute: Infinity });
  });

  it("uses the Sunday before the week for mood and prices, and Monday's open as the entry", async () => {
    const week = await getWeek();
    expect(week.mood.date).toBe("2026-11-08");
    expect(week.mood.value).toBe(up.mood(day("2026-11-08")));
    expect([week.week_of, week.next_call]).toEqual(["2026-11-09", "2026-11-16"]);
    expect(week.coins.map((c) => c.coin)).toEqual(["BTC", "ETH", "SOL", "SUI"]);
    expect((week.preview as NonNullable<typeof week.preview>).mood.date).toBe(isoDay(day("2026-11-11")));
    for (const coin of week.coins.filter((c) => c.side)) {
      // Monday's candle has closed by Wednesday, so its open is the entry (not the live price).
      expect(coin.entry).not.toBe(coin.price);
      const sign = coin.side === "long" ? 1 : -1;
      expect(((coin.stop_price as number) - (coin.entry as number)) * sign).toBeLessThan(0);
    }
  });

  it("asks for each coin's daily candles once, and again only after a day has closed", async () => {
    await getWeek();
    await getScoreboard(18, 0);
    expect(up.candleRequests("1d").length).toBe(4);           // one per coin serves the call and the scoreboard
    expect(up.sent.filter((s) => s.url.includes("alternative.me")).length).toBe(1);
    up.now += 2 * 3600 * 1000;                                // later the same day
    await getWeek();
    expect(up.candleRequests("1d").length).toBe(4);
    up.now += 24 * 3600 * 1000;                               // a day has closed
    const rows = up.rowsReturned;
    await getWeek();
    expect(up.candleRequests("1d").length).toBe(8);
    expect(up.rowsReturned - rows).toBeLessThanOrEqual(4 * 3);    // only the newest days, not 330 again
  });

  it("refuses a leverage that is not on the list, or a bad amount", async () => {
    await expect(getScoreboard(18, 4)).rejects.toThrow("Choose one of the listed leverage levels.");
    await expect(getScoreboard(0, 5)).rejects.toThrow("Enter the weekly amount in USD.");
    const board = await getScoreboard(18, 5);
    expect(board.leverage).toBe(5);
    expect(board.rows.length).toBeGreaterThan(0);
    expect(board.rows.every((r) => r.week_of < "2026-11-09")).toBe(true);    // finished weeks only
  });

  it("on Monday, waits for the mood index to publish the new day, checking every minute", async () => {
    const monday = Date.UTC(2026, 10, 16);                    // 00:00 UTC, the moment the week turns
    up.now = monday - 30 * 60 * 1000;                         // Sunday 23:30: still last week's call
    up.moodDelayMs = 10 * 60 * 1000;                          // the index publishes ten minutes after midnight
    const sunday = await getWeek();
    expect(sunday.week_of).toBe("2026-11-09");
    const moodRequests = () => up.sent.filter((s) => s.url.includes("alternative.me")).length;

    up.now = monday + 2 * 60 * 1000;                          // 00:02: Sunday's candle has closed, the index has not moved
    await expect(getWeek()).rejects.toThrow("This week's mood and prices are out of step; try again shortly.");
    up.now += 5 * 1000;
    await expect(getWeek()).rejects.toThrow("out of step");   // looks again at once: the reading held is an hour-old kind
    const asked = moodRequests();
    expect(asked).toBe(2);
    up.now += 20 * 1000;
    await expect(getWeek()).rejects.toThrow("out of step");
    expect(moodRequests()).toBe(asked);                       // then not more than once a minute
    up.now = monday + 11 * 60 * 1000;                         // 00:11: the new reading is out
    const week = await getWeek();
    expect([week.week_of, week.next_call, week.mood.date]).toEqual(["2026-11-16", "2026-11-23", "2026-11-15"]);
    // and from then on the index is asked for hourly again
    const settledAt = moodRequests();
    up.now += 30 * 60 * 1000;
    await getWeek();
    expect(moodRequests()).toBe(settledAt);
  });

  it("week open reads Monday's forming candle", async () => {
    up.now = Date.UTC(2026, 10, 16, 10, 0);
    const at = (d: number) => Date.UTC(2026, 10, d);
    const row = (d: number, o: string) => ({ t: at(d), o, h: o, l: o, c: o, v: "1" });
    up.fixed.set("ETH:1d", [row(15, "1800"), row(16, "1834.4")]);
    expect(await wc.weekOpen("ETH", day("2026-11-16"))).toBe(1834.4);
    expect(await wc.weekOpen("ETH", day("2026-11-23"))).toBeNull();          // that Monday has not started
    // one request for the last nine days, as the Python reference sends it, shared by both questions
    expect(up.candleRequests("1d").map((s) => s.payload?.req)).toEqual([
      { coin: "ETH", interval: "1d", startTime: up.now - 9 * 24 * 3600 * 1000, endTime: up.now }]);

    configure({ fetch: up.fetch, now: () => up.now, storage: null, weightPerMinute: Infinity });
    up.failWith = (sent) => (sent.payload?.type === "candleSnapshot" ? 500 : null);
    expect(await wc.weekOpen("ETH", day("2026-11-16"))).toBeNull();          // no answer: the live price stands in
  });

  describe("on a Monday", () => {
    const MONDAY = Date.UTC(2026, 10, 16);
    const calls = (week: Awaited<ReturnType<typeof getWeek>>) => week.coins.filter((c) => c.side);

    beforeEach(() => {
      up.mood = () => 20;                                       // fearful, so the falling coin is shorted
    });

    it("the entry is Monday's open and stays put while the price moves, with no request of its own", async () => {
      up.now = MONDAY + 10 * 3600 * 1000;
      const first = await getWeek();
      expect(first.week_of).toBe("2026-11-16");
      expect(calls(first).map((c) => c.coin)).toEqual(["ETH"]);
      const eth = calls(first)[0];
      expect(eth.entry).toBe(Number(priceAt("ETH", MONDAY).toPrecision(8)));    // the open of the candle still forming
      expect(eth.entry).not.toBe(eth.price);
      expect(eth.stop_price as number).toBeGreaterThan(eth.entry as number);
      expect(eth.target_price as number).toBeLessThan(eth.entry as number);

      up.now += 30 * 1000;                                      // half a minute on: the price has moved
      const later = calls(await getWeek())[0];
      expect(later.price).not.toBe(eth.price);
      expect([later.entry, later.stop_price, later.target_price, later.liquidation_price])
        .toEqual([eth.entry, eth.stop_price, eth.target_price, eth.liquidation_price]);
      // the daily candles already fetched carried Monday's open: nothing else was asked for
      expect(up.candleRequests("1d").length).toBe(4);

      up.now = MONDAY + 34 * 3600 * 1000;                       // Tuesday: Monday has finished, with the same open
      expect(calls(await getWeek())[0].entry).toBe(eth.entry);
    });

    it("in the first seconds, before the exchange has a candle for the day, the live price stands in", async () => {
      up.barDelayMs = 30 * 1000;
      up.now = MONDAY + 10 * 1000;
      const early = calls(await getWeek())[0];
      expect(early.coin).toBe("ETH");
      expect(early.entry).toBe(early.price);
      // asked the way the Python reference asks, once, for the one coin that is called
      const nineDays = () => up.candleRequests("1d").filter((s) => {
        const req = s.payload?.req;
        return req && req.endTime - req.startTime === 9 * 24 * 3600 * 1000;
      });
      expect(nineDays().map((s) => s.payload?.req?.coin)).toEqual(["ETH"]);

      up.now = MONDAY + 70 * 1000;                              // the daily candles are looked at again a minute in
      const settledWeek = calls(await getWeek())[0];
      expect(settledWeek.entry).toBe(Number(priceAt("ETH", MONDAY).toPrecision(8)));
      expect(settledWeek.entry).not.toBe(settledWeek.price);
      expect(nineDays().length).toBe(1);
    });
  });

  it("says so in plain words when the mood index cannot be read", async () => {
    up.failWith = (sent) => (sent.url.includes("alternative.me") ? 503 : null);
    await expect(getWeek()).rejects.toThrow("The mood index is unavailable right now (HTTPError).");
  });
});
