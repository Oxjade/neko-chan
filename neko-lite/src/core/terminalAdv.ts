// Advanced mode of the terminal: the dense, many-panel view.
//
// Port of service/server/terminal_adv.py. Every panel is computed from data
// the app already has: live prices, funding and open interest (Hyperliquid),
// 4-hour candles, the Fear & Greed index, the central-bank forex rates, and
// the saved research tables. Nothing is filled in for effect.
//
// Each panel fails on its own: a missing source empties that panel, not the page.

import * as cryptoMood from "./cryptoMood";
import { isoDay, isoMoment } from "./dates";
import * as desk from "./desk";
import * as forex from "./forex";
import { want, type Bars } from "./history";
import * as md from "./marketData";
import { logReturns, mean, pySum, sampleStd } from "./numeric";
import { runtime, soft } from "./runtime";
import * as terminal from "./terminal";
import type {
  AdvancedExits, AdvancedSnapshot, AssetClass, BreadthCount, CallsConfig, Correlation, ForexPair, FundingRow,
  LeverageTable, Market, MoodPoint, Mover, MoverGroup, Position, Risk, RiskRow, Scoreboard, Side, SignalRow,
  WeekCoin, WorksheetRow,
} from "./types";
import * as wc from "./weeklyCalls";

export const WORKSHEET: readonly string[] = [...md.CRYPTO, ...[
  "NVDA", "TSLA", "AAPL", "MSFT", "GOOGL", "META", "AMZN", "AMD", "COIN", "MSTR",
  "SP500", "XYZ100", "GOLD", "SILVER", "CL", "EUR", "GBP", "JPY"].map((name) => `xyz:${name}`)];
export const PERFORMANCE = ["BTC", "ETH", "SOL", "SUI", "xyz:NVDA", "xyz:SP500", "xyz:GOLD"] as const;
export const CORRELATION = ["BTC", "ETH", "SOL", "SUI", "xyz:NVDA", "xyz:SP500", "xyz:GOLD", "xyz:EUR"] as const;
export const BARS_PER_DAY = 6;
const MIN_VOLUME_USD = 250_000;            // ignore markets too thin for their day change to mean much
const ANNUAL = Math.sqrt(BARS_PER_DAY * 365);
const MONTH_BARS = 30 * BARS_PER_DAY;

type Live = Map<string, Market>;

/** Most up and most down today, per asset class. */
export function movers(live: Live): Record<AssetClass, MoverGroup> {
  const out = {} as Record<AssetClass, MoverGroup>;
  for (const cls of md.CLASSES) {
    const rows = [...live.values()].filter((r) => r.class === cls && r.day_change_pct !== null
      && r.day_volume_usd >= MIN_VOLUME_USD);
    rows.sort((a, b) => (a.day_change_pct as number) - (b.day_change_pct as number));
    const pick = (r: Market): Mover => ({ symbol: r.symbol, label: r.label, price: r.price,
                                          day_change_pct: r.day_change_pct as number });
    // with only a couple of markets in a class, the same one must not appear on both sides
    out[cls] = {
      up: rows.slice(-3).reverse().filter((r) => (r.day_change_pct as number) > 0).map(pick),
      down: rows.slice(0, 3).filter((r) => (r.day_change_pct as number) < 0).map(pick),
      count: rows.length,
    };
  }
  return out;
}

/** How many markets are up, down or flat today, per asset class. */
export function breadth(live: Live): Record<AssetClass, BreadthCount> {
  const out = {} as Record<AssetClass, BreadthCount>;
  for (const cls of md.CLASSES) {
    const changes = [...live.values()].filter((r) => r.class === cls && r.day_change_pct !== null)
      .map((r) => r.day_change_pct as number);
    out[cls] = {
      up: changes.filter((c) => c > 0.1).length,
      down: changes.filter((c) => c < -0.1).length,
      flat: changes.filter((c) => Math.abs(c) <= 0.1).length,
    };
  }
  return out;
}

