/** Standardized Call:Put ratio used across the app (Multi-TF, Change-in-OI, Ratio page).
 *
 * The ratio is normalized so the SMALLER side is always 1 and shown in Call : Put
 * order (e.g. 2L call / 10L put -> "1 : 5"). "Side" is the DOMINANT side — whichever
 * OI change has the larger magnitude (Call larger -> CALL, Put larger -> PUT, equal
 * or both flat -> NEUTRAL). Inputs are OI *changes* (in whole contracts) and may be
 * negative (unwinding); magnitudes drive the ratio/side.
 */

export type DominantSide = "CALL" | "PUT" | "NEUTRAL";

export interface CallPutRatio {
  /** Whichever side has the larger |OI change|. */
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

  let side: DominantSide;
  if (a < EPS && b < EPS) side = "NEUTRAL";
  else if (a === b) side = "NEUTRAL";
  else side = a > b ? "CALL" : "PUT";

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
