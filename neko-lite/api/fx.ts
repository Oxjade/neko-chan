// The one server piece of the app: the forex figures.
//
// The European Central Bank publishes its daily reference rates as a zip file
// that browsers are not allowed to read from another site, so this small
// function downloads it and returns only what a screen needs (a few kilobytes
// instead of the two-megabyte table):
//
//   /api/fx                    the weekly picture for each pair (the forex board)
//   /api/fx?pair=EURUSD&direction=long&stop=50&target=100&spread=0
//                              what a plan of that shape has done since 1999
//
// Port of service/server/forex_weekly.py. No rule tested picks a forex pair's
// direction better than a coin flip, so nothing here picks one: it reports how
// far each pair normally moves in a week, and how often the trader's own plan
// (a stop and a target, in pips) reached its target or its stop.
//
// A plan request carries no account size and no risk: those are personal, and
// leaving them out lets the CDN keep one answer for everyone who asks about
// the same shape. The browser works out the position size from the answer.
//
// Runs on Vercel as a Node function; in local dev and preview the Vite config
// calls the same handler. It imports nothing from the rest of the app.

import { inflateRawSync } from "node:zlib";

const ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip";
const DAY_MS = 24 * 3600 * 1000;
const VOL_WEEKS = 26;            // the "normal week" is measured over the last half year
const MIN_WEEKS = 60;
const MIN_PLAN_WEEKS = 50;       // fewer past weeks than this cannot judge a plan
export const MAX_PIPS = 5000;    // the furthest stop or target the planner accepts
const KEEP_RATES_MS = 3600 * 1000;
const UNAVAILABLE = "Exchange-rate history is unavailable right now.";

// pair -> [numerator column, denominator column or null]; price = quote per base
const PAIRS: Record<string, [string, string | null]> = {
  EURUSD: ["USD", null], GBPUSD: ["USD", "GBP"], USDJPY: ["JPY", "USD"],
  USDCHF: ["CHF", "USD"], AUDUSD: ["USD", "AUD"], NZDUSD: ["USD", "NZD"],
  USDCAD: ["CAD", "USD"], EURGBP: ["GBP", null], EURJPY: ["JPY", null],
  EURCHF: ["CHF", null], GBPJPY: ["JPY", "GBP"], AUDJPY: ["JPY", "AUD"],
  EURAUD: ["AUD", null], AUDNZD: ["NZD", "AUD"],
};

export interface ForexPair {
  pair: string;
  price: number;
  as_of: string;
  last_week_pct: number;
  normal_week_pct: number;
  pip_size: number;
  last_week_pips: number;
  normal_week_pips: number;
  range_low: number;
  range_high: number;
  range_held: number;
  trend_13w: "up" | "down";
  trend_52w: "up" | "down";
}

export interface ForexOverview {
  pairs: ForexPair[];
  as_of: string | null;
  history_from: string | null;
}

/** Daily rates, oldest first. `day` counts days since 1970; a missing rate is NaN. */
export interface DailyRates {
  days: number[];
  columns: Record<string, Float64Array>;
}

export type Direction = "long" | "short";

/** How a set of past weeks went for a plan (forex_weekly._summary). */
export interface PlanSummary {
  weeks: number;
  win_rate: number | null;
  win_rate_low: number | null;
  win_rate_high: number | null;
  wins: number;
  breakeven_win_rate: number | null;
  target_first: number;
  stop_first: number;
  week_ended: number;
  week_ended_avg_pct: number | null;
  avg_result_pct: number;
  week_ended_avg_pips: number | null;
  avg_result_pips: number;
}

/** Everything about a plan that does not depend on the trader's account. */
export interface PlanOdds {
  pair: string;
  direction: Direction;
  price: number;
  as_of: string;
  pip_size: number;
  stop_pips: number;
  target_pips: number;
  spread_pips: number;
  normal_week_pct: number;
  normal_week_pips: number;
  stop_pct: number;
  target_pct: number;
  stop_price: number;
  target_price: number;
  stop_in_weeks: number;
  target_in_weeks: number;
  all: PlanSummary;
  with_trend: PlanSummary;
  against_trend: PlanSummary;
  trend_13w: "up" | "down";
  plan_is_with_trend: boolean;
  cost_pct: number;
  history_from: string;
  /** One unit of the pair's base currency in US dollars, for sizing the position. */
  base_usd: number;
}

