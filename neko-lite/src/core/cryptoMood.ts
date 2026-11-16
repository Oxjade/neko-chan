// Crypto mood for the week: the Fear & Greed index and what it has meant.
//
// Port of service/server/crypto_mood.py. The rule uses the reading on the
// week's last day (Sunday, UTC) for the week that follows. The index is
// market-wide, so it gives one direction for all coins.

import { cached } from "./cache";
import evidenceTable from "./data/mood_evidence.json";
import { SUNDAY, dayOf, isoDay, weekday } from "./dates";
import { DataError, FNG_URL, onReset, runtime } from "./runtime";
import type { MoodReading, Side } from "./types";

const HISTORY_DAYS = 200;
const RECENT_DAYS = 21;

/** One day's reading. `day` counts days since 1970 (UTC). */
export interface Reading {
  day: number;
  value: number;
  label: string;
}

export interface MoodEvidenceHalf {
  accuracy: number;
  avg_pct: number;
  coin_weeks: number;
}

export interface MoodEvidence {
  with_mood: { choose: MoodEvidenceHalf; check: MoodEvidenceHalf } | null;
  extreme: { choose: MoodEvidenceHalf; check: MoodEvidenceHalf } | null;
  holding: { choose: MoodEvidenceHalf; check: MoodEvidenceHalf } | null;
  from: string | null;
  to: string | null;
  split: string | null;
  coins: number;
}

export interface MoodWeek {
  today: MoodReading;
  week_reading: MoodReading;
  change_from_last_week: number | null;
  rules: { with_mood: Side; extreme: Side | null };
  evidence: MoodEvidence | null;
}

interface RawReading {
  value: string;
  value_classification?: string;
  timestamp: string;
}

const KEEP_SECONDS = 3600;
const KEEP_WHEN_DUE_SECONDS = 60;
let updateDue = false;
onReset(() => { updateDue = false; });

/**
 * Just after midnight on Monday the week's prices are in but the index has not
 * published the new day yet. While that is so, look for the new reading every
 * minute instead of every hour, so the new call appears as soon as it can.
 */
export function expectUpdate(due: boolean): void {
  updateDue = due;
}

/**
 * The last 200 daily readings, newest first. One request serves both the
 * week's reading and the look back over past weeks; it is repeated hourly
 * because the index changes once a day.
 */
function download(): Promise<Reading[]> {
  return cached("fear_greed", updateDue ? KEEP_WHEN_DUE_SECONDS : KEEP_SECONDS, async () => {
    const fail = (reason: string) => new DataError(`The mood index is unavailable right now (${reason}).`);
    let response: Response;
    try {
      response = await runtime.fetch(`${FNG_URL}?limit=${HISTORY_DAYS}&format=json`);
    } catch {
      throw fail("ConnectionError");
    }
    if (!response.ok) throw fail("HTTPError");
    let data: RawReading[];
    try {
      data = (await response.json() as { data: RawReading[] }).data;
    } catch {
      throw fail("JSONDecodeError");
    }
    if (!Array.isArray(data)) throw fail("KeyError");
    return data.map((d) => ({
      day: dayOf(Math.trunc(Number(d.timestamp)) * 1000),
      value: Math.trunc(Number(d.value)),
      label: d.value_classification ?? "",
    }));
  });
}

/** Recent daily readings, newest first. */
export async function readings(): Promise<Reading[]> {
  return (await download()).slice(0, RECENT_DAYS);
}

/** Daily readings by day, for looking back over past weeks. */
export async function history(): Promise<Map<number, number>> {
  const byDay = new Map<number, number>();
  for (const reading of await download()) byDay.set(reading.day, reading.value);
  return byDay;
}

/** What the two tested rules say for a reading. */
export function ruleDirection(value: number): { with_mood: Side; extreme: Side | null } {
  return {
    with_mood: value >= 50 ? "long" : "short",
    extreme: value > 75 ? "long" : value < 25 ? "short" : null,
  };
}

function words(reading: Reading): MoodReading {
  return { value: reading.value, label: reading.label, date: isoDay(reading.day) };
}

/** This week's reading from a list of recent readings (newest first). */
export function weekFrom(recent: Reading[]): MoodWeek {
  const today = recent[0];
  // A week runs Monday to Monday and is governed by the Sunday reading before
  // it. On a Sunday the current week is therefore still last Sunday's.
  const sunday = recent.find((r) => weekday(r.day) === SUNDAY && r.day < today.day) ?? today;
  const weekAgo = recent.find((r) => sunday.day - r.day === 7) ?? null;
  return {
    today: words(today),
    week_reading: words(sunday),
    change_from_last_week: weekAgo ? sunday.value - weekAgo.value : null,
    rules: ruleDirection(sunday.value),
    evidence: evidenceTable as MoodEvidence,
  };
}

export async function thisWeek(): Promise<MoodWeek> {
  const recent = await readings();
  if (!recent.length) throw new DataError("The mood index is unavailable right now (KeyError).");
  return weekFrom(recent);
}