type Stats = Omit<WorksheetRow, "price" | "day_change_pct" | "funding_apr" | "open_interest_usd" | "day_volume_usd">;

/** Slow-moving figures for one market, from its 4-hour candles. */
export function stats(market: Market, bars: Bars): Stats {
  const row = desk.marketSignal(market, terminal.replayOf(bars));
  const close = bars.close;
  const n = close.length;
  const returns = logReturns(close);
  const change = (count: number) => (n > count ? (close[n - 1] / close[n - count - 1] - 1) * 100 : null);
  const vol = (count: number) => (returns.length >= count
    ? sampleStd(returns, returns.length - count) * ANNUAL * 100 : null);
  return {
    symbol: market.symbol, label: row.label, class: row.class,
    week_pct: change(7 * BARS_PER_DAY), month_pct: change(MONTH_BARS),
    four_week_pct: change(28 * BARS_PER_DAY),
    trend: row.trend, trend_days: row.trend_days, atr_pct: row.atr_pct,
    vol_7d: vol(7 * BARS_PER_DAY), vol_30d: vol(MONTH_BARS),
    signal: row.signal.muted ? null : row.signal.direction,
    signal_state: row.signal.state,
    record_wins: row.record.wins, record_trades: row.record.trades,
    record_avg_pct: row.record.expectancy,
  };
}

const statsOf = new WeakMap<Bars, Stats>();

/**
 * One row per worksheet market whose history has loaded, and how many are
 * still downloading. A market that cannot be read is left out, as in Python.
 */
export function worksheet(live: Live): { rows: WorksheetRow[]; pending: number } {
  const rows: WorksheetRow[] = [];
  let pending = 0;
  for (const symbol of WORKSHEET) {
    const market = live.get(symbol);
    if (!market) continue;
    const history = md.candles(symbol);
    if (history.state === "pending") pending += 1;
    if (history.state !== "ready") continue;
    // These figures depend only on closed bars, so they are worked out once per set of bars.
    let slow = statsOf.get(history.bars);
    if (!slow) {
      try {
        slow = stats(market, history.bars);
      } catch {
        continue;
      }
      statsOf.set(history.bars, slow);
    }
    rows.push({
      ...slow, price: market.price, day_change_pct: market.day_change_pct,
      funding_apr: (market.funding_hourly || 0.0) * 24 * 365 * 100,
      open_interest_usd: market.open_interest_usd, day_volume_usd: market.day_volume_usd,
    });
  }
  return { rows, pending };
}

function closesOf(symbol: string): Bars | null {
  const history = md.candles(symbol);
  return history.state === "ready" ? history.bars : null;
}

/** Thirty days of each market, rebased to 100, one point every twelve hours. */
export function performance(): Record<string, number[]> {
  const series: Record<string, number[]> = {};
  for (const symbol of PERFORMANCE) {
    const bars = closesOf(symbol);
    if (!bars || bars.close.length < MONTH_BARS + 1) continue;
    const close = bars.close;
    const first = close.length - MONTH_BARS - 1;
    const points: number[] = [];
    for (let i = first; i < close.length; i += 3) points.push(close[i] / close[first] * 100);
    series[md.label(symbol)] = points;
  }
  return series;
}