const isoDay = (day: number) => new Date(day * DAY_MS).toISOString().slice(0, 10);

/** One pip in price terms: 0.01 for pairs quoted in yen, 0.0001 for the rest. */
export function pipSize(pair: string): number {
  return pair.endsWith("JPY") ? 0.01 : 0.0001;
}

/** How many pips one percent of the price is. */
export function pipsPerPercent(pair: string, price: number): number {
  return price / pipSize(pair) / 100;
}

/** What is wrong with a plan request, in plain words, or null when it can be checked. */
export function planProblem(pair: string, direction: string, stopPips: number, targetPips: number,
                            spreadPips = 0): string | null {
  if (!Object.hasOwn(PAIRS, pair)) return "That pair is not on the list.";
  if (direction !== "long" && direction !== "short") return "Choose long or short.";
  if (!(stopPips > 0 && stopPips <= MAX_PIPS)) {
    return `Enter the stop distance in pips: more than 0 and at most ${MAX_PIPS}.`;
  }
  if (!(targetPips > 0 && targetPips <= MAX_PIPS)) {
    return `Enter the target distance in pips: more than 0 and at most ${MAX_PIPS}.`;
  }
  if (!(spreadPips >= 0 && spreadPips < stopPips)) {
    return "Enter your broker's spread in pips: 0 or more, and smaller than the stop.";
  }
  return null;
}

/** The first file inside a zip archive, as text. */
export function unzipFirst(zip: Uint8Array): string {
  const view = new DataView(zip.buffer, zip.byteOffset, zip.byteLength);
  // The directory at the end of the archive says where the file starts and how long it is.
  let end = zip.length - 22;
  while (end >= 0 && view.getUint32(end, true) !== 0x06054b50) end -= 1;
  if (end < 0) throw new Error("not a zip archive");
  const entry = view.getUint32(end + 16, true);
  if (view.getUint32(entry, true) !== 0x02014b50) throw new Error("zip directory is damaged");
  const method = view.getUint16(entry + 10, true);
  const packedSize = view.getUint32(entry + 20, true);
  const header = view.getUint32(entry + 42, true);
  if (view.getUint32(header, true) !== 0x04034b50) throw new Error("zip entry is damaged");
  const start = header + 30 + view.getUint16(header + 26, true) + view.getUint16(header + 28, true);
  const packed = zip.subarray(start, start + packedSize);
  if (method === 0) return new TextDecoder().decode(packed);
  if (method !== 8) throw new Error("zip compression is not supported");
  return new TextDecoder().decode(inflateRawSync(packed));
}

/** Read the ECB table: one row per day, one column per currency (units per euro). */
export function parseRates(csv: string): DailyRates {
  const lines = csv.split(/\r?\n/).filter((line) => line.trim());
  const names = lines[0].split(",").map((name) => name.trim());
  const rows = lines.slice(1).map((line) => line.split(","))
    .map((cells) => ({ day: Math.floor(Date.parse(`${cells[0].trim()}T00:00:00Z`) / DAY_MS), cells }))
    .filter((row) => Number.isFinite(row.day))
    .sort((a, b) => a.day - b.day);
  const columns: Record<string, Float64Array> = {};
  names.forEach((name, at) => {
    if (at === 0 || !name) return;
    const values = new Float64Array(rows.length);
    rows.forEach((row, i) => {
      const cell = (row.cells[at] ?? "").trim();
      values[i] = cell === "" || cell === "N/A" ? NaN : Number(cell);
    });
    columns[name] = values;
  });
  return { days: rows.map((row) => row.day), columns };
}

/** A price on each day it has one, oldest first. */
export interface Series {
  days: number[];
  values: number[];
}

