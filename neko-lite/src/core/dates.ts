// Calendar days in UTC. A day is a whole number of days since 1970-01-01,
// which makes "seven days earlier" plain subtraction.

export const HOUR_MS = 3600 * 1000;
export const DAY_MS = 24 * HOUR_MS;

/** The UTC day a moment falls on. */
export function dayOf(ms: number): number {
  return Math.floor(ms / DAY_MS);
}

/** "2026-11-15" for a day number. */
export function isoDay(day: number): string {
  return new Date(day * DAY_MS).toISOString().slice(0, 10);
}

/** Day number for "2026-11-15". */
export function parseDay(iso: string): number {
  return Math.floor(Date.parse(`${iso.slice(0, 10)}T00:00:00Z`) / DAY_MS);
}

/** Monday is 0 and Sunday is 6, as in Python. */
export function weekday(day: number): number {
  return (((day + 3) % 7) + 7) % 7;
}

export const SUNDAY = 6;

/** A moment as Python's datetime.isoformat() writes it in UTC: "2026-11-15T21:33:23.714000+00:00". */
export function isoMoment(ms: number): string {
  const text = new Date(ms).toISOString();           // 2026-11-15T21:33:23.714Z
  const millis = text.slice(20, 23);
  return `${text.slice(0, 19)}${millis === "000" ? "" : `.${millis}000`}+00:00`;
}
