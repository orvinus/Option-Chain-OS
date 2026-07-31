/** Compact Indian-market number formatting (lakh / crore) for OI values.
 *  Hidden-dashboard copy — keep in sync with `frontend/src/utils/num.ts`. */
export function compact(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  const a = Math.abs(n);
  const sign = n < 0 ? "-" : "";
  if (a >= 1e7) return `${sign}${(a / 1e7).toFixed(2)}Cr`;
  if (a >= 1e5) return `${sign}${(a / 1e5).toFixed(2)}L`;
  if (a >= 1e3) return `${sign}${(a / 1e3).toFixed(1)}k`;
  return `${sign}${a}`;
}

/** Signed compact with an explicit leading + for positive values. */
export function signedCompact(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  return (n > 0 ? "+" : "") + compact(n);
}

/** Fixed 2-decimal display, or an em dash for null/undefined/NaN. */
export function fmt2(n: number | null | undefined): string {
  return n == null || Number.isNaN(n) ? "—" : n.toFixed(2);
}