/** A pair's price on every day both of its currencies have a rate. */
export function pairSeries(daily: DailyRates, pair: string): Series {
  const [num, den] = PAIRS[pair];
  const top = daily.columns[num];
  const bottom = den === null ? null : daily.columns[den];
  const out: Series = { days: [], values: [] };
  if (!top || (den !== null && !bottom)) return out;
  for (let i = 0; i < daily.days.length; i += 1) {
    const value = bottom ? top[i] / bottom[i] : top[i];
    if (Number.isNaN(value)) continue;
    out.days.push(daily.days[i]);
    out.values.push(value);
  }
  return out;
}

interface Weeks {
  /** The Friday that ends each week, as a day number. */
  friday: number[];
  close: number[];
  ret: number[];
  normal: number[];
}

/**
 * One row per week: Friday close (the last fixing on or before each Friday),
 * that week's move, and the normal weekly move (in percent) known at that
 * Friday. The first 26 weeks have no normal move yet and are left out.
 */
function weeklyFrame(series: Series): Weeks {
  const close: number[] = [];
  const fridays: number[] = [];
  let bin: number | null = null;
  series.days.forEach((day, i) => {
    // 1970-01-01 was a Thursday; a week's bin is the Friday on or after the day.
    const weekday = (((day + 3) % 7) + 7) % 7;               // Monday is 0, Friday is 4
    const friday = day + (((4 - weekday) % 7) + 7) % 7;
    if (friday === bin) close[close.length - 1] = series.values[i];
    else {
      close.push(series.values[i]);
      fridays.push(friday);
    }
    bin = friday;
  });
  const ret = close.map((value, k) => (k === 0 ? NaN : (value / close[k - 1] - 1) * 100));
  const normal = rollingStd(ret, VOL_WEEKS);
  return { friday: fridays.slice(VOL_WEEKS), close: close.slice(VOL_WEEKS), ret: ret.slice(VOL_WEEKS),
           normal: normal.slice(VOL_WEEKS) };
}

/**
 * Sample standard deviation over a moving window, NaN until the window is
 * full. Follows pandas' running calculation (add the newest value, remove the
 * oldest, with a correction term for rounding) so the figures match the
 * Python version to the last digits.
 */
function rollingStd(values: number[], window: number): number[] {
  let count = 0;
  let mean = 0;
  let squares = 0;
  let addSlip = 0;
  let removeSlip = 0;
  const add = (value: number) => {
    if (Number.isNaN(value)) return;
    count += 1;
    const previous = mean - addSlip;
    const y = value - addSlip;
    const t = y - mean;
    addSlip = t + mean - y;
    mean = count ? mean + t / count : 0;
    squares += (value - previous) * (value - mean);
  };
  const remove = (value: number) => {
    if (Number.isNaN(value)) return;
    count -= 1;
    if (!count) {
      mean = 0;
      squares = 0;
      return;
    }
    const previous = mean - removeSlip;
    const y = value - removeSlip;
    const t = y - mean;
    removeSlip = t + mean - y;
    mean -= t / count;
    squares -= (value - previous) * (value - mean);
  };
  return values.map((value, i) => {
    if (i >= window) remove(values[i - window]);
    add(value);
    if (count < window) return NaN;
    return Math.sqrt(Math.max(0, squares / (count - 1)));
  });
}

/** This week's picture for every pair, from the ECB table as text. */
export function overview(csv: string): ForexOverview {
  return overviewOf(parseRates(csv));
}

