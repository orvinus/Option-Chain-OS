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

/** Canonical ATM strike = arithmetic round of spot to the nearest step. Matches
 *  the backend `atm_strike` (`round(spot/step)*step`) so every client window and
 *  every displayed ATM agrees with the server (and with each other). */
export function atmRound(spot: number, step: number): number {
  return Math.round(spot / step) * step;
}

/** Effective strike window: a picked N means EXACTLY ATM ± N strikes, with the
 *  sentinels preserved (0 = ATM only, <0 = All).
 *
 *  Until 2026-09-14 this returned N-1 ("one fewer each side than picked"). Algo
 *  Config never did that — its `strikes_atm_window` is a literal ATM ± N
 *  (`series._resolve_strike_basket`) — so the same "10" summed 19 strikes on the
 *  dashboard and 21 in Algo Config, and the SAME ratio crossover read 10:16 on
 *  Replay against 10:15 in Algo Config. Every dashboard page reads the window
 *  through this one function, so this is the single place that keeps them equal.
 *  Do not reintroduce an offset here without changing the engine to match. */
export function effectiveAtmWindow(atmWindow: number): number {
  return atmWindow;
}

/** Strike ladder window filter.
 *  atmWindow = -1  → all rows (no filter)
 *  atmWindow =  0  → ATM strike only
 *  atmWindow =  N  → ATM ± N strikes  (same basket as Algo Config's ATM ± N)
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
  const w = effectiveAtmWindow(atmWindow);
  const allStrikes = allRows.map((r) => r.strike);
  const step = stepHint ?? (allStrikes.length > 1 ? allStrikes[1] - allStrikes[0] : 50);
  // Center on the arithmetic ATM (== backend `atm_strike`) so this window matches
  // the server's and the displayed ATM everywhere.
  const atm = atmRound(spot, step);
  const lo = atm - w * step;
  const hi = atm + w * step;
  return allRows.filter((r) => r.strike >= lo && r.strike <= hi);
}
