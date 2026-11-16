// The forex function (api/fx.ts): reading the ECB's zip, working out the board,
// and judging a plan in pips. The expected answers come from Python's
// forex_weekly on the same made-up table (tests/fixtures/make_reference.py);
// the cases are those of service/server/tests/test_forex_weekly.py.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  GET, baseInUsd, forgetRates, overview, pairSeries, parseRates, performance, pipSize, pipsPerPercent, planOdds,
  planProblem, replayShape, setClock, summary, unzipFirst, weekInProgress, weeklyFrame, wilsonInterval,
  type Direction, type Series,
} from "../api/fx";
import reference from "./fixtures/reference.json";
import { zipOf } from "./helpers/zip";

const daily = parseRates(reference.forex.csv);
const DAY = 24 * 3600 * 1000;
const dayOf = (iso: string) => Date.parse(`${iso}T00:00:00Z`) / DAY;
const at = (iso: string, hour = 12) => () => Date.parse(`${iso}T00:00:00Z`) + hour * 3600 * 1000;

// The made-up table ends on Wednesday 11 November 2026, and Python answered for that evening:
// its last week is still in progress.
beforeEach(() => setClock(() => reference.forex.now_ms));
afterEach(() => setClock(null));

describe("the forex function", () => {
  beforeEach(() => forgetRates());
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

  it("works out the same board as Python once the week has ended, too", () => {
    setClock(() => reference.forex.after_the_week.now_ms);          // the Saturday after the table's last day
    expect(JSON.stringify(overview(reference.forex.csv))).toBe(JSON.stringify(reference.forex.after_the_week.overview));
    const mine = planOdds(daily, "EURUSD", "long", 60, 120) as unknown as Record<string, unknown>;
    const want = reference.forex.after_the_week.plan as unknown as Record<string, unknown>;
    for (const key of Object.keys(want).filter((k) => k !== "sizing" && k !== "avg_result_usd")) {
      expect(mine[key], key).toEqual(want[key]);
    }
    // seen midweek, the same table has one finished week fewer
    expect((want.all as { weeks: number }).weeks).toBe(reference.forex.plans[0].plan.all.weeks + 1);
  });

  it("works out the same board as Python, pair by pair", () => {
    const mine = overview(reference.forex.csv);
    const theirs = reference.forex.overview;
    // the week in progress is reported apart: Monday to Wednesday against last Friday's close
    expect(mine.pairs.every((p) => typeof p.this_week_pips === "number" && typeof p.this_week_pct === "number")).toBe(true);
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

  it("answers a plan request with the odds, and nothing about the account", async () => {
    let downloads = 0;
    vi.stubGlobal("fetch", async () => {
      downloads += 1;
      return new Response(zipOf("eurofxref-hist.csv", reference.forex.csv));
    });
    const response = await GET(new Request("http://localhost/api/fx?pair=eurusd&direction=Long&stop=60&target=120"));
    expect(response.status).toBe(200);
    expect(response.headers.get("Cache-Control")).toBe("public, s-maxage=3600, stale-while-revalidate=86400");
    const body = await response.json() as Record<string, unknown>;
    const want = reference.forex.plans[0].plan as unknown as Record<string, unknown>;
    expect(body).not.toHaveProperty("sizing");
    expect(body).not.toHaveProperty("avg_result_usd");
    for (const key of Object.keys(want).filter((k) => k !== "sizing" && k !== "avg_result_usd")) {
      expect(body[key], key).toEqual(want[key]);
    }
    expect(body.base_usd).toBe(reference.forex.base_in_usd.EURUSD);
    expect(JSON.stringify(body).length).toBeLessThan(2500);
    // a warm function keeps the table: a second shape does not download it again
    await GET(new Request("http://localhost/api/fx?pair=USDJPY&direction=short&stop=90&target=150&spread=1"));
    await GET(new Request("http://localhost/api/fx"));
    expect(downloads).toBe(1);
  });

  it("refuses a plan it cannot check with a 400 and plain words, before downloading anything", async () => {
    let downloads = 0;
    vi.stubGlobal("fetch", async () => {
      downloads += 1;
      return new Response(zipOf("eurofxref-hist.csv", reference.forex.csv));
    });
    const refused = async (query: string) => {
      const response = await GET(new Request(`http://localhost/api/fx?${query}`));
      expect(response.status).toBe(400);
      expect(response.headers.get("Cache-Control")).toBe("no-store");
      return (await response.json() as { error: string }).error;
    };
    expect(await refused("pair=XXXYYY&direction=long&stop=60&target=120")).toBe("That pair is not on the list.");
    expect(await refused("pair=EURUSD&direction=up&stop=60&target=120")).toBe("Choose long or short.");
    expect(await refused("pair=EURUSD&direction=long&target=120"))
      .toBe("Enter the stop distance in pips: more than 0 and at most 5000.");
    expect(await refused("pair=EURUSD&direction=long&stop=abc&target=120"))
      .toBe("Enter the stop distance in pips: more than 0 and at most 5000.");
    expect(await refused("pair=EURUSD&direction=long&stop=60&target=5001"))
      .toBe("Enter the target distance in pips: more than 0 and at most 5000.");
    expect(await refused("pair=EURUSD&direction=long&stop=60&target=120&spread=60"))
      .toBe("Enter your broker's spread in pips: 0 or more, and smaller than the stop.");
    expect(downloads).toBe(0);
  });

  it("says so when a pair has too little history to judge a plan", async () => {
    vi.stubGlobal("fetch", async () => new Response(zipOf("eurofxref-hist.csv", reference.forex.csv)));
    const response = await GET(new Request("http://localhost/api/fx?pair=NZDUSD&direction=long&stop=60&target=120"));
    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({ error: "NZDUSD has too little history to judge a plan." });
  });
});

describe("weeks end with Friday's fixing", () => {
  const FRIDAY = dayOf("2026-11-20");

  /** A rate on every working day from the start of 2024 through the given day. */
  const through = (lastDay: string): Series => {
    const series: Series = { days: [], values: [] };
    for (let day = dayOf("2024-01-01"), i = 0; day <= dayOf(lastDay); day += 1) {
      if ((((day + 3) % 7) + 7) % 7 > 4) continue;                   // weekends have no fixing
      series.days.push(day);
      series.values.push(1.10 * Math.exp(0.03 * Math.sin(i / 9) + 0.004 * Math.sin(i * 1.7)));
      i += 1;
    }
    return series;
  };
  const lastFriday = (series: Series, today: string) => {
    const weeks = weeklyFrame(series, dayOf(today));
    return weeks.friday[weeks.friday.length - 1];
  };

  it("a week is in progress until its Friday fixing, or until the Friday has passed", () => {
    const cases: Array<[number, string, string, boolean]> = [
      // Friday label, last fixing, today, still in progress?
      [FRIDAY, "2026-11-16", "2026-11-16", true],                    // Monday, one fixing in
      [FRIDAY, "2026-11-19", "2026-11-20", true],                    // Friday before its fixing
      [FRIDAY, "2026-11-20", "2026-11-20", false],                   // Friday after its fixing
      [FRIDAY, "2026-11-19", "2026-11-21", false],                   // a holiday Friday, seen on Saturday
      [dayOf("2026-11-13"), "2026-11-13", "2026-11-15", false],      // an ordinary weekend
    ];
    for (const [friday, lastFixing, today, expected] of cases) {
      expect(weekInProgress(friday, dayOf(lastFixing), dayOf(today)), `${lastFixing} seen on ${today}`).toBe(expected);
    }
  });

  it("Monday with one fixing after a completed Friday", () => {
    const finished = weeklyFrame(through("2026-11-13"), dayOf("2026-11-14"));
    const monday = weeklyFrame(through("2026-11-16"), dayOf("2026-11-16"));
    expect(monday.friday[monday.friday.length - 1]).toBe(dayOf("2026-11-13"));
    expect(monday).toEqual(finished);                               // Monday's fixing changes nothing measured per week
  });

  it("Friday before and after its fixing", () => {
    expect(lastFriday(through("2026-11-19"), "2026-11-20")).toBe(dayOf("2026-11-13"));
    expect(lastFriday(through("2026-11-20"), "2026-11-20")).toBe(FRIDAY);
    const before = weeklyFrame(through("2026-11-19"), FRIDAY);
    const after = weeklyFrame(through("2026-11-20"), FRIDAY);
    expect(after.close.length).toBe(before.close.length + 1);
  });

  it("a holiday Friday seen on Saturday closes the week at Thursday's fixing", () => {
    const series = through("2026-11-19");                            // no fixing on Friday the 20th
    const weeks = weeklyFrame(series, dayOf("2026-11-21"));
    expect(weeks.friday[weeks.friday.length - 1]).toBe(FRIDAY);
    expect(weeks.close[weeks.close.length - 1]).toBe(series.values[series.values.length - 1]);
  });

  it("the unfinished week is not scored as a trade", () => {
    setClock(at("2026-11-18"));
    const midweek = replayShape(through("2026-11-18"), "long", 0.75, 1.5);
    const friday = replayShape(through("2026-11-13"), "long", 0.75, 1.5);
    expect(midweek).toEqual(friday);
    setClock(at("2026-11-20", 18));
    const done = replayShape(through("2026-11-20"), "long", 0.75, 1.5);
    expect(done.length).toBe(friday.length + 1);
    expect(done.slice(0, -1)).toEqual(friday);
  });

  it("the function goes by today's date: the same table reads differently after the week has ended", async () => {
    const download = async () => new Response(zipOf("eurofxref-hist.csv", reference.forex.csv));
    vi.stubGlobal("fetch", download);
    forgetRates();
    const board = async () => (await (await GET(new Request("http://localhost/api/fx"))).json() as typeof reference.forex.overview).pairs[0];
    const midweek = await board();
    setClock(() => reference.forex.after_the_week.now_ms);
    const weekend = await board();
    vi.unstubAllGlobals();
    expect(midweek.this_week_pips).toBe(reference.forex.overview.pairs[0].this_week_pips);
    expect(weekend.this_week_pips).toBeNull();
    expect(weekend.last_week_pips).toBe(midweek.this_week_pips);     // what was this week so far is now last week
  });
});

describe("pips", () => {
  it("a pip is the second decimal for yen pairs and the fourth for the rest", () => {
    const pairsKnown = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "NZDUSD", "USDCAD", "EURGBP", "EURJPY",
                        "EURCHF", "GBPJPY", "AUDJPY", "EURAUD", "AUDNZD"];
    expect(pairsKnown.filter((pair) => pipSize(pair) === 0.01)).toEqual(["USDJPY", "EURJPY", "GBPJPY", "AUDJPY"]);
    expect(pairsKnown.filter((pair) => pipSize(pair) === 0.0001).length).toBe(10);
    expect(pipsPerPercent("EURUSD", 1.2)).toBeCloseTo(120.0, 7);        // 1% of 1.2000 is 120 pips
    expect(pipsPerPercent("USDJPY", 150.0)).toBeCloseTo(150.0, 7);      // 1% of 150.00 is 150 pips
  });

  it("a plan request is checked in plain words", () => {
    expect(planProblem("EURUSD", "long", 50, 100)).toBeNull();
    expect(planProblem("EURUSD", "long", 50, 100, 1.5)).toBeNull();
    expect(planProblem("USDJPY", "short", 5000, 5000, 4999)).toBeNull();
    expect(planProblem("XXXYYY", "long", 50, 100)).toBe("That pair is not on the list.");
    expect(planProblem("toString", "long", 50, 100)).toBe("That pair is not on the list.");
    expect(planProblem("EURUSD", "up", 50, 100)).toBe("Choose long or short.");
    const stop = "Enter the stop distance in pips: more than 0 and at most 5000.";
    const target = "Enter the target distance in pips: more than 0 and at most 5000.";
    const spread = "Enter your broker's spread in pips: 0 or more, and smaller than the stop.";
    for (const bad of [0, -5, 5000.5, NaN, Infinity]) {
      expect(planProblem("EURUSD", "long", bad, 100)).toBe(stop);
      expect(planProblem("EURUSD", "long", 50, bad)).toBe(target);
    }
    for (const bad of [-0.1, 50, 80, NaN]) expect(planProblem("EURUSD", "long", 50, 100, bad)).toBe(spread);
  });
});

describe("past weeks replayed with a shape", () => {
  const eurusd = pairSeries(daily, "EURUSD");

  it("outcomes are bounded by the stop and the target", () => {
    const rows = replayShape(eurusd, "long", 0.75, 1.5, 0.04);
    expect(rows.length).toBeGreaterThan(60);
    for (const r of rows) {
      expect(r.gross).toBeGreaterThanOrEqual(-0.75 - 1e-9);
      expect(r.gross).toBeLessThanOrEqual(1.5 + 1e-9);
      if (r.how === "stop") expect(r.gross).toBeCloseTo(-0.75, 7);
      if (r.how === "target") expect(r.gross).toBeCloseTo(1.5, 7);
      expect(r.net).toBeLessThan(r.gross);                           // costs always charged
    }
    expect(new Set(rows.map((r) => r.how))).toEqual(new Set(["stop", "target", "week ended"]));
  });

  it("no costs by default", () => {
    const rows = replayShape(eurusd, "long", 0.75, 1.5);
    expect(rows.every((r) => r.net === r.gross && r.fees === 0)).toBe(true);
  });

  it("long and short mirror when the week runs out", () => {
    const series = pairSeries(daily, "EURGBP");
    const longs = replayShape(series, "long", 50, 50);              // levels never reached
    const shorts = replayShape(series, "short", 50, 50);
    expect(longs.every((r) => r.how === "week ended")).toBe(true);
    longs.forEach((r, i) => expect(r.gross).toBeCloseTo(-shorts[i].gross, 7));
  });

  it("a tight target wins more often but needs more", () => {
    const tight = summary(replayShape(eurusd, "long", 1.5, 0.5), 1.0, 100.0);
    const wide = summary(replayShape(eurusd, "long", 0.5, 1.5), 1.0, 100.0);
    expect(tight.win_rate as number).toBeGreaterThan(wide.win_rate as number);
    expect(tight.breakeven_win_rate as number).toBeGreaterThan(wide.breakeven_win_rate as number);
  });

  it("an uptrend makes longs win", () => {
    // 600 working days that climb steadily with a small wobble
    const series: Series = { days: [], values: [] };
    for (let day = 16440, i = 0; i < 600; day += 1) {               // 16440 is Monday 2015-01-05
      if ((((day + 3) % 7) + 7) % 7 > 4) continue;                   // weekends have no fixing
      series.days.push(day);
      series.values.push(Math.exp(i * 0.002 + 0.001 * Math.sin(i * 1.7)));
      i += 1;
    }
    const s = summary(replayShape(series, "long", 3, 3), 1.0, 100.0);
    expect(s.win_rate as number).toBeGreaterThan(0.9);
    // results in pips are the percent results at the given pips per percent
    expect(s.avg_result_pips).toBeCloseTo(s.avg_result_pct * 100.0, 7);
  });

  it("every week comes out as it does in Python, to the last digit", () => {
    for (const shape of reference.forex.shapes) {
      const rows = replayShape(pairSeries(daily, shape.pair), shape.direction as Direction, shape.stop_mult,
                               shape.target_mult, shape.cost_pct);
      expect(rows, `${shape.pair} ${shape.direction}`).toEqual(shape.rows);
    }
  });
});

describe("the win-rate statistics", () => {
  it("the likely range of a win rate, as Python works it out", () => {
    for (const [wins, n, low, high] of reference.forex.statistics.wilson) {
      expect(wilsonInterval(wins, n), `${wins} of ${n}`).toEqual([low, high]);
    }
    expect(wilsonInterval(0, 0)).toEqual([0.0, 1.0]);               // nothing known yet
  });

  it("win rate, break-even win rate and average result, as Python works them out", () => {
    const nets = reference.forex.shapes[0].rows.map((r) => ({ net: r.net }));
    expect(performance(nets)).toEqual(reference.forex.statistics.performance);
  });

  it("with no trades there is no win rate, and with only wins no break-even", () => {
    expect(performance([])).toEqual({ trades: 0, wins: 0, win_rate: null, win_rate_low: null, win_rate_high: null,
                                      breakeven_win_rate: null, expectancy: null });
    const wins = performance([{ net: 1 }, { net: 2 }]);
    expect([wins.win_rate, wins.breakeven_win_rate, wins.expectancy]).toEqual([1, null, 1.5]);
    // one win of 2 and two losses of 1: break-even is winning one time in three
    expect(performance([{ net: 2 }, { net: -1 }, { net: -1 }]).breakeven_win_rate).toBeCloseTo(1 / 3, 12);
  });
});

describe("a plan's odds", () => {
  it("are the same as Python's for every reference plan", () => {
    for (const { args, plan } of reference.forex.plans) {
      const [pair, direction, stop, target, , , spread] = args as [string, Direction, number, number, number, number, number];
      const mine = planOdds(daily, pair, direction, stop, target, spread) as unknown as Record<string, unknown>;
      const want = plan as unknown as Record<string, unknown>;
      for (const key of Object.keys(want).filter((k) => k !== "sizing" && k !== "avg_result_usd")) {
        expect(mine[key], `${pair} ${key}`).toEqual(want[key]);
      }
    }
  });

  it("the base currency in dollars, from the latest fixing", () => {
    for (const [pair, value] of Object.entries(reference.forex.base_in_usd)) expect(baseInUsd(daily, pair)).toBe(value);
    expect(baseInUsd(daily, "USDJPY")).toBe(1.0);
  });
});
