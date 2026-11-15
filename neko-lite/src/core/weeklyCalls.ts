// This week's calls on perps: long, short or stay out, with a suggested leverage.
//
// Port of service/server/weekly_calls.py. For someone who does not trade.
// Once a week, per coin:
//
//   long   when the market's mood is greedy (Fear & Greed 50 or more) and the
//          coin rose over the last four weeks
//   short  when the mood is fearful (below 50) and the coin fell over the last
//          four weeks
//   out    otherwise
//
// A call is only made where the research found that side right more often
// than not AND profitable in both halves of the history for that coin. The
// suggested leverage is the highest tested level at which the weekly stake
// was liquidated in at most one week in fifty. Positions are opened on Monday
// (00:00 UTC) and closed the following Monday. A stop one "normal week" from
// entry (the coin's typical weekly move over the last half year) is suggested
// as protection; a far target, three normal weeks away, is optional.

import * as cryptoMood from "./cryptoMood";
import callsTable from "./data/calls.json";
import exitsTable from "./data/exits.json";
import spotTable from "./data/spot_record.json";
import { DAY_MS, SUNDAY, dayOf, isoDay, parseDay, weekday } from "./dates";
import { closedBars, type Bars } from "./history";
import { catalogue, type Catalogue } from "./marketData";
import { pySum } from "./numeric";
import { DataError, runtime } from "./runtime";
import type {
  CallsConfig, CoinRecord, ExitRecord, Expectation, MoodReading, PreviewCoin, ScoreRow, Scoreboard, Side,
  SideConfig, SpotRecord, Week, WeekCoin, WeekPreview,
} from "./types";

export const NAMES: Record<string, string> = { BTC: "Bitcoin", ETH: "Ether", SOL: "Solana", SUI: "Sui" };
export const VOL_WEEKS = 26;                 // the "normal week" looks back half a year
export const SUGGESTED = 0;                  // leverage value meaning "use the suggested level"
export const LEVERAGES = [SUGGESTED, 1, 2, 3, 5, 10, 20, 30] as const;
export const FEE_PER_SIDE = 0.00045;         // exchange fee, share of the position, each way
export const FUNDING_PER_DAY = 0.0003;       // typical cost of holding a leveraged long, per day
const DAILY_DAYS = 330;
const SUNDAY_DAYS = 70;
const OUT_OF_STEP = "This week's mood and prices are out of step; try again shortly.";

export interface Day {
  open: number;
  high: number;
  low: number;
  close: number;
}

/** Completed daily candles by day number. */
export type Days = Map<number, Day>;

export interface SundayClose {
  day: number;
  close: number;
}

export interface ExitRule {
  stop: number | null;
  optional_target: number | null;
  with: ExitRecord | null;
  holding: ExitRecord | null;
  by_call: Record<string, { all: ExitRecord }>;
}

/** Per coin and side: whether the call is made, the suggested leverage, and its record. */
export function callsConfig(): CallsConfig {
  return callsTable as CallsConfig;
}

/** Stop and optional target, in normal weeks, with the tested effect. */
export function exitRule(): ExitRule {
  return exitsTable as ExitRule;
}

/** The long side without leverage, against simply holding. */
export function spotRecord(): SpotRecord | null {
  return spotTable as SpotRecord;
}

/** A coin's typical weekly move (as a fraction) over the half year ending that Sunday. */
export function normalWeek(candles: Days, sunday: number): number | null {
  const prices: number[] = [];
  for (let k = VOL_WEEKS; k >= 0; k -= 1) {
    const candle = candles.get(sunday - 7 * k);
    if (!candle) return null;
    prices.push(candle.close);
  }
  const returns = prices.slice(1).map((price, i) => price / prices[i] - 1);
  const mean = pySum(returns) / returns.length;
  return (pySum(returns.map((r) => (r - mean) ** 2)) / (returns.length - 1)) ** 0.5;
}

/** What the rule says before asking whether that side has earned a call. */
export function signal(moodValue: number, closeNow: number, closeFourWeeksAgo: number): Side | null {
  if (moodValue >= 50 && closeNow > closeFourWeeksAgo) return "long";
  if (moodValue < 50 && closeNow < closeFourWeeksAgo) return "short";
  return null;
}

/** Adverse move, as a fraction of the entry price, at which the exchange closes the position. */
export function liquidationDistance(leverage: number, maxLeverage: number): number {
  if (leverage <= 1) return 1.0;
  return 1.0 / leverage - 1.0 / (2 * maxLeverage);
}

// ---- prices ----------------------------------------------------------------------

const daysOf = new WeakMap<Bars, Days>();