/** How closely the markets moved together over the last thirty days (4-hour moves). */
export function correlation(): Correlation {
  const frames = new Map<string, Map<number, number>>();
  for (const symbol of CORRELATION) {
    const bars = closesOf(symbol);
    if (!bars) continue;
    const frame = new Map<number, number>();
    for (let i = Math.max(0, bars.t.length - MONTH_BARS - 1); i < bars.t.length; i += 1) {
      frame.set(bars.t[i], bars.close[i]);
    }
    frames.set(md.label(symbol), frame);
  }
  const labels = [...frames.keys()];
  if (labels.length < 2) return { labels: [], matrix: [] };
  const all = [...frames.values()];
  const shared = [...all[0].keys()].filter((t) => all.every((frame) => frame.has(t))).sort((a, b) => a - b);
  if (shared.length < 30) return { labels: [], matrix: [] };
  // Each market's 4-hour moves, with its own average taken out.
  const moves = labels.map((name) => {
    const frame = frames.get(name) as Map<number, number>;
    const returns = logReturns(shared.map((t) => frame.get(t) as number));
    const centre = mean(returns);
    return returns.map((r) => r - centre);
  });
  const count = moves[0].length;
  const scale = 1 / (count - 1);
  const cov = moves.map((a) => moves.map((b) => {
    let dot = 0;
    for (let i = 0; i < count; i += 1) dot += a[i] * b[i];
    return dot * scale;
  }));
  const spread = cov.map((row, i) => Math.sqrt(row[i]));
  const matrix = cov.map((row, i) => row.map((v, j) => Math.max(-1, Math.min(1, v / spread[i] / spread[j]))));
  return { labels, matrix, bars: shared.length };
}

/** Per coin and side: is it called, at what leverage, and how it has done. */
export function signalMatrix(week: { coins: Array<Pick<WeekCoin, "coin" | "side" | "signal">> },
                             config: CallsConfig = wc.callsConfig()): SignalRow[] {
  const rows: SignalRow[] = [];
  for (const coin of week.coins) {
    for (const side of ["long", "short"] as const) {
      const c = config[coin.coin][side];
      rows.push({
        coin: coin.coin, side, allowed: c.allowed,
        leverage: c.suggested_leverage, active: coin.side === side,
        pointed: coin.signal === side,
        right_first: c.first_half.right, right_second: c.second_half.right,
        avg_first: c.first_half.avg_pct, avg_second: c.second_half.avg_pct,
        weeks: (c.first_half.coin_weeks || 0) + (c.second_half.coin_weeks || 0),
      });
    }
  }
  return rows;
}

/** Share of traded weeks in which the weekly stake was liquidated, by leverage. */
export function leverageTable(config: CallsConfig = wc.callsConfig()): LeverageTable {
  const seen = new Set<number>();
  for (const sides of Object.values(config)) {
    for (const c of Object.values(sides)) {
      for (const key of Object.keys(c.by_leverage)) seen.add(parseInt(key.slice(0, -1), 10));
    }
  }
  const levels = [...seen].sort((a, b) => a - b);
  const rows: LeverageTable["rows"] = [];
  for (const [coin, sides] of Object.entries(config)) {
    for (const [side, c] of Object.entries(sides) as Array<[Side, CallsConfig[string][Side]]>) {
      if (!c.allowed) continue;
      const at = (level: number) => c.by_leverage[`${level}x`];
      rows.push({
        coin, side, suggested: c.suggested_leverage,
        liquidated: Object.fromEntries(levels.map((l) => [String(l), at(l)?.liquidated ?? null])),
        avg: Object.fromEntries(levels.map((l) => [String(l), at(l)?.avg_per_week ?? null])),
      });
    }
  }
  return { levels, rows };
}

export function risk(positions: Position[], stake: number): Risk {
  const rows: RiskRow[] = positions.map((p) => {
    const open = p.status === "open";
    const atStop = p.stop
      ? p.share * p.leverage * (Math.abs(p.stop - p.entry) / p.entry + 2 * wc.FEE_PER_SIDE)
      : p.share;
    return {
      coin: p.coin, side: p.side, status: p.status, leverage: p.leverage,
      exposure_usd: open ? p.position_usd : 0.0, pnl_usd: p.pnl_usd,
      stop_distance_pct: p.stop_distance_pct ?? null,
      liquidation_distance_pct: open && p.liquidation ? Math.abs(p.liquidation - p.last) / p.last * 100 : null,
      loss_at_stop_usd: open ? Math.min(atStop, p.share) : 0.0,
      normal_week_pct: p.normal_week_pct ?? null,
    };
  });
  const exposure = pySum(rows.map((r) => r.exposure_usd));
  return { rows, exposure_usd: exposure, exposure_of_stake: stake ? exposure / stake : 0.0,
           worst_case_usd: -pySum(rows.map((r) => r.loss_at_stop_usd)),
           in_cash_usd: stake - pySum(positions.filter((p) => p.status === "open").map((p) => p.share)) };
}

