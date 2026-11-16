// Copies the few fields the app reads out of the research tables into small
// JSON files under src/core/data/. The full tables stay in research/exports/tables
// and are not shipped to the browser.
//
// Run from neko-lite/ after the research scripts have rewritten their tables:
//   node scripts/extract-tables.mjs

import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const TABLES = resolve(here, "../../research/exports/tables");
const OUT = resolve(here, "../src/core/data");

const read = (name) => JSON.parse(readFileSync(resolve(TABLES, name), "utf8"));
const pick = (source, keys) => Object.fromEntries(keys.map((key) => [key, source[key]]));

function write(name, value) {
  writeFileSync(resolve(OUT, name), `${JSON.stringify(value, null, 1)}\n`);
  console.log(`${name}: ${JSON.stringify(value).length} bytes`);
}

mkdirSync(OUT, { recursive: true });

// Weekly calls: per coin and side, whether it is called, at what leverage, and its record.
{
  const calls = {};
  for (const [coin, sides] of Object.entries(read("crypto_long_short.json").calls)) {
    calls[coin] = {};
    for (const [side, c] of Object.entries(sides)) {
      const byLeverage = {};
      for (const [level, at] of Object.entries(c.by_leverage)) {
        byLeverage[level] = pick(at, ["weeks", "ahead", "liquidated", "avg_per_week"]);
      }
      calls[coin][side] = {
        allowed: c.allowed,
        suggested_leverage: c.suggested_leverage,
        first_half: pick(c.first_half, ["right", "avg_pct", "coin_weeks"]),
        second_half: pick(c.second_half, ["right", "avg_pct", "coin_weeks"]),
        by_leverage: byLeverage,
      };
    }
  }
  write("calls.json", calls);
}

// The stop, in normal weeks, with the tested effect.
{
  const rec = read("crypto_exits.json").recommendation;
  const byCall = {};
  for (const [call, record] of Object.entries(rec.by_call || {})) byCall[call] = { all: record.all };
  write("exits.json", {
    stop: rec.stop,
    with: rec.with.all,
    holding: rec.holding.all,
    by_call: byCall,
  });
}

// Per side, the take profit the test allows, in normal weeks (null: hold to Monday).
{
  const sides = {};
  for (const [side, s] of Object.entries(read("crypto_take_profit.json").by_side)) {
    sides[side] = {
      target: s.target ?? null,
      with: s.with.all,
      stop_only: s.stop_only.all,
      at_one_normal_week: s.at_one_normal_week ?? null,
      plans_as_good: s.plans_as_good,
      plans_tested: s.plans_tested,
    };
  }
  write("take_profit.json", sides);
}

// The long side without leverage, against simply holding.
{
  const data = read("crypto_in_or_out.json");
  const rows = Object.fromEntries(data.baskets["BTC and ETH"].map((row) => [row.rule, row]));
  write("spot_record.json", {
    rule: rows["In when mood and 4 weeks agree"],
    hold: rows["Always hold"],
    from: data.from, to: data.to, split: data.split,
  });
}

// What the Fear & Greed index has meant as a weekly signal.
{
  const data = read("crypto_weekly_search.json");
  const rows = Object.fromEntries(data.rows.map((row) => [row.rule, row]));
  const half = (row) => {
    if (!row) return null;
    const out = {};
    for (const name of ["choose", "check"]) {
      out[name] = pick(row.no_costs[name], ["accuracy", "avg_pct", "coin_weeks"]);
    }
    return out;
  };
  write("mood_evidence.json", {
    with_mood: half(rows["Long when greedy, short when fearful"]),
    extreme: half(rows["Sell extreme fear, buy extreme greed"]),
    holding: half(rows["Always long"]),
    from: data.from ?? null, to: data.to ?? null, split: data.split ?? null,
    coins: (data.coins || []).length,
  });
}