function barsToDays(bars: Bars): Days {
  let days = daysOf.get(bars);
  if (!days) {
    days = new Map();
    for (let i = 0; i < bars.t.length; i += 1) {
      days.set(dayOf(bars.t[i]), { open: bars.open[i], high: bars.high[i], low: bars.low[i], close: bars.close[i] });
    }
    daysOf.set(bars, days);
  }
  return days;
}

/** Completed daily candles by day. */
export async function dailyCandles(coin: string): Promise<Days> {
  return barsToDays(await closedBars(coin, "1d", DAILY_DAYS));
}

/** Closing price of each completed week (the Sunday daily candle), newest first. */
export async function sundayCloses(coin: string): Promise<SundayClose[]> {
  const bars = await closedBars(coin, "1d", DAILY_DAYS);
  // The same ten weeks a request for the last 70 days would return.
  const start = runtime.now() - SUNDAY_DAYS * DAY_MS;
  const out: SundayClose[] = [];
  for (let i = bars.t.length - 1; i >= 0; i -= 1) {
    if (bars.t[i] + DAY_MS <= start) break;
    const day = dayOf(bars.t[i]);
    if (weekday(day) === SUNDAY) out.push({ day, close: bars.close[i] });
  }
  return out;
}

function record(sideConfig: SideConfig, leverage: number | null): CoinRecord {
  const at = leverage ? (sideConfig.by_leverage ?? {})[`${leverage}x`] : undefined;
  const half = (h: SideConfig["first_half"]) => ({ right: h.right, avg_pct: h.avg_pct, coin_weeks: h.coin_weeks });
  return {
    first_half: half(sideConfig.first_half),
    second_half: half(sideConfig.second_half),
    at_leverage: at ? { ahead: at.ahead, liquidated: at.liquidated, avg_per_week: at.avg_per_week, weeks: at.weeks }
                    : null,
  };
}

// ---- this week -------------------------------------------------------------------

export interface WeekInputs {
  config: CallsConfig;
  exits: ExitRule;
  mood: { week_reading: MoodReading; today?: MoodReading };
  live: Catalogue | Map<string, { price?: number; max_leverage?: number }>;
  closes: Record<string, SundayClose[]>;
  candles: Record<string, Days>;
  /** Replaceable so a test can fix the normal week. */
  normalWeek?: (candles: Days, sunday: number) => number | null;
}

/** The week's calls from data already in hand. */
export function buildWeek(inputs: WeekInputs): Week {
  const { config, exits, mood, live } = inputs;
  const normalOf = inputs.normalWeek ?? normalWeek;
  const value = mood.week_reading.value;
  const coins: WeekCoin[] = [];
  for (const coin of Object.keys(config)) {
    const closes = inputs.closes[coin] ?? [];
    if (closes.length < 5) throw new DataError("Not enough weekly prices to decide.");
    if (isoDay(closes[0].day) !== mood.week_reading.date) {
      // Mood and price must describe the same week's end.
      throw new DataError(OUT_OF_STEP);
    }
    const now = closes[0].close;
    const before = closes[4].close;
    const sig = signal(value, now, before);
    const sideConfig = sig ? config[coin][sig] : null;
    const called = Boolean(sig && sideConfig && sideConfig.allowed);
    const leverage = called && sideConfig ? sideConfig.suggested_leverage : null;
    const market = live.get(coin);
    const price = market?.price ?? null;
    const maxLev = market?.max_leverage ?? 20;
    const row: WeekCoin = {
      coin, name: NAMES[coin] ?? coin, mood_greedy: value >= 50,
      four_week_change_pct: (now / before - 1) * 100,
      signal: sig, side: called ? sig : null,
      skipped: Boolean(sig && !called), leverage, price,
      record: sideConfig ? record(sideConfig, sideConfig.suggested_leverage) : null,
    };
    if (called && sig && leverage !== null && price) {
      const sign = sig === "long" ? 1 : -1;
      const candles = inputs.candles[coin] ?? new Map<number, Day>();
      const sunday = closes[0].day;
      // The trade opens at Monday's open; before that candle has closed, use the live price.
      const entry = candles.get(sunday + 1)?.open || price;
      const distance = liquidationDistance(leverage, maxLev);
      row.entry = entry;
      row.liquidation_move_pct = distance * 100;
      row.liquidation_price = entry * (1 - sign * distance);
      const normal = normalOf(candles, sunday);
      if (normal && exits.stop) {
        const stopMove = Math.min(exits.stop * normal, distance * 0.95);
        row.normal_week_pct = normal * 100;
        row.stop_pct = stopMove * 100;
        row.stop_price = entry * (1 - sign * stopMove);
        row.stake_lost_at_stop = Math.min(1.0, leverage * (stopMove + 2 * FEE_PER_SIDE));
        if (exits.optional_target) {
          const targetMove = exits.optional_target * normal;
          row.target_pct = targetMove * 100;
          row.target_price = entry * (1 + sign * targetMove);
        }
      }
    }
    coins.push(row);
  }
  const weekStart = parseDay(mood.week_reading.date) + 1;
  return {
    calls: coins.filter((c) => c.side).map((c) => ({ coin: c.coin, side: c.side as Side, leverage: c.leverage as number })),
    mood: mood.week_reading, coins, spot_record: spotRecord(),
    week_of: isoDay(weekStart), next_call: isoDay(weekStart + 7),
    preview: preview(config, mood, live, inputs.closes),
    exits: { stop: exits.stop, optional_target: exits.optional_target, with: exits.with, holding: exits.holding },
  };
}

