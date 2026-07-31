/** Shared UI helpers (hidden-dashboard copy — keep in sync with `frontend/src/utils/ui.ts`). */

import type { DominantSide } from "./ratio";

/** Canonical Δ colour: positive = green (building), negative = red (unwinding),
 *  flat = muted — the same for both Call and Put. */
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
