// What "live" means on the terminal, as plain functions with no page in them:
// where prices are coming from, when they have stopped, and how the figures
// that hang off a price are worked out again from a live one.
//
// Nothing here invents a number. Every result is a price the exchange sent,
// or arithmetic on one. (The same file, untyped, serves the Python version:
// web/assets/terminal_live.js.)

import type { AdvancedSnapshot, AssetClass, FundingRow, Position, Risk, UniverseMarket } from "../core/api";

export const STALE_MS = 5000;        // with no price for this long, there are no live prices
export const GRACE_MS = 1500;        // what the stream gets to connect before prices are asked for directly
export const FAST_MS = 1000;         // how often prices are asked for directly
export const FAST_MAX_MS = 10000;    // and how far that backs off when the exchange does not answer
export const FAST_FRESH_MS = 3000;   // a direct answer this recent counts as "every second"
export const SILENT_MS = 10000;      // a stream that says nothing for this long is treated as broken
export const PANEL_MS = 500;         // panels are rebuilt and re-ranked at most this often
export const MIN_VOLUME_USD = 250000;  // movers ignore markets too thin for their day change to mean much
export const CLASSES: AssetClass[] = ["crypto", "stocks", "indices", "forex", "commodities"];
const ROUND_TRIP_FEE = 0.0009;       // the exchange's fee, in and out, as a share of the position

export type Stream = "connecting" | "live" | "poll";
export type Feed = "live" | "fast" | "poll" | "down";
/** "" is the exchange's main list (crypto); "xyz" its stocks, indices, forex and commodities. */
export type Dex = "" | "xyz";

/**
 * Which exchanges' prices to ask for directly, once a second.
 *
 * Without the stream: all of them, so the screen keeps moving. With it, on the
 * advanced screen: only the stock, index, forex and commodity markets, because
 * the stream is followed for the worksheet's markets alone and the movers and
 * breadth count every market. Nothing while the tab is hidden.
 */
export function directPrices(stream: Stream, hidden: boolean, notLiveForMs: number, mode: "basic" | "advanced"): Dex[] {
  if (hidden) return [];
  if (stream !== "live") return notLiveForMs >= GRACE_MS ? ["", "xyz"] : [];
  return mode === "advanced" ? ["xyz"] : [];
}

/** The wait before asking again: a second after an answer, doubling after each failure. */
export function nextDirectWait(previousWait: number, ok: boolean): number {
  return ok ? FAST_MS : Math.min(Math.max(previousWait, FAST_MS) * 2, FAST_MAX_MS);
}

/** True when the last price is too old to call live (or there never was one). */
export function isStale(lastAt: number, now: number): boolean {
  return !(lastAt > 0 && now - lastAt <= STALE_MS);
}

/** How prices are reaching the page: "live" (the stream), "fast" (asked each second), "poll" or "down". */
export function feedState(stream: Stream, directAt: number, lastPollAt: number, now: number, pollMs: number): Feed {
  if (stream === "live") return "live";
  if (directAt > 0 && now - directAt <= FAST_FRESH_MS) return "fast";
  return now - lastPollAt < pollMs * 3 ? "poll" : "down";
}

/** Messages a second, averaged over the last few seconds' counts. */
export function updatesPerSecond(counts: number[]): number {
  if (!counts.length) return 0;
  return Math.round(counts.reduce((sum, n) => sum + n, 0) / counts.length);
}

/** Decimal places in a number as the exchange wrote it ("1834.35" has 2). */
export function decimalsOf(text: string | number): number {
  const s = String(text);
  const at = s.indexOf(".");
  return at < 0 ? 0 : s.length - at - 1;
}

