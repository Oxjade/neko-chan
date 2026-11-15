// The number handling that has to agree with Python and NumPy to the last digit.
// Expected values come from tests/fixtures/make_reference.py.

import { describe, expect, it } from "vitest";

import { isoDay, isoMoment, parseDay, weekday } from "../src/core/dates";
import { mean, pairwiseSum, pyFixed, pyGrouped, pySum, sampleStd } from "../src/core/numeric";
import reference from "./fixtures/reference.json";

describe("adding up like NumPy", () => {
  it("gives the same sum, mean and spread, bit for bit", () => {
    for (const row of reference.sums) {
      const values = Float64Array.from(row.values);
      expect(pairwiseSum(values), `sum of ${values.length}`).toBe(row.sum);
      expect(mean(values), `mean of ${values.length}`).toBe(row.mean);
      if (row.std !== null) expect(sampleStd(values), `std of ${values.length}`).toBe(row.std);
    }
  });

  it("and like Python's own sum(), which keeps a correction for lost rounding", () => {
    let plainDiffers = 0;
    for (const row of reference.python_sums) {
      expect(pySum(row.values), `sum of ${row.values.length}`).toBe(row.sum);
      if (row.values.reduce((a, b) => a + b, 0) !== row.sum) plainDiffers += 1;
    }
    expect(plainDiffers).toBeGreaterThan(0);                 // otherwise this test proves nothing
    expect(pySum([])).toBe(0);
  });

  it("can work on part of an array", () => {
    const row = reference.sums.find((r) => r.values.length === 42) as { values: number[]; sum: number };
    const padded = [9, 9, 9, ...row.values, 7];
    expect(pairwiseSum(padded, 3, 45)).toBe(row.sum);
  });
});

describe("writing numbers like Python", () => {
  it("rounds exact ties to the even digit and groups thousands", () => {
    for (const [value, digits, plain, grouped] of reference.formats as Array<[number, number, string, string]>) {
      // JSON cannot carry -0.0, so the fixture's "-0.0" case is checked by hand below.
      if (Object.is(value, 0) && plain.startsWith("-")) continue;
      expect(pyFixed(value, digits), `${value} to ${digits} places`).toBe(plain);
      expect(pyGrouped(value, digits), `${value} grouped`).toBe(grouped);
    }
  });

  it("differs from toFixed exactly where Python does", () => {
    expect((2.5).toFixed(0)).toBe("3");
    expect(pyFixed(2.5, 0)).toBe("2");
    expect(pyFixed(0.5, 0)).toBe("0");
    expect(pyFixed(1.5, 0)).toBe("2");
    expect(pyFixed(-0.0, 1)).toBe("-0.0");
    expect(pyFixed(-0.04, 1)).toBe("-0.0");
    expect(pyGrouped(1234567.891, 2)).toBe("1,234,567.89");
  });
});

describe("dates", () => {
  it("counts days and weekdays in UTC the way Python does", () => {
    const sunday = parseDay("2026-11-15");
    expect(weekday(sunday)).toBe(6);
    expect(weekday(sunday + 1)).toBe(0);
    expect(isoDay(sunday - 7)).toBe("2026-11-08");
    expect(weekday(parseDay("1970-01-01"))).toBe(3);
  });

  it("writes a moment as datetime.isoformat() does", () => {
    expect(isoMoment(Date.UTC(2026, 10, 15, 21, 33, 23, 714))).toBe("2026-11-15T21:33:23.714000+00:00");
    expect(isoMoment(Date.UTC(2026, 10, 15, 20, 0, 0))).toBe("2026-11-15T20:00:00+00:00");
  });
});
