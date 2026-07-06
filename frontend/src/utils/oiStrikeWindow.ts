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

/** Strike ladder window filter.
 *  atmWindow = -1  → all rows (no filter)
 *  atmWindow =  0  → ATM strike only
 *  atmWindow =  N  → ATM ± N strikes
 *  stepHint: the symbol's registry strike_step; inferred from the first two
 *  strikes only as a fallback (inference breaks if the ladder has a gap).
 */
export function filterOiRowsByAtmWindow<T extends { strike: number }>(
  allRows: T[],
  spot: number | null,
  atmWindow: number,
  stepHint?: number | null,
): T[] {
  if (spot == null || atmWindow < 0) return allRows;
  const allStrikes = allRows.map((r) => r.strike);
  const atm = atmStrike(allStrikes, spot);
  const step = stepHint ?? (allStrikes.length > 1 ? allStrikes[1] - allStrikes[0] : 50);
  const lo = atm - atmWindow * step;
  const hi = atm + atmWindow * step;
  return allRows.filter((r) => r.strike >= lo && r.strike <= hi);
}
