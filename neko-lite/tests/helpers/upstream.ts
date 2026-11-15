// A stand-in for the outside world: Hyperliquid, the Fear & Greed index and
// the forex function, answering from simple formulas. A candle's values depend
// only on its market and its time, so a request for "the bars since X" agrees
// with an earlier request for the whole history, as the real exchange does.

import type { RawCandle } from "../../src/core/history";
import { FNG_URL, HL_URL, type BarStorage } from "../../src/core/runtime";
import type { StoredBars } from "../../src/core/history";

const HOUR = 3600 * 1000;
const DAY = 24 * HOUR;
export const INTERVALS: Record<string, number> = { "1h": HOUR, "4h": 4 * HOUR, "1d": DAY };

export const MAIN = ["BTC", "ETH", "SOL", "SUI", "HYPE", "XRP", "DOGE", "BNB", "AVAX", "LINK", "ADA", "LTC"];
export const XYZ = ["NVDA", "TSLA", "AAPL", "MSFT", "GOOGL", "META", "AMZN", "AMD", "COIN", "MSTR",
                    "SP500", "XYZ100", "GOLD", "SILVER", "CL", "EUR", "GBP", "JPY"].map((name) => `xyz:${name}`);

export interface Sent {
  url: string;
  payload?: { type: string; dex?: string; coin?: string; req?: { coin: string; interval: string; startTime: number; endTime: number } };
}

export interface Upstream {
  fetch: typeof fetch;
  /** The clock; tests move it forward by assigning to it. */
  now: number;
  sent: Sent[];
  /** Return a status code to make a request fail. */
  failWith: (sent: Sent) => number | null;
  /** Fixed candles for a market and interval, instead of the formula. */
  fixed: Map<string, RawCandle[]>;
  /** When a market was listed: no candles before this. */
  listedSince: Map<string, number>;
  /** Fear & Greed value for a day number. */
  mood: (day: number) => number;
  /** What the forex function answers, or null for "down". */
  forex: unknown | null;
  candleRequests: (interval?: string) => Sent[];
  rowsReturned: number;
}

function seed(symbol: string): number {
  let total = 0;
  for (const ch of symbol) total = (total * 31 + ch.charCodeAt(0)) % 9973;
  return total;
}

/** A market's price at a moment: smooth waves, different for every market. */
export function priceAt(symbol: string, ms: number): number {
  const s = seed(symbol);
  const base = 20 + (s % 400) * 7.5;
  const phase = (s % 100) / 15;
  const days = ms / DAY;
  return base * Math.exp(0.25 * Math.sin(days / 9 + phase) + 0.07 * Math.sin(days / 1.3 + 2 * phase)
    + 0.01 * Math.sin(days * 5.1 + phase));
}

function candle(symbol: string, t: number, ms: number, now: number): RawCandle {
  const o = priceAt(symbol, t);
  const c = priceAt(symbol, Math.min(t + ms, now));
  const h = Math.max(o, c) * 1.004;
  const l = Math.min(o, c) * 0.996;
  const text = (v: number) => v.toPrecision(8);
  return { t, o: text(o), h: text(h), l: text(l), c: text(c), v: String(100 + (t / ms) % 7) };
}

export function label(value: number): string {
  return value < 25 ? "Extreme Fear" : value < 47 ? "Fear" : value < 55 ? "Neutral" : value < 76 ? "Greed" : "Extreme Greed";
}