/** What next Monday's calls would be if the week ended now. Final only on Monday. */
export function preview(config: CallsConfig, mood: { today?: MoodReading }, live: WeekInputs["live"],
                        closesByCoin: Record<string, SundayClose[]>): WeekPreview | null {
  const today = mood.today;
  if (!today) return null;
  const rows: PreviewCoin[] = [];
  for (const coin of Object.keys(config)) {
    const closes = closesByCoin[coin] ?? [];
    const price = live.get(coin)?.price;
    if (closes.length < 4 || !price) continue;
    const before = closes[3].close;              // four weeks before the coming week's end
    const sig = signal(today.value, price, before);
    const called = Boolean(sig && config[coin][sig].allowed);
    rows.push({
      coin, name: NAMES[coin] ?? coin, signal: sig,
      side: called ? sig : null, skipped: Boolean(sig && !called),
      leverage: called && sig ? config[coin][sig].suggested_leverage : null,
      four_week_change_pct: (price / before - 1) * 100,
    });
  }
  return { mood: today, coins: rows };
}

export async function thisWeek(): Promise<Week> {
  const config = callsConfig();
  const coins = Object.keys(config);
  const [mood, live, closes, candles] = await Promise.all([
    cryptoMood.thisWeek(), catalogue(),
    Promise.all(coins.map(sundayCloses)), Promise.all(coins.map(dailyCandles)),
  ]);
  try {
    const week = buildWeek({
      config, exits: exitRule(), mood, live,
      closes: Object.fromEntries(coins.map((coin, i) => [coin, closes[i]])),
      candles: Object.fromEntries(coins.map((coin, i) => [coin, candles[i]])),
    });
    cryptoMood.expectUpdate(false);
    return week;
  } catch (error) {
    // The prices have moved on to a new week and the mood index has not yet: look for it more often.
    if (error instanceof DataError && error.message === OUT_OF_STEP) cryptoMood.expectUpdate(true);
    throw error;
  }
}

// ---- week by week ----------------------------------------------------------------

export interface WeekResult {
  known: boolean;
  side?: Side | null;
  skipped?: boolean;
  result?: number;
  wiped?: boolean;
  stopped?: boolean;
  leverage_used?: number;
}

/** One coin, one finished week: the call, and what its share of the stake made. */
export function weekResult(candles: Days, monday: number, moodValue: number, share: number, leverage: number,
                           maxLeverage: number, coinConfig: Record<Side, SideConfig>, stopRule: number | null,
                           normalOf: (candles: Days, sunday: number) => number | null = normalWeek): WeekResult {
  const sundayBefore = monday - 1;
  const days: Day[] = [];
  for (let i = 0; i < 7; i += 1) {
    const day = candles.get(monday + i);
    if (!day) return { known: false };
    days.push(day);
  }
  const now = candles.get(sundayBefore);
  const before = candles.get(sundayBefore - 28);
  if (!now || !before) return { known: false };
  const sig = signal(moodValue, now.close, before.close);
  if (!sig || !coinConfig[sig].allowed) {
    return { known: true, side: null, skipped: Boolean(sig), result: 0.0, wiped: false, stopped: false };
  }
  const lev = leverage === SUGGESTED ? coinConfig[sig].suggested_leverage as number : Math.min(leverage, maxLeverage);
  const entry = days[0].open;
  let move = sig === "long" ? days[6].close / entry - 1 : 1 - days[6].close / entry;
  const liquidation = liquidationDistance(lev, maxLeverage);
  const normal = stopRule ? normalOf(candles, sundayBefore) : null;
  const stopMove = stopRule && normal ? stopRule * normal : null;
  // Walk the week a day at a time: liquidation or the stop ends it early.
  let held = 0;
  let wiped = false;
  let stopped = false;
  for (const day of days) {
    held += 1;
    const worst = sig === "long" ? 1 - day.low / entry : day.high / entry - 1;
    if (stopMove !== null && stopMove < liquidation && worst >= stopMove) {
      move = -stopMove;
      stopped = true;
      break;
    }
    if (worst >= liquidation) {
      wiped = true;
      break;
    }
  }
  const funding = sig === "long" && lev > 1 ? held * FUNDING_PER_DAY : 0.0;
  const result = wiped ? -share : share * lev * (move - 2 * FEE_PER_SIDE - funding);
  return { known: true, side: sig, skipped: false, result: Math.max(result, -share), wiped, stopped,
           leverage_used: lev };
}

