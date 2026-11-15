// The forex function (api/fx.ts): reading the ECB's zip and working out the board.
// The expected board comes from Python's forex_weekly.overview() on the same
// made-up table (tests/fixtures/make_reference.py).

import { deflateRawSync } from "node:zlib";
import { afterEach, describe, expect, it, vi } from "vitest";

import { GET, overview, parseRates, unzipFirst } from "../api/fx";
import reference from "./fixtures/reference.json";

/** A zip archive holding one file, built by hand so the reader is tested against the format itself. */
function zipOf(name: string, text: string, compress = true): Uint8Array<ArrayBuffer> {
  const raw = Buffer.from(text, "utf8");
  const data = compress ? deflateRawSync(raw) : raw;
  const file = Buffer.from(name, "utf8");
  const local = Buffer.alloc(30);
  local.writeUInt32LE(0x04034b50, 0);
  local.writeUInt16LE(compress ? 8 : 0, 8);
  local.writeUInt32LE(data.length, 18);
  local.writeUInt32LE(raw.length, 22);
  local.writeUInt16LE(file.length, 26);
  const central = Buffer.alloc(46);
  central.writeUInt32LE(0x02014b50, 0);
  central.writeUInt16LE(compress ? 8 : 0, 10);
  central.writeUInt32LE(data.length, 20);
  central.writeUInt32LE(raw.length, 24);
  central.writeUInt16LE(file.length, 28);
  central.writeUInt32LE(0, 42);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(1, 8);
  end.writeUInt16LE(1, 10);
  end.writeUInt32LE(central.length + file.length, 12);
  end.writeUInt32LE(local.length + file.length + data.length, 16);
  return new Uint8Array(Buffer.concat([local, file, data, central, file, end]));
}

describe("the forex function", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("reads the first file out of a zip, compressed or stored", () => {
    const text = "Date,USD,\n2026-11-13,1.1052,\n";
    expect(unzipFirst(zipOf("eurofxref-hist.csv", text))).toBe(text);
    expect(unzipFirst(zipOf("eurofxref-hist.csv", text, false))).toBe(text);
    expect(() => unzipFirst(new Uint8Array(40))).toThrow();
  });

  it("reads the ECB table: oldest first, N/A as missing, the trailing comma ignored", () => {
    const rates = parseRates("Date,USD,JPY,\n2026-11-13,1.1052,176.83,\n2026-11-12,N/A,175.5,\n");
    expect(rates.days.length).toBe(2);
    expect(rates.days[1] - rates.days[0]).toBe(1);
    expect(Object.keys(rates.columns)).toEqual(["USD", "JPY"]);
    expect(Number.isNaN(rates.columns.USD[0])).toBe(true);
    expect(rates.columns.USD[1]).toBe(1.1052);
    expect(rates.columns.JPY[0]).toBe(175.5);
  });

  it("works out the same board as Python, pair by pair", () => {
    const mine = overview(reference.forex.csv);
    const theirs = reference.forex.overview;
    expect(mine.as_of).toBe(theirs.as_of);
    expect(mine.history_from).toBe(theirs.history_from);
    // pairs of a currency with too little history are left out, as in Python
    expect(mine.pairs.map((p) => p.pair)).toEqual(theirs.pairs.map((p) => p.pair));
    expect(mine.pairs.map((p) => p.pair)).not.toContain("NZDUSD");
    mine.pairs.forEach((pair, i) => {
      const want = theirs.pairs[i] as unknown as Record<string, number | string>;
      for (const [key, value] of Object.entries(pair)) {
        if (typeof value === "number") {
          expect(Math.abs(value - (want[key] as number)), `${pair.pair} ${key}`)
            .toBeLessThanOrEqual(1e-9 * Math.abs(want[key] as number));
        } else {
          expect(value, `${pair.pair} ${key}`).toBe(want[key]);
        }
      }
    });
  });

  it("answers with the board and lets the CDN keep it for an hour", async () => {
    vi.stubGlobal("fetch", async () => new Response(zipOf("eurofxref-hist.csv", reference.forex.csv)));
    const response = await GET(new Request("http://localhost/api/fx"));
    expect(response.status).toBe(200);
    expect(response.headers.get("Cache-Control")).toBe("public, s-maxage=3600, stale-while-revalidate=86400");
    const body = await response.json() as { pairs: unknown[] };
    expect(body.pairs.length).toBe(reference.forex.overview.pairs.length);
    expect(JSON.stringify(body).length).toBeLessThan(6000);          // a few kilobytes, not the 2 MB table
  });

  it("says so, without caching, when the ECB cannot be reached", async () => {
    vi.stubGlobal("fetch", async () => { throw new Error("offline"); });
    const response = await GET(new Request("http://localhost/api/fx"));
    expect(response.status).toBe(502);
    expect(response.headers.get("Cache-Control")).toBe("no-store");
    expect(await response.json()).toEqual({ error: "Exchange-rate history is unavailable right now." });
  });
});
