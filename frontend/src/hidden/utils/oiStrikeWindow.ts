export function nearestIdx(strikes: number[], spot: number): number {
  let bestIdx = 0;
  let bestDiff = Infinity;
  for (let i = 0; i < strikes.length; i++) {
    const d = Math.abs(strikes[i] - spot);
    if (d < bestDiff) {
      bestDiff = d;
      bestIdx = i;
    }
  }
  return bestIdx;
}

export function atmStrike(strikes: number[], spot: number): number {
  return strikes[nearestIdx(strikes, spot)] ?? spot;
}

/** Effective strike window: one fewer strike on each side than the picked value,
 *  while preserving the sentinels (0 = ATM only, <0 = All). So a picked N shows
 *  ATM ± (N-1). Keep this in sync with the main-dashboard copy. */
export function effectiveAtmWindow(atmWindow: number): number {
  return atmWindow > 0 ? atmWindow - 1 : atmWindow;
}

/** Strike ladder window filter.
 *  atmWindow = -1  → all rows (no filter)
 *  atmWindow =  0  → ATM strike only
 *  atmWindow =  N  → ATM ± (N-1) strikes  (one fewer each side than the picked N)
 */
export function filterOiRowsByAtmWindow<T extends { strike: number }>(
  allRows: T[],
  spot: number | null,
  atmWindow: number,
): T[] {
  if (spot == null || atmWindow < 0) return allRows;
  const w = effectiveAtmWindow(atmWindow);
  const allStrikes = allRows.map((r) => r.strike);
  const atm = atmStrike(allStrikes, spot);
  const step = allStrikes.length > 1 ? allStrikes[1] - allStrikes[0] : 50;
  const lo = atm - w * step;
  const hi = atm + w * step;
  return allRows.filter((r) => r.strike >= lo && r.strike <= hi);
}