export interface ScoreboardInputs {
  config: CallsConfig;
  stopRule: number | null;
  moods: Map<number, number>;
  live: WeekInputs["live"];
  candles: Record<string, Days>;
  normalWeek?: (candles: Days, sunday: number) => number | null;
}

/** The last finished weeks from data already in hand. */
export function buildScoreboard(inputs: ScoreboardInputs, stake: number, leverage: number, weeks = 12): Scoreboard {
  const { config, moods, live, candles } = inputs;
  const coins = Object.keys(config);
  const maxLev: Record<string, number> = {};
  for (const coin of coins) maxLev[coin] = live.get(coin)?.max_leverage ?? 20;
  const newest = coins.map((coin) => Math.max(...candles[coin].keys()));
  if (!newest.length || newest.some((day) => !Number.isFinite(day))) {
    throw new DataError("Not enough weekly prices to decide.");
  }
  const lastDay = Math.min(...newest);
  // the last finished week ends on the latest completed Sunday
  const lastSunday = lastDay - (weekday(lastDay) + 1) % 7;
  const rows: ScoreRow[] = [];
  for (let i = 0; i < weeks; i += 1) {
    const monday = lastSunday - (6 + 7 * i);
    const moodValue = moods.get(monday - 1);
    if (moodValue === undefined) continue;
    const parts = coins.map((coin) => weekResult(candles[coin], monday, moodValue, stake / coins.length, leverage,
                                                 maxLev[coin], config[coin], inputs.stopRule, inputs.normalWeek));
    if (!parts.every((p) => p.known)) continue;
    rows.push({
      week_of: isoDay(monday), mood: moodValue,
      calls: coins.flatMap((coin, k) => (parts[k].side
        ? [{ coin, side: parts[k].side as Side, leverage: parts[k].leverage_used as number }] : [])),
      skipped: coins.filter((_, k) => parts[k].skipped),
      wiped: coins.filter((_, k) => parts[k].wiped),
      stopped: coins.filter((_, k) => parts[k].stopped),
      result: pySum(parts.map((p) => p.result as number)), running_total: 0,
    });
  }
  let total = 0.0;
  for (let i = rows.length - 1; i >= 0; i -= 1) {          // oldest first for the running total
    total += rows[i].result;
    rows[i].running_total = total;
  }
  const expect: Expectation[] = [];
  for (const coin of coins) {
    for (const side of ["long", "short"] as const) {
      const c = config[coin][side];
      if (!c.allowed) continue;
      const lev = leverage === SUGGESTED ? c.suggested_leverage as number : Math.min(leverage, maxLev[coin]);
      // use the nearest tested level at or below the leverage actually used
      const tested = Object.keys(c.by_leverage).map((k) => parseInt(k.slice(0, -1), 10)).sort((a, b) => a - b);
      const below = tested.filter((t) => t <= lev);
      const level = below.length ? Math.max(...below) : tested[0];
      const at = c.by_leverage[`${level}x`];
      if (at) {
        expect.push({ coin, side, leverage: lev, weeks: at.weeks, ahead: at.ahead, liquidated: at.liquidated,
                      avg_per_week: at.avg_per_week });
      }
    }
  }
  const capped: Record<string, number> = {};
  for (const coin of coins) if (maxLev[coin] < leverage) capped[coin] = maxLev[coin];
  return {
    stake, leverage, rows, total,
    ahead: rows.filter((r) => r.result > 0).length,
    behind: rows.filter((r) => r.result < 0).length,
    no_trade: rows.filter((r) => !r.calls.length).length,
    expect, capped,
  };
}

/** The last finished weeks: the calls made and what a fresh stake made each week. */
export async function scoreboard(stake: number, leverage: number, weeks = 12): Promise<Scoreboard> {
  const config = callsConfig();
  const coins = Object.keys(config);
  const [moods, live, candles] = await Promise.all([
    cryptoMood.history(), catalogue(), Promise.all(coins.map(dailyCandles)),
  ]);
  return buildScoreboard({
    config, stopRule: exitRule().stop, moods, live,
    candles: Object.fromEntries(coins.map((coin, i) => [coin, candles[i]])),
  }, stake, leverage, weeks);
}
