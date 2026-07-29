/** IST trading-session time helpers.
 *
 * Generalizes the timestamp math that used to live inline in `Dashboard.tsx`
 * (which only ever worked for *today* via `health.now_ist`). These helpers work
 * for ANY trading date, so the custom-range slider, historical date picker and
 * replay all build correct `from_ts`/`to_ts` on whatever day is selected — the
 * fix for the "blank chart when off-hours / past date" bug.
 */
import type { HealthResponse } from "../types";

// NSE regular session, minutes-from-midnight (IST). 09:15 → 15:30 = 375 minutes.
export const SESSION_OPEN_MIN = 9 * 60 + 15; // 555
export const SESSION_CLOSE_MIN = 15 * 60 + 30; // 930
export const SESSION_SPAN_MIN = SESSION_CLOSE_MIN - SESSION_OPEN_MIN; // 375

export const clamp = (v: number, lo: number, hi: number) => Math.min(Math.max(v, lo), hi);

/** Parse an IST ISO string ("YYYY-MM-DDTHH:MM:SS...+05:30") into date + minutes-from-open. */
export function parseIstDateAndMinute(
  iso: string | null | undefined,
): { date: string; sinceOpen: number } | null {
  if (!iso || iso.length < 16) return null;
  const date = iso.slice(0, 10);
  const hh = Number(iso.slice(11, 13));
  const mm = Number(iso.slice(14, 16));
  if (Number.isNaN(hh) || Number.isNaN(mm)) return null;
  return { date, sinceOpen: hh * 60 + mm - SESSION_OPEN_MIN };
}

/** Build an IST ISO timestamp for a given minutes-from-open on a trading date. */
export function isoForSessionMinuteOnDate(dateStr: string, min: number): string {
  const total = SESSION_OPEN_MIN + Math.round(min);
  const hh = Math.floor(total / 60);
  const mm = total % 60;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${dateStr}T${p(hh)}:${p(mm)}:00+05:30`;
}

/** The IST calendar date (YYYY-MM-DD) reported by the backend health poll. */
export function todayIstDate(health: HealthResponse | null): string | null {
  return health?.now_ist ? health.now_ist.slice(0, 10) : null;
}

/** True when `dateStr` is the current IST trading date per the health clock. */
export function isToday(dateStr: string | null, health: HealthResponse | null): boolean {
  return dateStr != null && dateStr === todayIstDate(health);
}

/**
 * The highest valid slider minute for a date. On *today* it clamps to the live
 * edge (minutes elapsed since open); on any past date the full 375-min session
 * is available. Returns 0 before the open / when the clock is unavailable.
 */
export function maxMinForDate(dateStr: string | null, health: HealthResponse | null): number {
  if (dateStr && !isToday(dateStr, health)) return SESSION_SPAN_MIN;
  const now = parseIstDateAndMinute(health?.now_ist);
  if (!now) return SESSION_SPAN_MIN; // clock unknown: allow the full session rather than blank
  return clamp(now.sinceOpen, 0, SESSION_SPAN_MIN);
}

/** Format minutes-from-open as a 12-hour IST clock label (e.g. "10:25 AM"). */
export function formatSessionMinute(min: number): string {
  const total = SESSION_OPEN_MIN + Math.round(min);
  let hh = Math.floor(total / 60);
  const mm = total % 60;
  const ampm = hh >= 12 ? "PM" : "AM";
  hh = hh % 12 || 12;
  return `${hh}:${String(mm).padStart(2, "0")} ${ampm}`;
}