export function funding(live: Live): FundingRow[] {
  const rows = [...live.values()].filter((r) => r.class === "crypto").map((r) => ({
    symbol: r.symbol, label: r.label, funding_apr: r.funding_hourly * 24 * 365 * 100,
    open_interest_usd: r.open_interest_usd, day_volume_usd: r.day_volume_usd,
  }));
  return rows.sort((a, b) => Math.abs(b.funding_apr) - Math.abs(a.funding_apr));
}

export async function moodHistory(): Promise<MoodPoint[]> {
  const values = await cryptoMood.history();
  return [...values.entries()].sort((a, b) => a[0] - b[0]).slice(-120)
    .map(([day, value]) => ({ date: isoDay(day), value }));
}

export function exits(): AdvancedExits {
  const rule = wc.exitRule();
  return {
    stop: rule.stop, optional_target: rule.optional_target, with: rule.with, holding: rule.holding,
    by_call: Object.fromEntries(Object.entries(rule.by_call ?? {}).map(([call, v]) => [call, v.all])),
  };
}

/** Where each panel gets its data. A test can replace any of them, for instance with one that fails. */
export interface Sources {
  catalogue: () => Promise<Live>;
  terminal: (stake: number) => Promise<{ positions: Position[] }>;
  week: () => Promise<{ coins: Array<Pick<WeekCoin, "coin" | "side" | "signal">> }>;
  config: () => CallsConfig;
  scoreboard: (stake: number) => Promise<Scoreboard>;
  exits: () => AdvancedExits;
  worksheet: (live: Live) => { rows: WorksheetRow[]; pending: number };
  performance: () => Record<string, number[]>;
  correlation: () => Correlation;
  forex: () => Promise<ForexPair[]>;
  moodHistory: () => Promise<MoodPoint[]>;
}

const SOURCES: Sources = {
  catalogue: md.catalogue,
  terminal: terminal.snapshot,
  week: wc.thisWeek,
  config: wc.callsConfig,
  scoreboard: (stake) => wc.scoreboard(stake, wc.SUGGESTED),
  exits, worksheet, performance, correlation,
  forex: forex.pairs,
  moodHistory,
};

export async function snapshot(stake: number, replaced: Partial<Sources> = {}): Promise<AdvancedSnapshot> {
  const from = { ...SOURCES, ...replaced };
  const now = runtime.now();
  // The markets the basic screen shows come first; the rest of the worksheet queues behind them.
  want(terminal.DASHBOARD);
  want(WORKSHEET);
  const [live, base, week] = await Promise.all([from.catalogue(), from.terminal(stake), from.week()]);
  // The three panels that may wait on the network run side by side.
  const [board, pairs, moods] = await Promise.all([
    soft<Scoreboard | null>(() => from.scoreboard(stake), null),
    soft(from.forex, []),
    soft(from.moodHistory, []),
  ]);
  const sheet = await soft(() => from.worksheet(live), { rows: [], pending: 0 });
  return {
    now: isoMoment(now),
    movers: await soft<AdvancedSnapshot["movers"]>(() => movers(live), {}),
    breadth: await soft<AdvancedSnapshot["breadth"]>(() => breadth(live), {}),
    worksheet: sheet.rows,
    performance: await soft(from.performance, {}),
    correlation: await soft(from.correlation, { labels: [], matrix: [] }),
    signals: await soft(() => signalMatrix(week, from.config()), []),
    leverage: await soft(() => leverageTable(from.config()), { levels: [], rows: [] }),
    risk: await soft(() => risk(base.positions, stake), { rows: [] }),
    scoreboard: board,
    funding: await soft(() => funding(live), []),
    forex: pairs,
    mood_history: moods,
    exits: await soft<AdvancedExits | null>(from.exits, null),
    markets: live.size,
    pending: sheet.pending,
  };
}
