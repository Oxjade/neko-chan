// The trend rule and each market's record under it.
//
// Port of the parts of service/server/desk.py the terminal reads. One rule
// produces every trend signal: on 4-hour bars, long when the 10-day average
// crosses above the 40-day average, short on the opposite cross, with a stop
// 4 ATR from entry, held until the opposite cross.
//
// For each market the same rule is replayed over that market's own history,
// so a signal is always shown beside what it has actually done there. Where
// a direction has lost money on a market it is muted.

import { isoMoment } from "./dates";
import type { Bars } from "./history";
import { pairwiseSum, pySum } from "./numeric";
import type { Market, Side, SignalState, TrendDirection } from "./types";

export const FAST_BARS = 60;
export const SLOW_BARS = 240;
export const ATR_BARS = 14;
export const STOP_ATR = 4.0;
export const COST_PCT = 0.13;               // round trip, exchange fees and slippage
export const NEW_SIGNAL_BARS = 6;           // a cross within the last 24 hours is "new"
export const MIN_TRADES_TO_JUDGE = 5;

type Numbers = ArrayLike<number>;

/** Exponential average, seeded with the first value. */
export function ema(values: Numbers, span: number): Float64Array {
  const k = 2.0 / (span + 1);
  const out = new Float64Array(values.length);
  if (!values.length) return out;
  out[0] = values[0];
  for (let i = 1; i < values.length; i += 1) out[i] = values[i] * k + out[i - 1] * (1 - k);
  return out;
}

/** Average true range over `n` bars; NaN until there are enough bars. */
export function atrSeries(high: Numbers, low: Numbers, close: Numbers, n = ATR_BARS): Float64Array {
  const count = close.length;
  const tr = new Float64Array(count);
  for (let i = 0; i < count; i += 1) {
    const range = high[i] - low[i];
    // The first bar has no previous close, so its true range is just high minus low.
    tr[i] = i === 0 ? range : Math.max(range, Math.abs(high[i] - close[i - 1]), Math.abs(low[i] - close[i - 1]));
  }
  const out = new Float64Array(count).fill(NaN);
  for (let i = n - 1; i < count; i += 1) out[i] = pairwiseSum(tr, i - n + 1, i + 1) / n;
  return out;
}

export interface Trade {
  direction: Side;
  opened_at: string;
  closed_at: string;
  entry: number;
  exit: number;
  reason: "stop" | "reversal";
  gross_pct: number;
}

export interface OpenTrade {
  i: number;
  dir: 1 | -1;
  px: number;
  stop: number;
}

export interface Replay {
  trades: Trade[];
  open: OpenTrade | null;
  pending: { direction: Side; stop_distance: number } | null;
  bars: number;
  trend: TrendDirection;
  trend_days: number;
  atr_pct: number | null;
  last_close: number;
  last_bar: string;
  times: Numbers;
}

/**
 * Run the rule over the bars. Returns closed trades and the current state.
 *
 * Signal on a bar's close, entry at the next bar's open. A gap through the
 * stop fills at the open. `warmup` bars are skipped so the averages have
 * settled before the first trade.
 */
export function replay(bars: Bars, warmup = SLOW_BARS): Replay {
  const { open: o, high: h, low: l, close: c, t } = bars;
  const fast = ema(c, FAST_BARS);
  const slow = ema(c, SLOW_BARS);
  const n = c.length;
  const up = new Array<boolean>(n);
  for (let i = 0; i < n; i += 1) up[i] = fast[i] > slow[i];
  const atr = atrSeries(h, l, c);
  const trades: Trade[] = [];
  let pos: OpenTrade | null = null;
  for (let i = Math.max(1, warmup); i < n; i += 1) {
    if (pos !== null) {
      const d = pos.dir;
      let exitPx: number | null = null;
      let reason: Trade["reason"] = "stop";
      if (d > 0 ? l[i] <= pos.stop : h[i] >= pos.stop) {
        exitPx = d > 0 ? Math.min(o[i], pos.stop) : Math.max(o[i], pos.stop);
      } else if (up[i - 1] !== (d > 0)) {
        exitPx = o[i];
        reason = "reversal";
      }
      if (exitPx !== null) {
        trades.push({
          direction: d > 0 ? "long" : "short",
          opened_at: isoMoment(t[pos.i]), closed_at: isoMoment(t[i]),
          entry: pos.px, exit: exitPx, reason,
          gross_pct: (exitPx / pos.px - 1) * 100 * d,
        });
        pos = null;
      }
    }
    const crossed = i >= 2 && up[i - 1] !== up[i - 2];
    if (pos === null && crossed && !Number.isNaN(atr[i - 1])) {
      const d = up[i - 1] ? 1 : -1;
      pos = { i, dir: d, px: o[i], stop: o[i] - d * STOP_ATR * atr[i - 1] };
    }
  }
  let pending: Replay["pending"] = null;
  if (pos === null && n >= 2 && up[n - 1] !== up[n - 2] && !Number.isNaN(atr[n - 1])) {
    pending = { direction: up[n - 1] ? "long" : "short", stop_distance: STOP_ATR * atr[n - 1] };
  }
  let held = 1;
  while (held < n && up[n - held - 1] === up[n - 1]) held += 1;
  return {
    trades, open: pos, pending, bars: n,
    trend: up[n - 1] ? "up" : "down", trend_days: held * 4 / 24,
    atr_pct: Number.isNaN(atr[n - 1]) ? null : atr[n - 1] / c[n - 1] * 100,
    last_close: c[n - 1], last_bar: isoMoment(t[n - 1]), times: t,
  };
}

