// Market data: prices and candles from Hyperliquid's public API.
//
// Covers crypto perpetuals on the main exchange and the stock, index, forex
// and commodity perpetuals on its "xyz" venue. Port of service/server/market_data.py;
// the difference is in how often the network is used, not in what comes back.

import { cached } from "./cache";
import { BAR_MS, historyStatus, type Bars, type RawCandle } from "./history";
import { backgroundBusy, candleWeight, info } from "./hyperliquid";
import { DataError, onReset, runtime } from "./runtime";
import type { AssetClass, Candle, ChartInterval, Market, OrderBook } from "./types";

export const CRYPTO = ["BTC", "ETH", "SOL", "SUI", "HYPE", "XRP", "DOGE", "BNB", "AVAX", "LINK", "ADA", "LTC"] as const;
const XYZ = "xyz";
const FOREX = new Set(["EUR", "GBP", "JPY"]);
const INDICES = new Set(["SP500", "XYZ100", "JP225", "KR200", "NIFTY", "MAGS", "SMH", "XBI", "XLE", "TLT",
                         "EWJ", "EWT", "EWY", "EWZ", "SOXL", "KORU", "URNM"]);
const COMMODITIES = new Set(["GOLD", "SILVER", "COPPER", "PLATINUM", "PALLADIUM", "CL", "BRENTOIL", "NATGAS",
                             "HO", "ALUMINIUM"]);
export const CLASSES: readonly AssetClass[] = ["crypto", "stocks", "indices", "forex", "commodities"];

export const H4_MS = BAR_MS["4h"];
export const MIN_BARS = 242;
/** Prices also arrive over the live stream, so the catalogue is asked for at most this often. */
export const CATALOGUE_SECONDS = 10;
const CATALOGUE_LOADING_SECONDS = 30;
const CHART_SECONDS = 20;
const BOOK_SECONDS = 2;

export const INTERVAL_MS: Record<ChartInterval, number> = { "1h": 3600 * 1000, "4h": H4_MS, "1d": 24 * 3600 * 1000 };

export function assetClass(symbol: string): AssetClass {
  if (!symbol.includes(":")) return "crypto";
  const name = symbol.slice(symbol.indexOf(":") + 1);
  if (FOREX.has(name)) return "forex";
  if (COMMODITIES.has(name)) return "commodities";
  if (INDICES.has(name)) return "indices";
  return "stocks";
}

export function label(symbol: string): string {
  const at = symbol.indexOf(":");
  return at < 0 ? symbol : symbol.slice(at + 1);
}

/** Every market, keyed by symbol in the exchange's own order. */
export type Catalogue = Map<string, Market>;

interface RawAsset {
  name: string;
  isDelisted?: boolean;
  maxLeverage?: number;
}

interface RawContext {
  markPx?: string | null;
  prevDayPx?: string | null;
  funding?: string | null;
  dayNtlVlm?: string | null;
  openInterest?: string | null;
}

type MetaAndContexts = [{ universe: RawAsset[] }, RawContext[]];

/** Every market the terminal can show, with its live price. */
export function catalogue(): Promise<Catalogue> {
  // While long histories are downloading, the allowance is better spent on them.
  return cached("catalogue", backgroundBusy() ? CATALOGUE_LOADING_SECONDS : CATALOGUE_SECONDS, async () => {
    const crypto = new Set<string>(CRYPTO);
    const venues = await Promise.all(["", XYZ].map((dex) =>
      info<MetaAndContexts>(dex ? { type: "metaAndAssetCtxs", dex } : { type: "metaAndAssetCtxs" }, { weight: 20 })));
    const out: Catalogue = new Map();
    venues.forEach(([meta, contexts], venue) => {
      const count = Math.min(meta.universe.length, contexts.length);
      for (let i = 0; i < count; i += 1) {
        const asset = meta.universe[i];
        const ctx = contexts[i];
        if (asset.isDelisted || !ctx.markPx) continue;
        if (venue === 0 && !crypto.has(asset.name)) continue;
        const price = Number(ctx.markPx);
        const prev = ctx.prevDayPx ? Number(ctx.prevDayPx) : null;
        out.set(asset.name, {
          symbol: asset.name, label: label(asset.name), class: assetClass(asset.name),
          price,
          day_change_pct: prev ? (price / prev - 1) * 100 : null,
          funding_hourly: Number(ctx.funding || 0),
          max_leverage: Math.trunc(Number(asset.maxLeverage || 1)),
          day_volume_usd: Number(ctx.dayNtlVlm || 0),
          open_interest_usd: Number(ctx.openInterest || 0) * price,
        });
      }
    });
    return out;
  });
}