/** A price with exactly this many decimals, and thousands separators. */
export function exact(value: number | string | null | undefined, decimals: number): string {
  const v = Number(value);
  if (value === null || value === undefined || Number.isNaN(v)) return "–";
  return v.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

/** The change, in percent, from a reference price to a price; null when either is missing. */
export function changeFrom(price: number | null | undefined, reference: number | null | undefined): number | null {
  return (price as number) > 0 && (reference as number) > 0 ? ((price as number) / (reference as number) - 1) * 100 : null;
}

/** A model position re-priced at a live price, by the core's own arithmetic. */
export function livePosition(p: Position, last: number | null | undefined): Position {
  if (p.status !== "open" || !((last as number) > 0)) return p;
  const price = last as number;
  const sign = p.side === "long" ? 1 : -1;
  const move = (price / p.entry - 1) * sign;
  const pnl = Math.max(p.share * p.leverage * (move - ROUND_TRIP_FEE), -p.share);
  const out: Position = { ...p, last: price, move_pct: move * 100, pnl_usd: pnl, pnl_pct: pnl / p.share * 100 };
  if (p.stop) {
    const left = (p.stop - price) * -sign;
    out.stop_distance_pct = left / price * 100;
    out.stop_room_left = Math.max(0, Math.min(1.5, left / Math.abs(p.stop - p.entry)));
  }
  return out;
}

/** The risk panel's rows with profit and distances at live prices; the totals do not depend on price. */
export function liveRisk(risk: Risk, positions: Position[]): Risk {
  if (!risk || !risk.rows) return risk;
  const byCoin = new Map(positions.map((p) => [p.coin, p]));
  return { ...risk, rows: risk.rows.map((row) => {
    const p = byCoin.get(row.coin);
    if (!p || p.status !== "open") return row;
    return { ...row, pnl_usd: p.pnl_usd,
             stop_distance_pct: p.stop_distance_pct === undefined ? row.stop_distance_pct : p.stop_distance_pct,
             liquidation_distance_pct: p.liquidation && p.last ? Math.abs(p.liquidation - p.last) / p.last * 100
               : row.liquidation_distance_pct };
  }) };
}

/** A catalogue market with its live price and today's change from it. */
export interface LiveMarket extends UniverseMarket {
  day_change_pct: number | null;
}

type Lookup = (symbol: string) => number | null | undefined;

/** Every market with its live price and today's change from it. */
export function liveMarkets(universe: UniverseMarket[], priceOf: Lookup, prevOf?: Lookup): LiveMarket[] {
  return universe.map((m) => {
    const price = priceOf(m.symbol) || m.price;
    const prev = (prevOf && prevOf(m.symbol)) || m.prev_day_price;
    return { ...m, price, day_change_pct: changeFrom(price, prev) };
  });
}

/** Most up and most down today, per asset class (the core's movers, on live figures). */
export function liveMovers(markets: LiveMarket[]): AdvancedSnapshot["movers"] {
  const out: AdvancedSnapshot["movers"] = {};
  for (const cls of CLASSES) {
    const rows = markets.filter((r) => r.class === cls && r.day_change_pct !== null
      && r.day_volume_usd >= MIN_VOLUME_USD) as Array<LiveMarket & { day_change_pct: number }>;
    rows.sort((a, b) => a.day_change_pct - b.day_change_pct);
    const pick = (r: LiveMarket & { day_change_pct: number }) =>
      ({ symbol: r.symbol, label: r.label, price: r.price, day_change_pct: r.day_change_pct });
    // with only a couple of markets in a class, the same one must not appear on both sides
    out[cls] = { up: rows.slice(-3).reverse().filter((r) => r.day_change_pct > 0).map(pick),
                 down: rows.slice(0, 3).filter((r) => r.day_change_pct < 0).map(pick),
                 count: rows.length };
  }
  return out;
}

/** How many markets are up, down or flat today, per asset class (the core's breadth, on live figures). */
export function liveBreadth(markets: LiveMarket[]): AdvancedSnapshot["breadth"] {
  const out: AdvancedSnapshot["breadth"] = {};
  for (const cls of CLASSES) {
    const changes = markets.filter((r) => r.class === cls && r.day_change_pct !== null)
      .map((r) => r.day_change_pct as number);
    out[cls] = { up: changes.filter((c) => c > 0.1).length, down: changes.filter((c) => c < -0.1).length,
                 flat: changes.filter((c) => Math.abs(c) <= 0.1).length };
  }
  return out;
}

/** A market's own figures from the stream. */
export interface MarketContext {
  funding: number;          // hourly rate
  openInterest: number;     // in units of the market
  dayVolume: number;        // in dollars
}

/** The funding panel's rows from the stream's own figures, most extreme funding first. */
export function liveFunding(rows: FundingRow[], contextOf: (symbol: string) => MarketContext | undefined,
                            priceOf: Lookup): FundingRow[] {
  return rows.map((row) => {
    const ctx = contextOf(row.symbol);
    if (!ctx) return row;
    const price = priceOf(row.symbol) as number;
    return { ...row, funding_apr: ctx.funding * 24 * 365 * 100,
             open_interest_usd: price > 0 ? ctx.openInterest * price : row.open_interest_usd,
             day_volume_usd: ctx.dayVolume > 0 ? ctx.dayVolume : row.day_volume_usd };
  }).sort((a, b) => Math.abs(b.funding_apr) - Math.abs(a.funding_apr));
}

/** The thirty-day lines with one more point: where the live price puts each market now. */
export function livePerformance(series: Record<string, number[]>,
                                starts: Record<string, { symbol: string; price: number }> | undefined,
                                priceOf: Lookup): Record<string, number[]> {
  const out: Record<string, number[]> = {};
  for (const [name, points] of Object.entries(series)) {
    const start = starts && starts[name];
    const price = start ? priceOf(start.symbol) as number : 0;
    out[name] = start && price > 0 ? [...points, price / start.price * 100] : points;
  }
  return out;
}
