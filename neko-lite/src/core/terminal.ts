// The live terminal: model positions for this week's calls, what to do about
// them right now, a recommendation per market, and chart data.
//
// Port of service/server/terminal.py. Everything here is derived from the
// tested rules:
//
//   calls      weeklyCalls (long, short or stay out; suggested leverage)
//   stop       one normal week from entry
//   in profit  hold to Monday. Moving the stop to break-even made no real
//              difference in testing and trailing it closely made results
//              worse, so the terminal never tells you to tighten a stop.
//   target     none by default; a far one (three normal weeks) is optional
//   exit       close at Monday 00:00 UTC and take the new call
//
// The "positions" are model positions: what the user's weekly amount would be
// doing had the calls been followed from Monday's open. The terminal does not
// know what the user actually holds.

import { DAY_MS, HOUR_MS, dayOf, isoMoment, parseDay } from "./dates";
import * as desk from "./desk";
import { settleQuick, want, type Bars } from "./history";
import * as md from "./marketData";
import { pyFixed, pyGrouped, pySum } from "./numeric";
import { DataError, runtime } from "./runtime";
import type {
  Advice, AdviceLevel, AssetClass, ChartData, ChartInterval, Position, PositionStatus, Recommendation, Side,
  TerminalMarket, TerminalSnapshot, TrendDirection, Vote, Week, WeekCoin,
} from "./types";
import * as wc from "./weeklyCalls";

export const DASHBOARD = ["BTC", "ETH", "SOL", "SUI", "xyz:NVDA", "xyz:SP500", "xyz:GOLD", "xyz:EUR"] as const;
export const NEAR_STOP = 0.25;              // warn when less than a quarter of the stop distance is left
export const EMA_DAYS = [10, 40] as const;
export const CHART_BARS: Record<ChartInterval, number> = { "1h": 240, "4h": 240, "1d": 240 };
const BARS_PER_DAY: Record<ChartInterval, number> = { "1h": 24, "4h": 6, "1d": 1 };
const SPARK_BARS = 42;                      // the last week of 4-hour closes
const HISTORY_PATIENCE_MS = 2000;           // how long a snapshot waits for stored history to catch up

function money(v: number): string {
  return `${v < 0 ? "-" : "+"}$${pyGrouped(Math.abs(v), 2)}`;
}

function px(v: number): string {
  return v >= 1000 ? pyGrouped(v, 0) : v >= 10 ? pyGrouped(v, 2) : pyGrouped(v, 4);
}

/** The fields of a called coin that a model position is built from. */
export type CalledCoin = Pick<WeekCoin, "coin" | "name"> & {
  side: Side;
  leverage: number;
  entry: number;
  stop_price?: number | null;
  target_price?: number | null;
  liquidation_price?: number | null;
  normal_week_pct?: number | null;
};

/**
 * Where a model position stands. `days` are this week's daily candles so far
 * (Monday first, the forming day included).
 */
export function positionState(coinRow: CalledCoin, days: Array<{ high: number; low: number }>, price: number,
                              share: number): Position {
  const side = coinRow.side;
  const lev = coinRow.leverage;
  const sign = side === "long" ? 1.0 : -1.0;
  const entry = coinRow.entry;
  const stop = coinRow.stop_price ?? null;
  const target = coinRow.target_price ?? null;
  const liq = coinRow.liquidation_price ?? null;
  let status: PositionStatus = "open";
  let closedAt: number | null = null;
  let targetSeen = false;
  for (const day of days) {
    const worst = side === "long" ? day.low : day.high;
    const best = side === "long" ? day.high : day.low;
    if (liq && (worst - liq) * sign <= 0) {
      status = "liquidated";
      closedAt = liq;
      break;
    }
    if (stop && (worst - stop) * sign <= 0) {
      status = "stopped";
      closedAt = stop;
      break;
    }
    if (target && (best - target) * sign >= 0) targetSeen = true;
  }
  const mark = closedAt || price;
  const move = (mark / entry - 1) * sign;
  const pnl = status === "liquidated" ? -share : share * lev * (move - 2 * wc.FEE_PER_SIDE);
  const out: Position = {
    coin: coinRow.coin, name: coinRow.name, side, leverage: lev,
    share, position_usd: share * lev, entry, last: price,
    stop, target, liquidation: liq, status,
    move_pct: move * 100, pnl_usd: Math.max(pnl, -share), pnl_pct: Math.max(pnl, -share) / share * 100,
    target_seen: targetSeen, normal_week_pct: coinRow.normal_week_pct ?? null,
  };
  if (stop && status === "open") {
    const total = Math.abs(stop - entry);
    const left = (stop - price) * -sign;          // positive while the stop has not been reached
    out.stop_distance_pct = left / price * 100;
    out.stop_room_left = total ? Math.max(0.0, Math.min(1.5, left / total)) : null;
  }
  return out;
}