// ---- chart candles ---------------------------------------------------------------

interface ChartSeries {
  rows: Candle[];
  loadedAt: number;
  loading: Promise<Candle[]> | null;
}

let charts = new Map<string, ChartSeries>();
onReset(() => { charts = new Map(); });

function toCandle(row: RawCandle): Candle {
  return { t: Number(row.t), o: Number(row.o), h: Number(row.h), l: Number(row.l), c: Number(row.c),
           v: Number(row.v) };
}

/**
 * Recent candles for a chart, oldest first, including the one still forming.
 *
 * The first request for a chart downloads the whole window. After that only
 * the newest candles are asked for (closed candles do not change) and joined
 * to the ones already held, so the result is what a full request would return.
 */
export function chartCandles(symbol: string, interval: ChartInterval, bars: number): Promise<Candle[]> {
  const ms = INTERVAL_MS[interval];
  if (!ms) return Promise.reject(new DataError("Unknown chart interval."));
  const key = `${symbol}:${interval}:${bars}`;
  let held = charts.get(key);
  if (!held) {
    held = { rows: [], loadedAt: -Infinity, loading: null };
    charts.set(key, held);
  }
  const series = held;
  const now = runtime.now();
  if (now - series.loadedAt < CHART_SECONDS * 1000) return Promise.resolve(series.rows);
  if (series.loading) return series.loading;

  const start = now - bars * ms;
  const closed = series.rows.filter((row) => row.t + ms <= now);
  const last = closed.length ? closed[closed.length - 1].t : null;
  const from = last !== null && last + ms > start ? last : start;
  const request = info<RawCandle[]>(
    { type: "candleSnapshot", req: { coin: symbol, interval, startTime: from, endTime: now } },
    { weight: candleWeight((now - from) / ms + 1) },
  ).then((raw) => {
    if (!Array.isArray(raw)) throw new DataError("Market data is unavailable right now (ValueError).");
    const fresh = raw.map(toCandle);
    const firstFresh = fresh.length ? fresh[0].t : Infinity;
    const joined = from === start ? fresh : [...closed.filter((row) => row.t < firstFresh), ...fresh];
    // Hyperliquid includes the candle that contains the start time; keep exactly that window.
    const rows = joined.filter((row) => row.t + ms > start);
    if (charts.get(key) === series) {
      series.rows = rows;
      series.loadedAt = now;
    }
    return rows;
  }).finally(() => {
    if (series.loading === request) series.loading = null;
  });
  series.loading = request;
  return request;
}

/** Best bids and asks with sizes, for the terminal's fallback when the stream is down. */
export function orderBook(symbol: string, depth = 12): Promise<OrderBook> {
  return cached(`book:${symbol}:${depth}`, BOOK_SECONDS, async () => {
    const raw = await info<{ levels?: Array<Array<{ px: string; sz: string }>>; time?: number }>(
      { type: "l2Book", coin: symbol }, { weight: 2 });
    const [bids, asks] = [...(raw.levels ?? [[], []]), [], []];
    const side = (rows: Array<{ px: string; sz: string }>) =>
      rows.slice(0, depth).map((row) => ({ px: Number(row.px), sz: Number(row.sz) }));
    return { symbol, bids: side(bids), asks: side(asks), time: raw.time ?? null };
  });
}

// ---- four-hour history -----------------------------------------------------------

export type CandleStatus =
  | { state: "ready"; bars: Bars }
  | { state: "pending" }
  | { state: "failed"; error: DataError };

/**
 * Closed four-hour bars for a market, oldest first, if they have been loaded.
 * History is downloaded in the background (see history.ts), so this never waits.
 */
export function candles(symbol: string): CandleStatus {
  const status = historyStatus(symbol);
  if (status.state === "ready" && status.bars.t.length < MIN_BARS) {
    return { state: "failed",
             error: new DataError(`${label(symbol)} has too little price history for a signal yet.`) };
  }
  return status;
}