function overviewOf(daily: DailyRates): ForexOverview {
  const pairs: ForexPair[] = [];
  for (const pair of Object.keys(PAIRS)) {
    const series = pairSeries(daily, pair);
    const weeks = weeklyFrame(series);
    const n = weeks.close.length;
    if (n < MIN_WEEKS) continue;
    const price = series.values[series.values.length - 1];
    const normal = weeks.normal[n - 1];
    // How often the following week stayed within one normal move.
    let inside = 0;
    for (let k = 0; k < n - 1; k += 1) if (Math.abs(weeks.ret[k + 1]) <= weeks.normal[k]) inside += 1;
    const pip = pipSize(pair);
    pairs.push({
      pair, price, as_of: isoDay(series.days[series.days.length - 1]),
      last_week_pct: weeks.ret[n - 1], normal_week_pct: normal,
      pip_size: pip,
      // last week's move as it happened, and a normal week at today's price
      last_week_pips: (weeks.close[n - 1] - weeks.close[n - 2]) / pip,
      normal_week_pips: normal * pipsPerPercent(pair, price),
      range_low: price * (1 - normal / 100), range_high: price * (1 + normal / 100),
      range_held: inside / (n - 1),
      trend_13w: weeks.close[n - 1] > weeks.close[n - 14] ? "up" : "down",
      trend_52w: weeks.close[n - 1] > weeks.close[n - 53] ? "up" : "down",
    });
  }
  return {
    pairs,
    as_of: pairs.length ? pairs.map((row) => row.as_of).sort().pop() as string : null,
    history_from: daily.days.length ? isoDay(daily.days[0]) : null,
  };
}

// ---- a plan of the trader's own shape ------------------------------------------

/** One past week traded with a shape; results are in units of that week's normal move. */
export interface ShapeWeek {
  how: "target" | "stop" | "week ended";
  gross: number;
  net: number;
  fees: number;
  /** Whether the trade went with the 13-week trend; null while there is no trend yet. */
  with_trend: boolean | null;
}

/**
 * Every past week traded with this shape, in units of that week's normal move.
 *
 * costPct is the round-trip trading cost as a percent of the position.
 *
 * Entry at one Friday's fixing; each later daily fixing up to the next Friday
 * is checked for the stop, then the target; otherwise the trade is closed at
 * that Friday's fixing.
 */
export function replayShape(series: Series, direction: Direction, stopMult: number, targetMult: number,
                            costPct = 0): ShapeWeek[] {
  const weeks = weeklyFrame(series);
  const sign = direction === "long" ? 1.0 : -1.0;
  const out: ShapeWeek[] = [];
  let next = 0;                                     // the first daily fixing not yet inside a week
  for (let k = 0; k < weeks.close.length - 1; k += 1) {
    const entry = weeks.close[k];
    const normal = weeks.normal[k];
    while (next < series.days.length && series.days[next] <= weeks.friday[k]) next += 1;
    const first = next;
    while (next < series.days.length && series.days[next] <= weeks.friday[k + 1]) next += 1;
    if (!normal || Number.isNaN(normal) || first === next) continue;
    let result: number | null = null;
    let how: ShapeWeek["how"] = "week ended";
    for (let i = first; i < next; i += 1) {
      const move = (series.values[i] / entry - 1) * 100 * sign / normal;
      if (move <= -stopMult) {
        result = -stopMult;
        how = "stop";
        break;
      }
      if (move >= targetMult) {
        result = targetMult;
        how = "target";
        break;
      }
    }
    if (result === null) result = (series.values[next - 1] / entry - 1) * 100 * sign / normal;
    const trend = k >= 13 ? Math.sign(weeks.close[k] / weeks.close[k - 13] - 1) : NaN;
    out.push({ how, gross: result, net: result - costPct / normal, fees: costPct / normal,
               with_trend: Number.isNaN(trend) || trend === 0 ? null : trend === sign });
  }
  return out;
}

/**
 * A sum the way Python adds a list of numbers (with a correction for
 * rounding), so averages match the Python version to the last digit.
 */
function pySum(values: number[]): number {
  let started = false;
  let total = 0;
  let lost = 0;
  for (const x of values) {
    if (!started) {
      total = 0 + x;
      started = true;
      continue;
    }
    const next = total + x;
    lost += Math.abs(total) >= Math.abs(x) ? (total - next) + x : (x - next) + total;
    total = next;
  }
  if (lost && Number.isFinite(lost)) total += lost;
  return total;
}

/** 95% Wilson score interval for a win rate. Honest at small n. (web_core.wilson_interval) */
export function wilsonInterval(wins: number, n: number, z = 1.96): [number, number] {
  if (n <= 0) return [0.0, 1.0];
  const p = wins / n;
  const denom = 1 + z * z / n;
  const centre = (p + z * z / (2 * n)) / denom;
  const half = z * Math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom;
  return [Math.max(0.0, centre - half), Math.min(1.0, centre + half)];
}