/** The parts of the week the advice reads. */
export type AdviceWeek = Pick<Week, "week_of" | "next_call"> & {
  coins: Array<Pick<WeekCoin, "coin" | "name" | "side" | "skipped" | "signal">>;
  preview?: { coins: Array<{ name: string; side: Side | null; leverage: number | null }> } | null;
};

/** What to do right now, most urgent first. `now` is in milliseconds. */
export function advice(positions: Position[], week: AdviceWeek, now: number): Advice[] {
  const items: Advice[] = [];
  const add = (level: AdviceLevel, key: string, title: string, text: string, symbol: string | null = null) => {
    items.push({ level, key, title, text, symbol });
  };

  const closeAt = parseDay(week.next_call) * DAY_MS;
  const openedAt = parseDay(week.week_of) * DAY_MS;
  const hoursLeft = (closeAt - now) / 1000 / 3600;
  const livePositions = positions.filter((p) => p.status === "open");

  for (const p of positions) {
    const tag = `${p.side} ${p.name}`;
    if (p.status === "liquidated") {
      add("critical", `liq:${p.coin}`, `${p.name}: liquidated`,
          `The ${tag} was liquidated near ${px(p.liquidation as number)}. That share is gone. Do not re-enter this week.`,
          p.coin);
    } else if (p.status === "stopped") {
      add("critical", `stop:${p.coin}`, `${p.name}: stop hit, trade closed`,
          `The ${tag} hit its stop at ${px(p.stop as number)} (${money(p.pnl_usd)}). It is closed for the week. ` +
          "Do not re-enter or move the stop after the fact.", p.coin);
    } else {
      const room = p.stop_room_left;
      const normal = p.normal_week_pct || 0;
      if (room !== null && room !== undefined && room <= NEAR_STOP) {
        add("warn", `near:${p.coin}`, `${p.name}: close to the stop`,
            `Price ${px(p.last)} is ${pyFixed(p.stop_distance_pct as number, 1)}% from the stop at ${px(p.stop as number)}. ` +
            "Leave the stop where it is and let it work.", p.coin);
      } else if (p.target_seen) {
        add("good", `target:${p.coin}`, `${p.name}: far target reached`,
            `The optional target at ${px(p.target as number)} was reached. If you set it, the trade is closed in ` +
            "profit. If not, the tested plan still holds to Monday.", p.coin);
      } else if (normal && p.move_pct >= normal) {
        add("good", `profit:${p.coin}`, `${p.name}: in profit, hold`,
            `The ${tag} is up ${pyFixed(p.move_pct, 1)}% (${money(p.pnl_usd)}), more than a normal week. ` +
            "Hold to Monday. Moving the stop to break-even made no difference in testing, and trailing it " +
            "closely made results worse.", p.coin);
      } else if (p.move_pct >= 0) {
        add("info", `hold:${p.coin}`, `${p.name}: on track`,
            p.stop
              ? `The ${tag} is up ${pyFixed(p.move_pct, 1)}% (${money(p.pnl_usd)}). No action. Stop at ${px(p.stop)}.`
              : `The ${tag} is up ${pyFixed(p.move_pct, 1)}% (${money(p.pnl_usd)}). No action.`, p.coin);
      } else {
        add("info", `down:${p.coin}`, `${p.name}: down, inside normal swings`,
            p.stop
              ? `The ${tag} is down ${pyFixed(Math.abs(p.move_pct), 1)}% (${money(p.pnl_usd)}), ` +
                `${pyFixed(p.stop_distance_pct as number, 1)}% from the stop. No action: the stop is the exit.`
              : `The ${tag} is down ${pyFixed(Math.abs(p.move_pct), 1)}% (${money(p.pnl_usd)}). No action.`, p.coin);
      }
    }
  }

  if (livePositions.length && hoursLeft > 0 && hoursLeft <= 24) {
    const names = livePositions.map((p) => p.name).join(" and ");
    const minutes = String(Math.trunc(hoursLeft * 60 % 60)).padStart(2, "0");
    add(hoursLeft <= 3 ? "warn" : "info", "close-soon", "Weekly close is coming",
        `Close ${names} at 00:00 UTC Monday, in ${Math.trunc(hoursLeft)}h ${minutes}m. Then take the new call.`);
  }
  if (livePositions.length && now - openedAt > 24 * HOUR_MS) {
    add("info", "late", "Not in yet? Wait for Monday",
        "These trades were opened at Monday's open. Entering part-way through the week was not tested, " +
        "so if you are not already in, skip to the next call.");
  }
  for (const c of week.coins) {
    if (c.side) continue;
    if (c.skipped) {
      add("info", `out:${c.coin}`, `${c.name}: stay out`,
          `The rule points ${c.signal}, but going ${c.signal} on ${c.name} did not hold up in testing.`, c.coin);
    } else {
      add("info", `out:${c.coin}`, `${c.name}: stay out`,
          "Mood and the last four weeks do not point the same way, so there is no call.", c.coin);
    }
  }
  const preview = week.preview;
  if (preview && hoursLeft > 0 && hoursLeft <= 48) {
    const calls = preview.coins.filter((c) => c.side).map((c) => `${c.side} ${c.name} at ${c.leverage}x`);
    add("info", "preview", "Preview of Monday's call",
        calls.length ? `If nothing changes: ${calls.join(" and ")}.` : "If nothing changes: stay out of everything.");
  }
  const order: Record<AdviceLevel, number> = { critical: 0, warn: 1, good: 2, info: 3 };
  return items.map((item, i) => ({ item, i }))
    .sort((a, b) => order[a.item.level] - order[b.item.level] || a.i - b.i)
    .map(({ item }) => item);
}

