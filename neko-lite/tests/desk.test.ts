// The trend rule replay and per-market trust. Port of ReplayTests and
// TrustTests in service/server/tests/test_desk.py.
//
// The Python test checks the replay against the research backtest on the saved
// BTC and SUI price history. Here the same price files are replayed in
// TypeScript and compared, trade for trade, with what Python's desk.replay and
// research/scripts/strategy_search.py gave (tests/fixtures/make_reference.py).

import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import * as desk from "../src/core/desk";
import type { Bars } from "../src/core/history";
import { H4_MS } from "../src/core/marketData";
import type { Market } from "../src/core/types";
import reference from "./fixtures/reference.json";

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), "../..");

function barsFromCsv(path: string): Bars {
  const rows = readFileSync(path, "utf8").trim().split("\n").slice(1).map((line) => line.split(","));
  const column = (at: number) => Float64Array.from(rows.map((row) => Number(row[at])));
  return { t: Float64Array.from(rows.map((row) => Date.parse(row[0].replace(" ", "T")))),
           open: column(1), high: column(2), low: column(3), close: column(4) };
}

function synthetic(closes: number[]): Bars {
  const c = Float64Array.from(closes);
  return { t: Float64Array.from(closes.map((_, i) => i * H4_MS)), open: c.slice(),
           high: c.map((v) => v * 1.01), low: c.map((v) => v * 0.99), close: c };
}

type Coin = keyof typeof reference.replay;
const coins = Object.keys(reference.replay) as Coin[];
const haveHistory = coins.every((coin) => existsSync(resolve(REPO, reference.replay[coin].csv)));

describe.skipIf(!haveHistory)("replay on the saved price history", () => {
  it("matches Python's replay and the research backtest, trade for trade", () => {
    for (const coin of coins) {
      const ref = reference.replay[coin];
      const bars = barsFromCsv(resolve(REPO, ref.csv));
      expect(bars.t.length).toBe(ref.bars);
      expect(bars.t[0]).toBe(ref.first_bar_ms);
      const mine = desk.replay(bars, 1).trades;
      expect(mine.length).toBe(ref.research_gross.length);
      mine.forEach((trade, i) => expect(trade.gross_pct).toBeCloseTo(ref.research_gross[i], 6));
      // and exactly what Python's desk.replay returns: same trades, same prices, same reasons
      expect(mine).toEqual(ref.trades_from_first_bar);
    }
  });

  it("warmup skips the unsettled start, and the current state is the same", () => {
    for (const coin of coins) {
      const ref = reference.replay[coin];
      const bars = barsFromCsv(resolve(REPO, ref.csv));
      const full = desk.replay(bars, 1).trades;
      const run = desk.replay(bars);
      expect(run.trades.length).toBeLessThan(full.length);
      const firstAllowed = new Date(bars.t[desk.SLOW_BARS]).toISOString().slice(0, 19);
      expect(run.trades.every((t) => t.opened_at.slice(0, 19) >= firstAllowed)).toBe(true);
      expect(run.trades).toEqual(ref.trades_after_warmup);
      const { open, pending, bars: count, trend, trend_days, atr_pct, last_close, last_bar } = run;
      expect({ open, pending, bars: count, trend, trend_days, atr_pct, last_close, last_bar }).toEqual(ref.state);
      expect({ long: desk.trust(run.trades, "long"), short: desk.trust(run.trades, "short") }).toEqual(ref.trust);
    }
  });
});

describe("replay", () => {
  it("fresh cross is pending", () => {
    const closes = Array.from({ length: 400 }, (_, i) => 200 - i * 0.2);
    while (desk.replay(synthetic(closes)).trend === "down") closes.push(closes[closes.length - 1] * 1.01);
    const run = desk.replay(synthetic(closes));
    expect((run.pending as NonNullable<typeof run.pending>).direction).toBe("long");
    expect(run.open).toBeNull();
    const later = desk.replay(synthetic([...closes, closes[closes.length - 1] * 1.01]));
    expect(later.pending).toBeNull();
    expect((later.open as NonNullable<typeof later.open>).dir).toBe(1);
  });

  it("a fresh cross is a new signal at the live price, and an older one becomes active", () => {
    const closes = Array.from({ length: 400 }, (_, i) => 200 - i * 0.2);
    while (desk.replay(synthetic(closes)).trend === "down") closes.push(closes[closes.length - 1] * 1.01);
    const live: Market = { symbol: "X", label: "X", class: "crypto", price: 150, day_change_pct: 0,
                           funding_hourly: 0, max_leverage: 20, day_volume_usd: 1, open_interest_usd: 1 };
    const fresh = desk.marketSignal(live, desk.replay(synthetic(closes)));
    expect([fresh.signal.state, fresh.signal.direction, fresh.signal.entry]).toEqual(["new", "long", 150]);
    expect(fresh.signal.stop as number).toBeLessThan(150);
    for (let i = 0; i < desk.NEW_SIGNAL_BARS + 2; i += 1) closes.push(closes[closes.length - 1] * 1.001);
    const older = desk.marketSignal(live, desk.replay(synthetic(closes)));
    expect([older.signal.state, older.signal.direction]).toEqual(["active", "long"]);
    expect(older.trend).toBe("up");
  });
});

describe("trust", () => {
  const rows = (direction: "long" | "short", results: number[]) => results.map((r) => ({ direction, gross_pct: r }));

  it("few trades are unproven", () => {
    expect(desk.trust(rows("long", [5, 5, 5]), "long").state).toBe("unproven");
  });

  it("losing side is muted and winning side trusted", () => {
    const all = [...rows("short", [-3, -3, -3, -3, 4]), ...rows("long", [-1, -1, -1, -1, 20])];
    expect(desk.trust(all, "short").state).toBe("muted");
    expect(desk.trust(all, "long").state).toBe("trusted");
  });

  it("costs count", () => {
    // five trades that each make less than the round-trip cost are a loss
    expect(desk.trust(rows("long", new Array<number>(5).fill(desk.COST_PCT / 2)), "long").state).toBe("muted");
  });
});