export interface Trust {
  state: "unproven" | "muted" | "trusted";
  trades: number;
  wins: number;
  total_net_pct: number;
}

/** Has this direction earned its place on this market? */
export function trust(rows: Array<Pick<Trade, "direction" | "gross_pct">>, direction: Side): Trust {
  const mine = rows.filter((r) => r.direction === direction).map((r) => r.gross_pct - COST_PCT);
  const total = pySum(mine);
  const wins = mine.filter((net) => net > 0).length;
  const state = mine.length < MIN_TRADES_TO_JUDGE ? "unproven" : total <= 0 ? "muted" : "trusted";
  return { state, trades: mine.length, wins, total_net_pct: total };
}

/** The rule's record on one market, net of the round-trip cost (percent of position). */
export function record(trades: Trade[]): { trades: number; wins: number; expectancy: number | null } {
  const nets = trades.map((trade) => trade.gross_pct - COST_PCT);
  return { trades: nets.length, wins: nets.filter((net) => net > 0).length,
           expectancy: nets.length ? pySum(nets) / nets.length : null };
}

export interface TrendSignal {
  state: SignalState;
  direction: Side | null;
  entry?: number;
  stop?: number;
  since?: string;
  change_pct?: number;
  muted?: boolean;
  stop_pct?: number;
}

export interface MarketSignal extends Market {
  trend: TrendDirection;
  trend_days: number;
  atr_pct: number | null;
  history_days: number;
  signal: TrendSignal;
  trust: Record<Side, Trust>;
  record: { trades: number; wins: number; expectancy: number | null };
}

/** Everything the trend rule says about one market, from its loaded history and live price. */
export function marketSignal(live: Market, run: Replay): MarketSignal {
  const price = live.price;
  const trustBy = { long: trust(run.trades, "long"), short: trust(run.trades, "short") };
  let signal: TrendSignal = { state: "waiting", direction: null };
  if (run.pending) {
    const d = run.pending.direction;
    const sign = d === "long" ? 1 : -1;
    signal = { state: "new", direction: d, entry: price, stop: price - sign * run.pending.stop_distance,
               since: run.last_bar, change_pct: 0.0 };
  } else if (run.open) {
    const pos = run.open;
    const barsAgo = run.bars - pos.i;
    signal = { state: barsAgo <= NEW_SIGNAL_BARS ? "new" : "active", direction: pos.dir > 0 ? "long" : "short",
               entry: pos.px, stop: pos.stop, since: isoMoment(run.times[pos.i]),
               change_pct: (price / pos.px - 1) * 100 * pos.dir };
  } else if (run.trades.length && run.trades[run.trades.length - 1].reason === "stop") {
    signal = { state: "stopped", direction: null, since: run.trades[run.trades.length - 1].closed_at };
  }
  if (signal.direction && trustBy[signal.direction].state === "muted") signal.muted = true;
  if (signal.stop && signal.entry !== undefined) {
    signal.stop_pct = Math.abs(signal.entry - signal.stop) / signal.entry * 100;
  }
  return {
    ...live,
    trend: run.trend, trend_days: run.trend_days, atr_pct: run.atr_pct,
    history_days: run.bars * 4 / 24,
    signal, trust: trustBy, record: record(run.trades),
  };
}