/** A reading for one market: which way the tested signals lean, and whether there is a call. */
export function recommendation(symbol: string, row: { trend?: TrendDirection | null },
                               week: { coins: Array<Pick<WeekCoin, "coin" | "side" | "skipped" | "signal"
                                 | "mood_greedy" | "four_week_change_pct" | "leverage">> }): Recommendation {
  const coin = week.coins.find((c) => c.coin === symbol) ?? null;
  const votes: Array<[string, number]> = [];
  if (row.trend) votes.push(["10-day against 40-day average", row.trend === "up" ? 1 : -1]);
  if (coin) {
    votes.push(["Market mood", coin.mood_greedy ? 1 : -1]);
    votes.push(["Last four weeks", coin.four_week_change_pct > 0 ? 1 : -1]);
  }
  let score = 0;
  for (const [, v] of votes) score += v;
  const lean = votes.length ? score / votes.length : 0.0;
  let verdict: Recommendation["verdict"];
  let detail: string;
  if (coin && coin.side) {
    verdict = coin.side.toUpperCase() as "LONG" | "SHORT";
    detail = `Called this week at ${coin.leverage}x.`;
  } else if (coin && coin.skipped) {
    verdict = "STAY OUT";
    detail = `Signals lean ${coin.signal}, but that side has not held up here.`;
  } else if (coin) {
    verdict = "STAY OUT";
    detail = "Mood and the last four weeks disagree.";
  } else {
    verdict = "NO CALL";
    detail = "No rule tested has an edge on this market. Shown for reference only.";
  }
  return {
    symbol, lean, verdict, detail, strength: Math.abs(score), of: votes.length,
    votes: votes.map(([name, v]): Vote => ({ name, direction: v > 0 ? "up" : "down" })),
  };
}

// ---- the trend rule on a loaded history ----------------------------------------------

const replays = new WeakMap<Bars, desk.Replay>();

/** The trend rule replayed over a market's history; redone only when the bars change. */
export function replayOf(bars: Bars): desk.Replay {
  let run = replays.get(bars);
  if (!run) {
    run = desk.replay(bars);
    replays.set(bars, run);
  }
  return run;
}

interface DashboardRow {
  symbol: string;
  label: string;
  class: AssetClass;
  price: number | null;
  trend?: TrendDirection;
  trend_days?: number;
  error?: string;
  /** The market's price history is still downloading. */
  loading?: boolean;
}

