// What the screens are handed has the same fields as what the Python server
// sent them. tests/ui/fixtures holds real answers captured from the Python
// version; here the TypeScript core answers from a stand-in exchange and the
// two are compared field name by field name. (The numbers themselves are
// checked by tests/parity on recorded data.)

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { beforeAll, describe, expect, it } from "vitest";

import { configure, getAdvanced, getBook, getChart, getScoreboard, getTerminal, getWeek } from "../src/core/api";
import { settled } from "../src/core/history";
import { makeUpstream } from "./helpers/upstream";

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), "ui/fixtures");
const captured = (name: string): unknown => JSON.parse(readFileSync(join(FIXTURES, `${name}.json`), "utf8"));

// Fields that are present only in some situations (a call was made, a stop exists, ...).
const SOMETIMES = new Set(["entry", "liquidation_move_pct", "liquidation_price", "normal_week_pct", "stop_pct",
                           "stop_price", "stake_lost_at_stop", "target_pct", "target_price", "stake_made_at_target",
                           "min_weekly_amount", "stop_distance_pct", "stop_room_left", "target_distance_pct", "bars"]);
// Objects keyed by data (a coin, a market label), not by field name.
const KEYED_BY_DATA = [/\.capped$/, /\.performance$/, /\.by_call$/];

type Fields = Map<string, Set<string>>;

/** Every object's field names by where it sits, with list positions and data keys folded together. */
function fieldsOf(value: unknown, path: string, into: Fields): Fields {
  if (Array.isArray(value)) {
    for (const item of value) fieldsOf(item, `${path}[]`, into);
  } else if (value !== null && typeof value === "object") {
    const byData = KEYED_BY_DATA.some((pattern) => pattern.test(path));
    const names = into.get(path) ?? new Set<string>();
    into.set(path, names);
    for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
      if (child === undefined) continue;
      if (!byData) names.add(key);
      fieldsOf(child, byData ? `${path}.*` : `${path}.${key}`, into);
    }
  }
  return into;
}

function compare(name: string, mine: unknown, extra: string[] = []): void {
  const theirs = fieldsOf(captured(name), name, new Map());
  const ours = fieldsOf(JSON.parse(JSON.stringify(mine)), name, new Map());
  const problems: string[] = [];
  for (const [path, names] of ours) {
    const known = theirs.get(path);
    if (!known) continue;                                    // nothing captured there to compare with (an empty list)
    for (const field of names) {
      if (!known.has(field) && !extra.includes(field) && !SOMETIMES.has(field)) {
        problems.push(`${path}.${field} is not in the Python answer`);
      }
    }
    for (const field of known) {
      if (!names.has(field) && !SOMETIMES.has(field)) problems.push(`${path}.${field} is missing`);
    }
  }
  expect(problems, name).toEqual([]);
  expect(ours.size).toBeGreaterThan(0);
}

describe("field names match the Python answers", () => {
  // A Wednesday with a fearful mood, so there are calls and open model positions.
  const NOW = Date.UTC(2026, 10, 11, 9, 30);
  const up = makeUpstream(NOW);
  up.mood = () => 30;

  beforeAll(async () => {
    configure({ fetch: up.fetch, now: () => up.now, storage: null, weightPerMinute: Infinity });
    await getAdvanced(18);
    await settled();
  });

  it("this week", async () => {
    const week = await getWeek();
    expect(week.calls.length).toBeGreaterThan(0);            // otherwise the call-only fields go unchecked
    compare("week", week);
  });

  it("week by week", async () => {
    compare("scoreboard", await getScoreboard(18, 0));
  });

  it("the terminal", async () => {
    const snap = await getTerminal(18);
    expect(snap.positions.length).toBeGreaterThan(0);
    compare("terminal", snap);
  });

  it("the advanced panels, plus `pending`", async () => {
    const advanced = await getAdvanced(18);
    expect(advanced.pending).toBe(0);
    expect(advanced.worksheet.length).toBe(30);
    compare("advanced", advanced, ["pending"]);
  });

  it("the chart and the order book", async () => {
    compare("chart-ETH-4h", await getChart("ETH", "4h"));
    compare("chart-NVDA-4h", await getChart("xyz:NVDA", "4h"));
    compare("book-ETH", await getBook("ETH"));
  });
});
