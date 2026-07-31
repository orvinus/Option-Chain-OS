/** Shared UI helpers used across the main dashboard pages. */

import type { DominantSide } from "./ratio";

/**
 * Canonical Δ (change) colour convention, used everywhere an OI change is shown:
 * positive = green (building), negative = red (unwinding), flat = muted — the
 * SAME for both Call and Put. Keep this the single source so the OI-Change,
 * Multi-TF, and Replay views never disagree on what a "+" looks like.
 */
export function changeColor(v: number): string {
  return v > 0 ? "text-emerald-400" : v < 0 ? "text-red-400" : "text-muted";
}

/** Colour for a dominant Side chip: Call = green, Put = red, Neutral = muted. */
export function sideColor(s: DominantSide): string {
  return s === "CALL" ? "text-emerald-400" : s === "PUT" ? "text-red-400" : "text-muted";
}

/** Human label for a dominant Side. */
export function sideLabel(s: DominantSide): string {
  return s === "CALL" ? "Call" : s === "PUT" ? "Put" : "Neutral";
}
