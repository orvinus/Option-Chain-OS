/** Standardized Call:Put ratio + dominant Side used across the app (Multi-TF,
 * Change-in-OI, Replay).
 *
 * The ratio TEXT is normalized so the SMALLER side is always 1 and shown in
 * Call : Put order (e.g. 2L call / 10L put -> "1 : 5"); it is driven by
 * MAGNITUDES and is unchanged by the Side rule below.
 *
 * "Side" is the DOMINANT side per the finalized spec: the side whose *signed*
 * OI change is the SMALLER (lower) value — signs included. So the more-negative
 * (or less-positive) side wins: Call smaller -> CALL, Put smaller -> PUT, equal
 * -> NEUTRAL. Examples: +4Cr/+2Cr -> PUT, +4Cr/-2Cr -> PUT, -4Cr/-2Cr -> CALL,
 * +3Cr/+3Cr -> NEUTRAL. Inputs are OI *changes* (in whole contracts) and may be
 * negative (unwinding).
 */

export type DominantSide = "CALL" | "PUT" | "NEUTRAL";

export interface CallPutRatio {
  /** Dominant side = whichever OI change is the SMALLER *signed* value (Neutral on tie). */
  side: DominantSide;
  /** Normalized "Call : Put" text with the smaller side pinned to 1 (e.g. "1 : 5", "5 : 1", "1 : 1"). */
  text: string;
  /** |call| / |put| (null when put is ~0). */
  callPerPut: number | null;
  /** |put| / |call| (null when call is ~0). */
  putPerCall: number | null;
}

// OI is measured in whole contracts, so treat sub-1 magnitudes as flat.
const EPS = 1;

/** Format a normalized ratio part: integer as-is, else ≤2dp with trailing zeros trimmed. */
function fmtPart(n: number): string {
  if (!Number.isFinite(n)) return "—";
  if (Number.isInteger(n)) return String(n);
  return n.toFixed(2).replace(/\.?0+$/, "");
}

export function callPutRatio(callChange: number, putChange: number): CallPutRatio {
  const a = Math.abs(callChange);
  const b = Math.abs(putChange);

  // Dominant Side = the side with the SMALLER *signed* OI change (Neutral on a
  // tie). Note this is independent of the magnitude-based ratio text below.
  let side: DominantSide;
  if (Math.abs(callChange - putChange) < EPS) side = "NEUTRAL";
  else side = callChange < putChange ? "CALL" : "PUT";

  const callPerPut = b > 0 ? a / b : null;
  const putPerCall = a > 0 ? b / a : null;

  let text: string;
  if (a < EPS && b < EPS) {
    text = "1 : 1";
  } else if (a < EPS) {
    text = "0 : 1"; // only puts moved
  } else if (b < EPS) {
    text = "1 : 0"; // only calls moved
  } else {
    const base = Math.min(a, b);
    text = `${fmtPart(a / base)} : ${fmtPart(b / base)}`;
  }

  return { side, text, callPerPut, putPerCall };
}
