// The terminal's live figures: when prices are asked for directly, when they
// count as stopped, and the figures worked out again from a live price. The
// live versions of the movers and the breadth must agree with the core's own
// (which are checked against Python).

import { beforeEach, describe, expect, it } from "vitest";

import { configure, getAdvanced, getLivePrices, getTerminal } from "../src/core/api";
import { settled } from "../src/core/history";
import * as terminalAdv from "../src/core/terminalAdv";
import type { Market, Position, Risk } from "../src/core/types";
import {
  FAST_MAX_MS, FAST_MS, GRACE_MS, STALE_MS, changeFrom, decimalsOf, directPrices, exact, feedState, isStale,
  liveBreadth, liveFunding, liveMarkets, liveMovers, livePerformance, livePosition, liveRisk, nextDirectWait,
  updatesPerSecond,
} from "../src/ui/terminal_live";
import { makeUpstream, priceAt } from "./helpers/upstream";

describe("asking for prices directly", () => {
  it("starts when the stream has not delivered for a moment, and stops the instant it does", () => {
    // the stream gets a short while to connect before the first direct question
    expect(directPrices("connecting", false, 0, "basic")).toEqual([]);
    expect(directPrices("connecting", false, GRACE_MS - 1, "basic")).toEqual([]);
    expect(directPrices("connecting", false, GRACE_MS, "basic")).toEqual(["", "xyz"]);
    // a stream that dropped: both lists, on either screen
    expect(directPrices("poll", false, 60_000, "basic")).toEqual(["", "xyz"]);
    expect(directPrices("poll", false, 60_000, "advanced")).toEqual(["", "xyz"]);
    // the stream is delivering: nothing on the basic screen
    expect(directPrices("live", false, 60_000, "basic")).toEqual([]);
    // on the advanced screen only the markets the stream is not followed for
    expect(directPrices("live", false, 0, "advanced")).toEqual(["xyz"]);
    // and never while nobody is looking
    for (const stream of ["connecting", "poll", "live"] as const) {
      expect(directPrices(stream, true, 60_000, "advanced")).toEqual([]);
    }
  });

  it("asks again after a second, and backs off while the exchange does not answer", () => {
    expect(nextDirectWait(FAST_MS, true)).toBe(FAST_MS);
    let wait = FAST_MS;
    const waits = [];
    for (let i = 0; i < 6; i += 1) waits.push(wait = nextDirectWait(wait, false));
    expect(waits).toEqual([2000, 4000, 8000, FAST_MAX_MS, FAST_MAX_MS, FAST_MAX_MS]);
    expect(nextDirectWait(wait, true)).toBe(FAST_MS);         // one answer and it is back to every second
  });

  it("goes through the request budget, two units a question", async () => {
    const up = makeUpstream(Date.UTC(2026, 10, 11, 9, 30));
    const asked: unknown[] = [];
    configure({
      fetch: (async (input: RequestInfo | URL, init?: RequestInit) => {
        const body = JSON.parse(String(init?.body));
        if (body.type !== "allMids") return up.fetch(input, init);
        asked.push(body);
        return new Response(JSON.stringify(body.dex ? { "xyz:NVDA": "156.665" } : { ETH: "1818.05", BTC: "64948.5" }));
      }) as typeof fetch,
      now: () => up.now, storage: null, weightPerMinute: 5,
    });
    expect(await getLivePrices("")).toEqual({ ETH: "1818.05", BTC: "64948.5" });
    expect(await getLivePrices("xyz")).toEqual({ "xyz:NVDA": "156.665" });
    expect(asked).toEqual([{ type: "allMids" }, { type: "allMids", dex: "xyz" }]);
    // the budget (5 a minute here) is spent: a third question waits its turn instead of going out
    let answered = false;
    void getLivePrices("").then(() => { answered = true; }, () => { answered = true; });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(answered).toBe(false);
    expect(asked.length).toBe(2);
  });
});

describe("when prices count as stopped", () => {
  it("after five seconds with none, or when there never was one", () => {
    const now = 1_000_000;
    expect(isStale(now, now)).toBe(false);
    expect(isStale(now - STALE_MS, now)).toBe(false);
    expect(isStale(now - STALE_MS - 1, now)).toBe(true);
    expect(isStale(0, now)).toBe(true);
  });

  it("the status says how prices are arriving", () => {
    const now = 1_000_000;
    expect(feedState("live", 0, 0, now, 5000)).toBe("live");
    expect(feedState("poll", now - 800, now, now, 5000)).toBe("fast");        // asked for a moment ago
    expect(feedState("poll", now - 3001, now - 2000, now, 5000)).toBe("poll"); // direct answers stopped; the slow poll still works
    expect(feedState("connecting", 0, now - 2000, now, 5000)).toBe("poll");
    expect(feedState("poll", 0, now - 15_001, now, 5000)).toBe("down");
    expect(updatesPerSecond([])).toBe(0);
    expect(updatesPerSecond([80, 90, 100])).toBe(90);
  });
});

