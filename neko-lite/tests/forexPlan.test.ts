// The forex planner as the screens use it: getForexOverview and getForexPlan.
//
// The odds come from the server function (api/fx.ts), which is run here on a
// made-up ECB table; the position size is worked out in the core. Every
// answer is compared with Python's forex_weekly on the same table
// (tests/fixtures/make_reference.py), and the cases of PlanTests in
// service/server/tests/test_forex_weekly.py are repeated on it.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GET, forgetRates, setClock } from "../api/fx";
import { DataError, configure, getForexOverview, getForexPlan } from "../src/core/api";
import reference from "./fixtures/reference.json";
import { zipOf } from "./helpers/zip";

const NOW = reference.forex.now_ms;                         // Wednesday 11 November 2026, the table's last day
const zip = zipOf("eurofxref-hist.csv", reference.forex.csv);

describe("the forex planner", () => {
  let asked: string[] = [];
  let clock = NOW;
  /** The ECB file the function downloads. */
  let table = zip;
  /** What the function answers; replaced to make it fail. */
  let answer: (url: string) => Promise<Response>;

  beforeEach(() => {
    asked = [];
    clock = NOW;
    table = zip;
    forgetRates();
    setClock(() => clock);                                           // the function goes by the same date
    vi.stubGlobal("fetch", async () => new Response(table));        // the function's own download of the ECB file
    answer = (url) => GET(new Request(`http://localhost${url}`));
    configure({
      fetch: (async (input: RequestInfo | URL) => {
        asked.push(String(input));
        return answer(String(input));
      }) as typeof fetch,
      now: () => clock, storage: null, fxUrl: "/api/fx",
    });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    setClock(null);
  });

  it("the week in progress is reported apart from last week", async () => {
    // The table cut back to Friday 6 November, seen on the Saturday: nothing has traded since.
    const lines = reference.forex.csv.split("\n");
    const friday = [lines[0], ...lines.slice(1).filter((line) => line && line.slice(0, 10) <= "2026-11-06"), ""];
    table = zipOf("eurofxref-hist.csv", friday.join("\n"));
    clock = Date.UTC(2026, 10, 7, 12);
    const before = await getForexOverview();
    expect(before.as_of).toBe("2026-11-06");
    for (const row of before.pairs) expect([row.this_week_pct, row.this_week_pips]).toEqual([null, null]);
    const planFriday = await getForexPlan("EURUSD", "long", 50, 100, 1000, 1);

    // Monday's fixing arrives: the euro is half a percent lower against everything.
    const cells = friday[1].split(",");
    const monday = ["2026-11-09", ...cells.slice(1).map((cell) => (Number(cell) > 0 ? (Number(cell) * 0.995).toFixed(6) : cell))];
    table = zipOf("eurofxref-hist.csv", [friday[0], monday.join(","), ...friday.slice(1)].join("\n"));
    clock = Date.UTC(2026, 10, 9, 16);
    forgetRates();
    configure({ fetch: (async (input: RequestInfo | URL) => answer(String(input))) as typeof fetch,
                now: () => clock, storage: null, fxUrl: "/api/fx" });
    const now = await getForexOverview();
    expect(now.as_of).toBe("2026-11-09");
    const was = new Map(before.pairs.map((row) => [row.pair, row]));
    for (const row of now.pairs) {
      const old = was.get(row.pair) as (typeof before.pairs)[number];
      // everything measured per week still rests on the week that ended on Friday
      expect([row.last_week_pct, row.last_week_pips, row.normal_week_pct, row.range_held, row.trend_13w, row.trend_52w])
        .toEqual([old.last_week_pct, old.last_week_pips, old.normal_week_pct, old.range_held, old.trend_13w, old.trend_52w]);
      // and the new fixing shows as this week so far, against Friday's close
      expect(row.this_week_pct as number).toBeCloseTo((row.price / old.price - 1) * 100, 9);
      expect(row.this_week_pips as number).toBeCloseTo((row.price - old.price) / row.pip_size, 6);
    }
    const eurusd = now.pairs.find((row) => row.pair === "EURUSD") as (typeof now.pairs)[number];
    expect(eurusd.this_week_pct as number).toBeCloseTo(-0.5, 3);
    expect(now.pairs.find((row) => row.pair === "GBPUSD")?.this_week_pct as number).toBeCloseTo(0.0, 3);   // both legs moved together
    // the plan enters at Monday's fixing, and the unfinished week is not one of its past weeks
    const planMonday = await getForexPlan("EURUSD", "long", 50, 100, 1000, 1);
    expect(planMonday.price).toBe(eurusd.price);
    expect(planMonday.all.weeks).toBe(planFriday.all.weeks);
    expect(planMonday.normal_week_pct).toBe(planFriday.normal_week_pct);
    expect(planMonday.trend_13w).toBe(planFriday.trend_13w);
  });

  it("the overview is Python's, field for field and in the same order", async () => {
    const mine = await getForexOverview();
    expect(JSON.stringify(mine)).toBe(JSON.stringify(reference.forex.overview));
    for (const row of mine.pairs) {
      // a normal week in pips is the same distance as the percent, at the latest price
      expect(row.normal_week_pips * row.pip_size).toBeCloseTo(row.price * row.normal_week_pct / 100, 9);
      expect(row.last_week_pips > 0).toBe(row.last_week_pct > 0);
    }
    await getForexOverview();
    expect(asked).toEqual(["/api/fx"]);                              // kept for an hour
  });

  it("every reference plan is Python's, field for field and in the same order", async () => {
    for (const { args, plan } of reference.forex.plans) {
      const [pair, direction, stop, target, account, risk, spread] = args as [string, string, number, number, number, number, number];
      const mine = await getForexPlan(pair, direction, stop, target, account, risk, spread);
      expect(JSON.stringify(mine), args.join(" ")).toBe(JSON.stringify(plan));
    }
  });

  it("plan prices and sizing", async () => {
    const p = await getForexPlan("EURUSD", "short", 50, 100, 1000, 1);
    expect(p.stop_price).toBeCloseTo(p.price + 0.0050, 7);           // a short's stop is 50 pips above
    expect(p.target_price).toBeCloseTo(p.price - 0.0100, 7);         // and its target 100 pips below
    expect([p.pip_size, p.stop_pips, p.target_pips, p.spread_pips]).toEqual([0.0001, 50, 100, 0]);
    expect(p.stop_pct).toBeCloseTo(0.005 / p.price * 100, 7);
    expect(p.normal_week_pips * 0.0001).toBeCloseTo(p.price * p.normal_week_pct / 100, 7);
    expect(p.stop_in_weeks).toBeCloseTo(50 / p.normal_week_pips, 7);
    expect(p.sizing.loss_at_stop_usd).toBeCloseTo(10.0, 6);
    expect(p.sizing.gain_at_target_usd).toBeCloseTo(20.0, 6);
    expect(p.cost_pct).toBe(0.0);
    // a pip on a pair quoted in dollars is worth 10 dollars a lot, and the stop is pips times pip value
    expect(p.sizing.pip_value_per_lot_usd).toBeCloseTo(10.0, 7);
    expect(p.sizing.pip_value_usd).toBeCloseTo(10.0 / 50, 7);
    expect(p.sizing.pip_value_usd).toBeCloseTo(p.sizing.lots * 10.0, 7);
    expect(p.sizing.loss_at_stop_usd).toBe(50 * p.sizing.pip_value_usd);
    expect(p.sizing.gain_at_target_usd).toBe(100 * p.sizing.pip_value_usd);
    for (const part of [p.all, p.with_trend, p.against_trend]) {
      expect(part.avg_result_pips * 0.0001).toBeCloseTo(p.price * part.avg_result_pct / 100, 7);
      expect((part.week_ended_avg_pips as number) * 0.0001)
        .toBeCloseTo(p.price * (part.week_ended_avg_pct as number) / 100, 7);
    }
    const costly = await getForexPlan("EURUSD", "short", 50, 100, 1000, 1, 2);
    expect(costly.all.avg_result_pct).toBeLessThan(p.all.avg_result_pct);
    expect(costly.all.avg_result_pips).toBeLessThan(p.all.avg_result_pips);
    // the spread is paid on top of the stop and taken off the target; the risk stays 1% of the account
    expect(costly.sizing.loss_at_stop_usd).toBeCloseTo(10.0, 6);
    expect(costly.sizing.loss_at_stop_usd).toBe(52 * costly.sizing.pip_value_usd);
    expect(costly.sizing.gain_at_target_usd).toBe(98 * costly.sizing.pip_value_usd);
    expect(costly.cost_pct).toBeCloseTo(0.0002 / p.price * 100, 7);
    expect(p.sizing.units * reference.forex.base_in_usd.EURUSD).toBeCloseTo(p.sizing.position_usd, 4);
    const total = p.all.target_first + p.all.stop_first + p.all.week_ended;
    expect(total).toBeCloseTo(1.0, 7);
  });

  it("a pair with the dollar as its base sizes in dollars", async () => {
    const p = await getForexPlan("USDJPY", "long", 100, 200, 500, 2);
    expect(p.sizing.units).toBeCloseTo(p.sizing.position_usd, 7);
  });

  it("the pip value of a yen pair", async () => {
    const p = await getForexPlan("USDJPY", "long", 100, 200, 500, 2);
    expect(p.pip_size).toBe(0.01);
    expect(p.stop_price).toBeCloseTo(p.price - 1.00, 7);             // 100 yen pips is one yen
    // one pip on a lot is 1,000 yen, worth 1,000 / USDJPY dollars
    expect(p.sizing.pip_value_per_lot_usd).toBeCloseTo(1000 / p.price, 7);
    expect(p.sizing.loss_at_stop_usd).toBeCloseTo(10.0, 6);
    expect(p.sizing.loss_at_stop_usd).toBe(100 * p.sizing.pip_value_usd);
    // a cross quoted in yen has the same pip value per lot: the pip is still 1,000 yen
    const cross = await getForexPlan("GBPJPY", "short", 150, 300, 500, 2);
    expect(cross.sizing.pip_value_per_lot_usd).toBeCloseTo(1000 / p.price, 7);
    expect(cross.stop_price).toBeCloseTo(cross.price + 1.50, 7);
  });

  it("the pip value of a pair not quoted in dollars", async () => {
    const p = await getForexPlan("EURGBP", "long", 40, 80, 2000, 1, 1.5);
    const poundInUsd = reference.forex.base_in_usd.GBPUSD;
    // one pip on a lot is 10 pounds
    expect(p.sizing.pip_value_per_lot_usd).toBeCloseTo(10 * poundInUsd, 7);
    expect(p.sizing.pip_value_usd).toBeCloseTo(p.sizing.lots * 10 * poundInUsd, 7);
    expect(p.sizing.loss_at_stop_usd).toBeCloseTo(20.0, 6);
    expect(p.sizing.loss_at_stop_usd).toBe(41.5 * p.sizing.pip_value_usd);
    expect(p.sizing.gain_at_target_usd).toBe(78.5 * p.sizing.pip_value_usd);
    // the position is worth its units of euros in dollars
    expect(p.sizing.units * reference.forex.base_in_usd.EURUSD).toBeCloseTo(p.sizing.position_usd, 4);
  });

  it("the account and the risk never leave the browser, and changing them asks for nothing new", async () => {
    const small = await getForexPlan("EURUSD", "long", 60, 120, 100, 1);
    const large = await getForexPlan("eurusd", " Long ", 60, 120, 25_000, 2.5);
    expect(asked).toEqual(["/api/fx?pair=EURUSD&direction=long&stop=60&target=120&spread=0"]);
    expect(large.all).toEqual(small.all);
    expect(large.sizing.position_usd).toBeCloseTo(small.sizing.position_usd * 250 * 2.5, 6);
    clock += 61 * 60 * 1000;                                         // an hour later the odds are asked for again
    await getForexPlan("EURUSD", "long", 60, 120, 100, 1);
    expect(asked.length).toBe(2);
  });

  it("refuses a plan it cannot check, in plain words, without asking the function", async () => {
    const refused = async (...args: Parameters<typeof getForexPlan>) => {
      const error = await getForexPlan(...args).catch((e: unknown) => e);
      expect(error).toBeInstanceOf(DataError);
      return (error as DataError).message;
    };
    expect(await refused("XXXYYY", "long", 60, 120, 100, 1)).toBe("That pair is not on the list.");
    expect(await refused("EURUSD", "up", 60, 120, 100, 1)).toBe("Choose long or short.");
    expect(await refused("EURUSD", "long", 0, 120, 100, 1))
      .toBe("Enter the stop distance in pips: more than 0 and at most 5000.");
    expect(await refused("EURUSD", "long", 60, 5001, 100, 1))
      .toBe("Enter the target distance in pips: more than 0 and at most 5000.");
    expect(await refused("EURUSD", "long", 60, 120, 100, 1, 60))
      .toBe("Enter your broker's spread in pips: 0 or more, and smaller than the stop.");
    expect(await refused("EURUSD", "long", 60, 120, 0, 1)).toBe("Enter your account size in USD.");
    expect(await refused("EURUSD", "long", 60, 120, 100, 101))
      .toBe("Enter the risk per trade as a percentage between 0 and 100.");
    expect(asked).toEqual([]);
  });

  it("says so in plain words when the function is down, or refuses the plan itself", async () => {
    answer = async () => new Response("down", { status: 502 });
    await expect(getForexPlan("EURUSD", "long", 60, 120, 100, 1))
      .rejects.toThrow("Exchange-rate history is unavailable right now.");
    await expect(getForexOverview()).rejects.toThrow("Exchange-rate history is unavailable right now.");
    answer = async () => { throw new TypeError("offline"); };
    await expect(getForexPlan("EURUSD", "long", 61, 120, 100, 1))
      .rejects.toThrow("Exchange-rate history is unavailable right now.");
    answer = (url) => GET(new Request(`http://localhost${url}`));
    await expect(getForexPlan("NZDUSD", "long", 60, 120, 100, 1))
      .rejects.toThrow("NZDUSD has too little history to judge a plan.");
  });
});
