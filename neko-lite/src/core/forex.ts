// Forex: the weekly picture for each pair, and the trader's own plan in pips.
//
// The figures are worked out by the app's one server function (api/fx.ts),
// because the central bank's rates cannot be read straight from a browser and
// its 27-year table is too large to ship. If the function is missing or down,
// only the forex board and the forex page are affected.
//
// The function never sees the account size or the risk: the position size is
// worked out here, from the price and pip size the function sends back.

import { DataError, onReset, runtime } from "./runtime";
import type { ForexOverview, ForexPair, ForexPlan, ForexSummary, Side, TrendDirection } from "./types";

const KEEP_MS = 3600 * 1000;           // the rates change once a working day
const RETRY_AFTER_MS = 5 * 60 * 1000;  // after a failure, leave the function alone for a while
export const LOT_UNITS = 100_000;      // one standard lot, in units of the base currency
export const MAX_PIPS = 5000;          // the furthest stop or target the planner accepts

/** The pairs the planner knows, in the order the screens list them. */
export const PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "NZDUSD", "USDCAD", "EURGBP", "EURJPY",
                      "EURCHF", "GBPJPY", "AUDJPY", "EURAUD", "AUDNZD"] as const;

/** What the function answers for a plan: everything that does not depend on the account. */
interface PlanOdds {
  pair: string;
  direction: Side;
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
  all: ForexSummary;
  with_trend: ForexSummary;
  against_trend: ForexSummary;
  trend_13w: TrendDirection;
  plan_is_with_trend: boolean;
  cost_pct: number;
  history_from: string;
  base_usd: number;
}

let held: { at: number; board: ForexOverview } | null = null;
let failedAt = -Infinity;
let loading: Promise<ForexOverview> | null = null;
let odds = new Map<string, { at: number; value: PlanOdds }>();

onReset(() => {
  held = null;
  failedAt = -Infinity;
  loading = null;
  odds = new Map();
});

const unavailable = () => new DataError("Exchange-rate history is unavailable right now.");

/** This week's picture for every pair (forex_weekly.overview()). */
export function overview(): Promise<ForexOverview> {
  const now = runtime.now();
  if (held && now - held.at < KEEP_MS) return Promise.resolve(held.board);
  if (now - failedAt < RETRY_AFTER_MS) return Promise.reject(unavailable());
  if (loading) return loading;
  const request = (async () => {
    try {
      const response = await runtime.fetch(runtime.fxUrl);
      if (!response.ok) throw unavailable();
      const board = await response.json() as Partial<ForexOverview>;
      if (!Array.isArray(board.pairs)) throw unavailable();
      const whole: ForexOverview = { pairs: board.pairs, as_of: board.as_of ?? null,
                                     history_from: board.history_from ?? null };
      held = { at: now, board: whole };
      return whole;
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

/** The rows of the forex board (forex_weekly.overview()["pairs"]). */
export async function pairs(): Promise<ForexPair[]> {
  return (await overview()).pairs;
}

/** One pip in price terms: 0.01 for pairs quoted in yen, 0.0001 for the rest. */
export function pipSize(pair: string): number {
  return pair.endsWith("JPY") ? 0.01 : 0.0001;
}

/** What is wrong with a plan, in plain words, or null when it can be checked. */
export function planProblem(pair: string, direction: string, stopPips: number, targetPips: number,
                            spreadPips = 0): string | null {
  if (!(PAIRS as readonly string[]).includes(pair)) return "That pair is not on the list.";
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

/** How often this shape reached its target or its stop, from the function. Kept for an hour. */
async function planOdds(pair: string, direction: Side, stopPips: number, targetPips: number,
                        spreadPips: number): Promise<PlanOdds> {
  const query = new URLSearchParams({ pair, direction, stop: String(stopPips), target: String(targetPips),
                                      spread: String(spreadPips) });
  const url = `${runtime.fxUrl}?${query}`;
  const now = runtime.now();
  const hit = odds.get(url);
  if (hit && now - hit.at < KEEP_MS) return hit.value;
  let response: Response;
  try {
    response = await runtime.fetch(url);
  } catch {
    throw unavailable();
  }
  let body: (Partial<PlanOdds> & { error?: unknown }) | null = null;
  try {
    body = await response.json();
  } catch { /* not JSON: handled below */ }
  if (!response.ok || !body || typeof body.price !== "number") {
    // The function explains a refused plan in plain words; anything else is the source being down.
    throw typeof body?.error === "string" ? new DataError(body.error) : unavailable();
  }
  const value = body as PlanOdds;
  odds.set(url, { at: now, value });
  return value;
}

/**
 * What a plan of this shape has done, and how big to trade it (forex_weekly.plan).
 * The stop, the target and the broker's round-trip spread are in pips.
 */
export async function plan(pair: string, direction: string, stopPips: number, targetPips: number,
                           account: number, riskPct: number, spreadPips = 0): Promise<ForexPlan> {
  pair = pair.trim().toUpperCase();
  direction = direction.trim().toLowerCase();
  const problem = planProblem(pair, direction, stopPips, targetPips, spreadPips);
  if (problem) throw new DataError(problem);
  if (!(account > 0 && account <= 1_000_000_000)) throw new DataError("Enter your account size in USD.");
  if (!(riskPct > 0 && riskPct <= 100)) {
    throw new DataError("Enter the risk per trade as a percentage between 0 and 100.");
  }
  const o = await planOdds(pair, direction as Side, stopPips, targetPips, spreadPips);

  // The position that loses the chosen share of the account at the stop, spread included.
  const quoteUsd = o.base_usd / o.price;            // one unit of the quote currency, in dollars
  const riskUsd = account * riskPct / 100;
  const positionUsd = riskUsd / ((o.stop_pct + o.cost_pct) / 100);
  const units = positionUsd / o.base_usd;
  const pipValueUsd = units * o.pip_size * quoteUsd;
  return {
    pair: o.pair, direction: o.direction, price: o.price, as_of: o.as_of,
    pip_size: o.pip_size, stop_pips: o.stop_pips, target_pips: o.target_pips, spread_pips: o.spread_pips,
    normal_week_pct: o.normal_week_pct, normal_week_pips: o.normal_week_pips,
    stop_pct: o.stop_pct, target_pct: o.target_pct,
    stop_price: o.stop_price, target_price: o.target_price,
    stop_in_weeks: o.stop_in_weeks, target_in_weeks: o.target_in_weeks,
    all: o.all, with_trend: o.with_trend, against_trend: o.against_trend,
    trend_13w: o.trend_13w, plan_is_with_trend: o.plan_is_with_trend,
    sizing: {
      position_usd: positionUsd, units, lots: units / LOT_UNITS,
      leverage: positionUsd / account,
      pip_value_usd: pipValueUsd,
      pip_value_per_lot_usd: LOT_UNITS * o.pip_size * quoteUsd,
      loss_at_stop_usd: (o.stop_pips + o.spread_pips) * pipValueUsd,
      gain_at_target_usd: (o.target_pips - o.spread_pips) * pipValueUsd,
    },
    cost_pct: o.cost_pct, history_from: o.history_from,
    avg_result_usd: positionUsd * o.all.avg_result_pct / 100,
  };
}
