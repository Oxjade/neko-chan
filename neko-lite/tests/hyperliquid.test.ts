// Staying inside the exchange's allowance: requests the screen is waiting for
// go first, long downloads queue behind them a few at a time, and a "slow
// down" from the exchange is waited out and tried again.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { backgroundBusy, candleWeight, info, weightSpent } from "../src/core/hyperliquid";
import { DataError, configure } from "../src/core/runtime";

interface Call {
  payload: { n: number };
  answer: (status?: number) => void;
}

describe("requests to the exchange", () => {
  let calls: Call[];

  // A fetch the test answers by hand, so it can see what is in flight.
  const manual: typeof fetch = (_input, init) => new Promise((resolve) => {
    const payload = JSON.parse(String(init?.body));
    calls.push({ payload, answer: (status = 200) => resolve(new Response(JSON.stringify({ ok: payload.n }), { status })) });
  });
  const flush = async () => { for (let i = 0; i < 20; i += 1) await Promise.resolve(); };

  beforeEach(() => {
    vi.useFakeTimers();
    calls = [];
    configure({ fetch: manual, weightPerMinute: 100 });
  });

  afterEach(() => {
    configure({ weightPerMinute: Infinity });
    vi.useRealTimers();
  });

  it("weighs a candle request by the candles it returns", () => {
    expect(candleWeight(12)).toBe(21);
    expect(candleWeight(5001)).toBe(104);
  });

  it("holds a request back when the minute's allowance is used up, then sends it", async () => {
    const results = [1, 2, 3].map((n) => info<{ ok: number }>({ n }, { weight: 40 }));
    await flush();
    expect(calls.map((c) => c.payload.n)).toEqual([1, 2]);      // the third would make 120 of 100
    calls[0].answer();
    calls[1].answer();
    await flush();
    expect(calls.length).toBe(2);
    await vi.advanceTimersByTimeAsync(61_000);                   // a minute later the allowance is back
    expect(calls.length).toBe(3);
    calls[2].answer();
    expect((await Promise.all(results)).map((r) => r.ok)).toEqual([1, 2, 3]);
  });

  it("runs at most three long downloads at once and lets a waiting screen request jump the queue", async () => {
    configure({ fetch: manual, weightPerMinute: 10_000 });
    const downloads = [1, 2, 3, 4, 5].map((n) => info({ n }, { weight: 100, background: true }));
    await flush();
    expect(calls.map((c) => c.payload.n)).toEqual([1, 2, 3]);
    expect(backgroundBusy()).toBe(true);
    const urgent = info({ n: 99 }, { weight: 20 });
    await flush();
    expect(calls.map((c) => c.payload.n)).toEqual([1, 2, 3, 99]);   // not behind downloads 4 and 5
    calls[0].answer();
    await flush();
    expect(calls.length).toBe(4);                                // no new download while the screen is waiting
    calls[3].answer();
    await urgent;
    await flush();
    expect(calls.map((c) => c.payload.n)).toEqual([1, 2, 3, 99, 4]);
    calls[1].answer();
    calls[2].answer();
    calls[4].answer();
    await flush();
    calls[5].answer();
    await Promise.all(downloads);
    expect(backgroundBusy()).toBe(false);
  });

  it("counts what a download really cost once the answer is in", async () => {
    configure({ fetch: manual, weightPerMinute: 1000 });
    const download = info({ n: 1 }, { weight: 104, background: true, actualWeight: () => 54 });
    await flush();
    expect(weightSpent()).toBe(104);
    calls[0].answer();
    await download;
    expect(weightSpent()).toBe(54);
  });

  it("waits and tries a long download again when the exchange says slow down", async () => {
    configure({ fetch: manual, weightPerMinute: 10_000 });
    const download = info<{ ok: number }>({ n: 7 }, { weight: 100, background: true });
    await flush();
    calls[0].answer(429);
    await flush();
    expect(calls.length).toBe(1);                                // not hammering: it waits first
    await vi.advanceTimersByTimeAsync(2_500);
    expect(calls.length).toBe(2);
    calls[1].answer();
    expect((await download).ok).toBe(7);
  });

  it("tells the screen at once, in plain words, when its own request fails", async () => {
    const asked = info({ n: 1 }, { weight: 20 });
    await flush();
    calls[0].answer(429);
    await expect(asked).rejects.toBeInstanceOf(DataError);
    await expect(asked).rejects.toThrow("Market data is unavailable right now (HTTPError).");
    expect(calls.length).toBe(1);
  });

  it("gives up on a request that gets no answer", async () => {
    configure({ fetch: ((_input: RequestInfo | URL, init?: RequestInit) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(new Error("aborted")));
    })) as typeof fetch, weightPerMinute: 1000 });
    const asked = info({ n: 1 }, { weight: 20 });
    const failed = expect(asked).rejects.toThrow("Market data is unavailable right now (Timeout).");
    await vi.advanceTimersByTimeAsync(21_000);
    await failed;
  });
});