describe("figures as the exchange writes them", () => {
  it("keeps the exchange's decimals", () => {
    expect([decimalsOf("1834.35"), decimalsOf("64997.5"), decimalsOf("0.71741"), decimalsOf("3080.0"), decimalsOf("65000")])
      .toEqual([2, 1, 5, 1, 0]);
    expect(exact("1834.35", 2)).toBe("1,834.35");
    expect(exact(1834.3, 2)).toBe("1,834.30");
    expect(exact(0.71741, 5)).toBe("0.71741");
    expect(exact(null, 2)).toBe("–");
  });

  it("a change is measured from its reference price, or not at all", () => {
    expect(changeFrom(101, 100)).toBeCloseTo(1.0, 12);
    expect(changeFrom(99, 100)).toBeCloseTo(-1.0, 12);
    expect(changeFrom(101, null)).toBeNull();
    expect(changeFrom(undefined, 100)).toBeNull();
    expect(changeFrom(101, 0)).toBeNull();
  });
});

describe("figures worked out again from live prices", () => {
  const NOW = Date.UTC(2026, 10, 11, 9, 30);
  let up = makeUpstream(NOW);

  beforeEach(() => {
    up = makeUpstream(NOW);
    configure({ fetch: up.fetch, now: () => up.now, storage: null, weightPerMinute: Infinity });
  });

  it("the snapshot carries what the screen needs: yesterday's price, the 7 and 30 day reference prices, every market", async () => {
    await getAdvanced(18);
    await settled();
    const adv = await getAdvanced(18);
    expect(adv.universe.length).toBe(adv.markets);
    for (const row of adv.worksheet) {
      expect(changeFrom(row.price, row.prev_day_price)).toBeCloseTo(row.day_change_pct as number, 9);
      // the reference prices are four-hour closes; the percentages beside them are measured from the same ones
      expect(row.week_ref_price as number).toBeGreaterThan(0);
      expect(row.month_ref_price as number).toBeGreaterThan(0);
    }
    for (const m of (await getTerminal(18)).markets) {
      expect(changeFrom(m.price, m.prev_day_price)).toBeCloseTo(m.day_change_pct as number, 9);
    }
    // each thirty-day line starts at 100 from the price given for it
    for (const [name, start] of Object.entries(adv.performance_start)) {
      expect(adv.performance[name][0]).toBe(100);
      expect(start.price).toBeGreaterThan(0);
      const live = livePerformance(adv.performance, adv.performance_start, () => start.price * 1.1);
      expect(live[name].length).toBe(adv.performance[name].length + 1);
      expect(live[name][live[name].length - 1]).toBeCloseTo(110, 9);
    }
    expect(livePerformance(adv.performance, adv.performance_start, () => undefined)).toEqual(adv.performance);
  });

  it("movers and breadth at the snapshot's own prices are the snapshot's", async () => {
    const adv = await getAdvanced(18);
    const markets = liveMarkets(adv.universe, () => undefined);
    expect(liveMovers(markets)).toEqual(adv.movers);
    expect(liveBreadth(markets)).toEqual(adv.breadth);
  });

  it("and at live prices they are what the core makes of a catalogue at those prices", async () => {
    const adv = await getAdvanced(18);
    // every market moves: some up, some down, by a few percent
    const bump = (symbol: string) => 1 + ((symbol.length * 7 + symbol.charCodeAt(symbol.length - 1)) % 11 - 5) / 100;
    const priceOf = (symbol: string) => (adv.universe.find((m) => m.symbol === symbol) as { price: number }).price * bump(symbol);
    const markets = liveMarkets(adv.universe, priceOf);
    const catalogue = new Map<string, Market>(adv.universe.map((m) => [m.symbol, {
      ...m, price: priceOf(m.symbol), day_change_pct: changeFrom(priceOf(m.symbol), m.prev_day_price),
      funding_hourly: 0, max_leverage: 20, open_interest_usd: 0,
    }]));
    expect(liveMovers(markets)).toEqual(terminalAdv.movers(catalogue));
    expect(liveBreadth(markets)).toEqual(terminalAdv.breadth(catalogue));
    expect(liveMovers(markets)).not.toEqual(adv.movers);             // the ranking really moved
  });

  it("the stream's own yesterday's price is used where it is known", () => {
    const universe = [{ symbol: "ETH", label: "ETH", class: "crypto" as const, price: 100, prev_day_price: 100,
                        day_volume_usd: 1e9 }];
    expect(liveMarkets(universe, () => 110)[0].day_change_pct).toBeCloseTo(10, 9);
    expect(liveMarkets(universe, () => 110, () => 88)[0].day_change_pct).toBeCloseTo(25, 9);
  });

  it("a position is re-priced by the core's own arithmetic", async () => {
    // a market that keeps falling, so the week has open short positions to re-price
    up.mood = () => 20;
    const snap = await getTerminal(18);
    const open = snap.positions.filter((p) => p.status === "open");
    expect(open.length).toBeGreaterThan(0);
    for (const p of open) {
      // at the snapshot's own last price the live figures are the snapshot's
      const same = livePosition(p, p.last);
      expect(same.pnl_usd).toBeCloseTo(p.pnl_usd, 9);
      expect(same.pnl_pct).toBeCloseTo(p.pnl_pct, 9);
      expect(same.stop_distance_pct as number).toBeCloseTo(p.stop_distance_pct as number, 9);
      // a short gains when the price falls, and its stop gets further away
      const lower = livePosition(p, (p.last as number) * 0.99);
      expect(Math.sign(lower.pnl_usd - p.pnl_usd)).toBe(p.side === "short" ? 1 : -1);
      expect(livePosition(p, undefined)).toBe(p);
    }
    const closed: Position = { ...open[0], status: "stopped" };
    expect(livePosition(closed, 1)).toBe(closed);                    // a closed position no longer moves
  });

  it("the risk rows follow the live positions; the totals do not depend on price", async () => {
    up.mood = () => 20;
    const adv = await getAdvanced(18);
    const snap = await getTerminal(18);
    const moved = snap.positions.map((p) => livePosition(p, (p.last as number) * 0.98));
    const risk = liveRisk(adv.risk as Risk, moved);
    expect(risk.exposure_usd).toBe(adv.risk.exposure_usd);
    expect(risk.worst_case_usd).toBe(adv.risk.worst_case_usd);
    const openRows = risk.rows.filter((r) => r.status === "open");
    expect(openRows.length).toBeGreaterThan(0);
    for (const row of openRows) {
      const p = moved.find((x) => x.coin === row.coin) as Position;
      expect(row.pnl_usd).toBe(p.pnl_usd);
      expect(row.stop_distance_pct).toBe(p.stop_distance_pct);
      expect(row.liquidation_distance_pct as number)
        .toBeCloseTo(Math.abs((p.liquidation as number) - (p.last as number)) / (p.last as number) * 100, 9);
    }
    // at the snapshot's own prices the rows are the snapshot's
    const still = liveRisk(adv.risk as Risk, snap.positions.map((p) => livePosition(p, p.last)));
    still.rows.forEach((row, i) => {
      expect(row.pnl_usd).toBeCloseTo(adv.risk.rows[i].pnl_usd, 9);
      if (row.liquidation_distance_pct !== null) {
        expect(row.liquidation_distance_pct).toBeCloseTo(adv.risk.rows[i].liquidation_distance_pct as number, 9);
      }
    });
  });

  it("funding and open interest come from the stream's figures, most extreme funding first", async () => {
    const adv = await getAdvanced(18);
    expect(liveFunding(adv.funding, () => undefined, () => undefined)).toEqual(adv.funding);
    const eth = adv.funding.find((r) => r.symbol === "ETH") as (typeof adv.funding)[number];
    const live = liveFunding(adv.funding, (symbol) => (symbol === "ETH"
      ? { funding: 0.0001, openInterest: 1000, dayVolume: 5e8 } : undefined), () => priceAt("ETH", NOW));
    expect(live[0].symbol).toBe("ETH");                              // 87.6% a year: the most extreme now
    expect(live[0].funding_apr).toBeCloseTo(0.0001 * 24 * 365 * 100, 9);
    expect(live[0].open_interest_usd).toBeCloseTo(1000 * priceAt("ETH", NOW), 6);
    expect(live[0].day_volume_usd).toBe(5e8);
    expect(live.length).toBe(adv.funding.length);
    expect(eth.funding_apr).not.toBe(live[0].funding_apr);
  });
});