function dashboardRow(symbol: string, live: md.Catalogue): DashboardRow {
  const market = live.get(symbol);
  if (!market) {
    return { symbol, label: md.label(symbol), class: md.assetClass(symbol), price: null,
             error: `${md.label(symbol)} is not available.` };
  }
  const base = { symbol, label: market.label, class: market.class, price: market.price };
  const history = md.candles(symbol);
  if (history.state === "failed") return { ...base, error: history.error.message };
  if (history.state === "pending") return { ...base, loading: true };
  const run = replayOf(history.bars);
  return { ...base, trend: run.trend, trend_days: run.trend_days };
}

function note(row: DashboardRow, rec: Recommendation): string {
  if (row.error) return row.error;
  // Not in the Python version, which waits for the history before answering.
  if (row.loading) return `Loading price history. ${rec.detail}`;
  return `Trend ${row.trend} ${pyFixed(row.trend_days as number, 0)}d. ${rec.detail}`;
}

export async function snapshot(stake: number): Promise<TerminalSnapshot> {
  const now = runtime.now();
  const [week, live] = await Promise.all([wc.thisWeek(), md.catalogue(), settleQuick(DASHBOARD, HISTORY_PATIENCE_MS)]);
  const coins = Object.keys(wc.callsConfig());
  const share = stake / coins.length;
  const monday = parseDay(week.week_of);
  const called = week.coins.filter((c) => c.side);
  const dailies = await Promise.all(called.map((c) => md.chartCandles(c.coin, "1d", 12)));
  // The long downloads start only now, so they do not slow down what the screen needs first.
  want(DASHBOARD);
  const positions = called.map((c, i) => {
    const price = live.get(c.coin)?.price || (c.price as number);
    const days = dailies[i].filter((d) => dayOf(d.t) >= monday).map((d) => ({ high: d.h, low: d.l }));
    return positionState(c as CalledCoin, days, price, share);
  });

  const markets: TerminalMarket[] = DASHBOARD.map((symbol) => {
    const row = dashboardRow(symbol, live);
    const fresh = live.get(symbol);
    const rec = recommendation(symbol, row, week);
    const history = md.candles(symbol);
    const closes = history.state === "ready" ? Array.from(history.bars.close.slice(-SPARK_BARS)) : [];
    return {
      symbol, label: row.label, class: row.class,
      price: fresh ? fresh.price : row.price, day_change_pct: fresh ? fresh.day_change_pct : null,
      spark: closes, note: note(row, rec), recommendation: rec,
    };
  });
  return {
    now: isoMoment(now), week_of: week.week_of, next_call: week.next_call,
    mood: { week: week.mood, today: week.preview ? week.preview.mood : null },
    stake, positions, week_pnl_usd: pySum(positions.map((p) => p.pnl_usd)),
    advice: advice(positions, week, now), markets,
    stay_out: week.coins.filter((c) => !c.side)
      .map((c) => ({ coin: c.coin, name: c.name, signal: c.signal, skipped: c.skipped })),
  };
}

/** Candles with the two averages the trend rule uses. */
export async function chart(symbol: string, interval: ChartInterval): Promise<ChartData> {
  const perDay = BARS_PER_DAY[interval];
  const shownBars = CHART_BARS[interval];
  const need = shownBars + EMA_DAYS[1] * perDay * 3;            // history for the slow average to settle
  const candles = await md.chartCandles(symbol, interval, Math.min(need, 4900));
  if (!candles.length) throw new DataError(`No chart data for ${md.label(symbol)}.`);
  const closes = candles.map((c) => c.c);
  const shown = candles.slice(-shownBars);
  const emas: ChartData["emas"] = {};
  for (const days of EMA_DAYS) {
    const span = days * perDay;
    if (closes.length < span) continue;
    const k = 2.0 / (span + 1);
    let value = closes[0];
    const series: number[] = [];
    for (const price of closes) {
      value = price * k + value * (1 - k);
      series.push(value);
    }
    const tail: Array<number | null> = series.slice(-shownBars).slice(-shown.length);
    // pad when history is shorter than the window
    emas[`${days}d`] = [...new Array<null>(shown.length - tail.length).fill(null), ...tail];
  }
  return { symbol, label: md.label(symbol), interval, candles: shown, emas };
}
