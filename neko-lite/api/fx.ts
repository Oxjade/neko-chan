// The one server piece of the app: the forex board's figures.
//
// The European Central Bank publishes its daily reference rates as a zip file
// that browsers are not allowed to read from another site, so this small
// function downloads it, works out the weekly picture for each pair, and
// returns only that (a few kilobytes instead of the two-megabyte table).
//
// Port of overview() in service/server/forex_weekly.py. No rule tested picks a
// forex pair's direction better than a coin flip, so nothing here picks one:
// it reports how far each pair normally moves in a week and where it stands.
//
// Runs on Vercel as a Node function; in local dev and preview the Vite config
// calls the same handler. It imports nothing from the rest of the app.

import { inflateRawSync } from "node:zlib";

const ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip";
const DAY_MS = 24 * 3600 * 1000;
const VOL_WEEKS = 26;            // the "normal week" is measured over the last half year
const MIN_WEEKS = 60;

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

const isoDay = (day: number) => new Date(day * DAY_MS).toISOString().slice(0, 10);

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

interface Series {
  days: number[];
  values: number[];
}

/** A pair's price on every day both of its currencies have a rate. */
function pairSeries(daily: DailyRates, pair: string): Series {
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
  let bin: number | null = null;
  series.days.forEach((day, i) => {
    // 1970-01-01 was a Thursday; a week's bin is the Friday on or after the day.
    const weekday = (((day + 3) % 7) + 7) % 7;               // Monday is 0, Friday is 4
    const friday = day + (((4 - weekday) % 7) + 7) % 7;
    if (friday === bin) close[close.length - 1] = series.values[i];
    else close.push(series.values[i]);
    bin = friday;
  });
  const ret = close.map((value, k) => (k === 0 ? NaN : (value / close[k - 1] - 1) * 100));
  const normal = rollingStd(ret, VOL_WEEKS);
  return { close: close.slice(VOL_WEEKS), ret: ret.slice(VOL_WEEKS), normal: normal.slice(VOL_WEEKS) };
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

/** This week's picture for every pair. */
export function overview(csv: string): ForexOverview {
  const daily = parseRates(csv);
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
    pairs.push({
      pair, price, as_of: isoDay(series.days[series.days.length - 1]),
      last_week_pct: weeks.ret[n - 1], normal_week_pct: normal,
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

const json = (body: unknown, status: number, cache: string) => new Response(JSON.stringify(body), {
  status, headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": cache },
});

export async function GET(_request: Request): Promise<Response> {
  try {
    const response = await fetch(ECB_URL, { signal: AbortSignal.timeout(30_000) });
    if (!response.ok) throw new Error(`ECB answered ${response.status}`);
    const board = overview(unzipFirst(new Uint8Array(await response.arrayBuffer())));
    if (!board.pairs.length) throw new Error("no rates in the ECB table");
    // The rates change once a working day: let the CDN answer for an hour, and
    // keep serving the last answer for a day while it fetches a new one.
    return json(board, 200, "public, s-maxage=3600, stale-while-revalidate=86400");
  } catch {
    return json({ error: "Exchange-rate history is unavailable right now." }, 502, "no-store");
  }
}