export function makeUpstream(now: number): Upstream {
  const up: Upstream = {
    fetch: (async () => new Response("unset")) as typeof fetch,
    now, sent: [], failWith: () => null, fixed: new Map(), listedSince: new Map(),
    mood: (day) => Math.round(50 + 30 * Math.sin(day / 11)),
    forex: { pairs: [{ pair: "EURUSD", price: 1.1, as_of: "2026-11-13", last_week_pct: 0.2, normal_week_pct: 1.0,
                       range_low: 1.089, range_high: 1.111, range_held: 0.7, trend_13w: "up", trend_52w: "down" }] },
    candleRequests: (interval) => up.sent.filter((s) => s.payload?.type === "candleSnapshot"
      && (!interval || s.payload.req?.interval === interval)),
    rowsReturned: 0,
  };
  const json = (body: unknown) => new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });

  const catalogue = (dex: string | undefined) => {
    const names = dex ? [...XYZ, "xyz:THIN"] : [...MAIN, "PEPE", "GONE"];
    const universe = names.map((name) => ({ name, maxLeverage: name === "BTC" ? 40 : name === "ETH" ? 25 : 20,
                                            ...(name === "GONE" ? { isDelisted: true } : {}) }));
    const contexts = names.map((name) => ({
      markPx: priceAt(name, up.now).toPrecision(8), prevDayPx: priceAt(name, up.now - DAY).toPrecision(8),
      funding: (((seed(name) % 9) - 4) * 0.000004).toFixed(8), openInterest: String(1000 + seed(name)),
      dayNtlVlm: name === "xyz:THIN" ? "1000.0" : String(5_000_000 + seed(name) * 100),
    }));
    return [{ universe }, contexts];
  };

  const candles = (req: NonNullable<Sent["payload"]>["req"] & object) => {
    const ms = INTERVALS[req.interval];
    const fixed = up.fixed.get(`${req.coin}:${req.interval}`);
    let rows: RawCandle[];
    if (fixed) {
      rows = fixed.filter((row) => row.t + ms > req.startTime && row.t <= req.endTime);
    } else {
      const since = up.listedSince.get(req.coin) ?? -Infinity;
      rows = [];
      for (let t = Math.floor(req.startTime / ms) * ms; t <= req.endTime && t <= up.now; t += ms) {
        if (t >= since) rows.push(candle(req.coin, t, ms, up.now));
      }
    }
    rows = rows.slice(-5001);
    up.rowsReturned += rows.length;
    return rows;
  };

  up.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const sent: Sent = { url };
    if (url === HL_URL) sent.payload = JSON.parse(String(init?.body));
    up.sent.push(sent);
    const status = up.failWith(sent);
    if (status) return new Response("no", { status });
    if (url === HL_URL) {
      const payload = sent.payload as NonNullable<Sent["payload"]>;
      if (payload.type === "metaAndAssetCtxs") return json(catalogue(payload.dex));
      if (payload.type === "candleSnapshot" && payload.req) return json(candles(payload.req));
      if (payload.type === "l2Book") {
        const mid = priceAt(payload.coin as string, up.now);
        const side = (sign: number) => Array.from({ length: 20 }, (_, i) =>
          ({ px: (mid * (1 + sign * (i + 1) * 0.0002)).toPrecision(8), sz: String(1 + i), n: 1 }));
        return json({ coin: payload.coin, time: up.now, levels: [side(-1), side(1)] });
      }
      return new Response("unknown", { status: 422 });
    }
    if (url.startsWith(FNG_URL)) {
      const limit = Number(new URL(url).searchParams.get("limit") ?? 1);
      const today = Math.floor(up.now / DAY);
      const data = Array.from({ length: limit }, (_, i) => {
        const value = up.mood(today - i);
        return { value: String(value), value_classification: label(value), timestamp: String((today - i) * 86400) };
      });
      return json({ name: "Fear and Greed Index", data });
    }
    if (url === "/api/fx") return up.forex ? json(up.forex) : new Response("down", { status: 502 });
    return new Response("not found", { status: 404 });
  }) as typeof fetch;
  return up;
}

/** Storage that keeps bars in a Map, like IndexedDB but in memory. */
export function memoryStorage(): BarStorage & { held: Map<string, StoredBars> } {
  const held = new Map<string, StoredBars>();
  return {
    held,
    get: async (key) => held.get(key) ?? null,
    put: async (key, bars) => { held.set(key, bars); },
  };
}

/**
 * Candle rows from a list of [open, high, low, close], oldest first. By default
 * the last row is the bar forming now; with `closed` it is the last finished bar.
 */
export function candleRows(now: number, interval: string, ohlc: Array<[number, number, number, number]>,
                           closed = false): RawCandle[] {
  const ms = INTERVALS[interval];
  const last = Math.floor(now / ms) * ms - (closed ? ms : 0);
  return ohlc.map(([o, h, l, c], i) => ({
    t: last - (ohlc.length - 1 - i) * ms, o: String(o), h: String(h), l: String(l), c: String(c), v: "1.0",
  }));
}