export interface Performance {
  trades: number;
  wins: number;
  win_rate: number | null;
  win_rate_low: number | null;
  win_rate_high: number | null;
  breakeven_win_rate: number | null;
  expectancy: number | null;
}

/**
 * Win rate with its uncertainty, the break-even win rate the measured payoff
 * implies, and the average result per trade. Everything is net of costs.
 * (The part of web_core.performance the planner uses.)
 */
export function performance(trades: Array<{ net: number }>): Performance {
  const n = trades.length;
  const wins = trades.filter((t) => t.net > 0).map((t) => t.net);
  const losses = trades.filter((t) => t.net <= 0).map((t) => t.net);
  const [low, high] = wilsonInterval(wins.length, n);
  const avgWin = wins.length ? pySum(wins) / wins.length : 0.0;
  const avgLoss = losses.length ? -pySum(losses) / losses.length : 0.0;
  const net = pySum(trades.map((t) => t.net));
  return {
    trades: n,
    wins: wins.length,
    win_rate: n ? wins.length / n : null,
    win_rate_low: n ? low : null,
    win_rate_high: n ? high : null,
    breakeven_win_rate: avgWin > 0 && avgLoss > 0 ? avgLoss / (avgWin + avgLoss) : null,
    expectancy: n ? net / n : null,
  };
}

/** How a set of past weeks went, in percent of the price and in pips. */
export function summary(rows: ShapeWeek[], normalNow: number, pipsPerPct: number): PlanSummary {
  const perf = performance(rows);
  const n = rows.length || 1;
  const count = (how: ShapeWeek["how"]) => rows.filter((r) => r.how === how).length;
  const ended = rows.filter((r) => r.how === "week ended").map((r) => r.net);
  const endedAvg = ended.length ? pySum(ended) / ended.length * normalNow : null;
  const avg = (perf.expectancy || 0.0) * normalNow;
  return {
    weeks: rows.length,
    win_rate: perf.win_rate, win_rate_low: perf.win_rate_low, win_rate_high: perf.win_rate_high,
    wins: perf.wins, breakeven_win_rate: perf.breakeven_win_rate,
    target_first: count("target") / n, stop_first: count("stop") / n, week_ended: ended.length / n,
    week_ended_avg_pct: endedAvg,
    avg_result_pct: avg,
    week_ended_avg_pips: endedAvg === null ? null : endedAvg * pipsPerPct,
    avg_result_pips: avg * pipsPerPct,
  };
}

/** Value of one unit of the pair's base currency in US dollars, latest fixing. */
export function baseInUsd(daily: DailyRates, pair: string): number {
  const base = pair.slice(0, 3);
  if (base === "USD") return 1.0;
  const usd = daily.columns.USD ?? new Float64Array(0);
  let last = usd.length - 1;
  while (last >= 0 && Number.isNaN(usd[last])) last -= 1;
  if (last < 0) return NaN;
  return base === "EUR" ? usd[last] : usd[last] / (daily.columns[base]?.[last] ?? NaN);
}

/** A plan could not be judged for a reason worth telling the trader. */
export class PlanError extends Error {}

/**
 * What a plan of this shape has done on the pair (forex_weekly.plan, without
 * the position size). The stop, the target and the broker's round-trip spread
 * are in pips; inside they become a percentage of the current price, so past
 * weeks at very different price levels are compared like with like.
 */
export function planOdds(daily: DailyRates, pair: string, direction: Direction, stopPips: number,
                         targetPips: number, spreadPips = 0): PlanOdds {
  const series = pairSeries(daily, pair);
  const weeks = weeklyFrame(series);
  const n = weeks.close.length;
  if (n < 14) throw new PlanError(`${pair} has too little history to judge a plan.`);
  const price = series.values[series.values.length - 1];
  const normal = weeks.normal[n - 1];
  const pip = pipSize(pair);
  const pctPerPip = pip / price * 100;              // one pip as a percent of the current price
  const stopPct = stopPips * pctPerPip;
  const targetPct = targetPips * pctPerPip;
  const costPct = spreadPips * pctPerPip;
  const stopMult = stopPct / normal;
  const targetMult = targetPct / normal;
  const rows = replayShape(series, direction, stopMult, targetMult, costPct);
  if (rows.length < MIN_PLAN_WEEKS) throw new PlanError(`${pair} has too little history to judge a plan.`);
  const sign = direction === "long" ? 1.0 : -1.0;
  const trendUp = weeks.close[n - 1] > weeks.close[n - 14];
  const pipsPerPct = pipsPerPercent(pair, price);
  return {
    pair, direction, price, as_of: isoDay(series.days[series.days.length - 1]),
    pip_size: pip, stop_pips: stopPips, target_pips: targetPips, spread_pips: spreadPips,
    normal_week_pct: normal, normal_week_pips: normal * pipsPerPct,
    stop_pct: stopPct, target_pct: targetPct,
    stop_price: price * (1 - sign * stopPct / 100),
    target_price: price * (1 + sign * targetPct / 100),
    stop_in_weeks: stopMult, target_in_weeks: targetMult,
    all: summary(rows, normal, pipsPerPct),
    with_trend: summary(rows.filter((r) => r.with_trend === true), normal, pipsPerPct),
    against_trend: summary(rows.filter((r) => r.with_trend === false), normal, pipsPerPct),
    trend_13w: trendUp ? "up" : "down", plan_is_with_trend: trendUp === (direction === "long"),
    cost_pct: costPct, history_from: isoDay(weeks.friday[0]),
    base_usd: baseInUsd(daily, pair),
  };
}

// ---- the function ----------------------------------------------------------------

// The table changes once a working day. A warm function keeps it for an hour,
// so trying several stops and targets in a row downloads it once.
let held: { at: number; daily: DailyRates } | null = null;

/** Forget the table that is being kept (tests swap the source underneath). */
export function forgetRates(): void {
  held = null;
}

async function rates(): Promise<DailyRates> {
  if (held && Date.now() - held.at < KEEP_RATES_MS) return held.daily;
  const response = await fetch(ECB_URL, { signal: AbortSignal.timeout(30_000) });
  if (!response.ok) throw new Error(`ECB answered ${response.status}`);
  const daily = parseRates(unzipFirst(new Uint8Array(await response.arrayBuffer())));
  if (!daily.days.length) throw new Error("no rates in the ECB table");
  held = { at: Date.now(), daily };
  return daily;
}

const json = (body: unknown, status: number, cache: string) => new Response(JSON.stringify(body), {
  status, headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": cache },
});

// The rates change once a working day: let the CDN answer for an hour, and
// keep serving the last answer for a day while it fetches a new one.
const KEEP = "public, s-maxage=3600, stale-while-revalidate=86400";

export async function GET(request: Request): Promise<Response> {
  const query = new URL(request.url).searchParams;
  const wantsPlan = query.has("pair");
  const pair = (query.get("pair") ?? "").trim().toUpperCase();
  const direction = (query.get("direction") ?? "").trim().toLowerCase();
  const pips = (name: string, missing: number) => {
    const text = (query.get(name) ?? "").trim();
    return text === "" ? missing : Number(text);
  };
  const stop = pips("stop", NaN);
  const target = pips("target", NaN);
  const spread = pips("spread", 0);
  if (wantsPlan) {
    // A mistake in the request is answered before anything is downloaded.
    const problem = planProblem(pair, direction, stop, target, spread);
    if (problem) return json({ error: problem }, 400, "no-store");
  }
  try {
    const daily = await rates();
    if (wantsPlan) return json(planOdds(daily, pair, direction as Direction, stop, target, spread), 200, KEEP);
    const board = overviewOf(daily);
    if (!board.pairs.length) throw new Error("no rates in the ECB table");
    return json(board, 200, KEEP);
  } catch (error) {
    const message = error instanceof PlanError ? error.message : UNAVAILABLE;
    return json({ error: message }, 502, "no-store");
  }
}
