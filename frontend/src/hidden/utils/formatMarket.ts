/** Compact strike-table formatting */
export function fmtInt(n: number): string {
  return n.toLocaleString();
}

/** OI / Volume — shows "—" when value is exactly 0 (no market on that side) */
export function fmtOi(n: number): string {
  if (n === 0) return "—";
  return n.toLocaleString();
}

export function fmtFloat(x: number | null | undefined, digits = 2): string {
  if (x == null || !Number.isFinite(x)) return "—";
  return x.toFixed(digits);
}

/** IV stored as decimal annualized (e.g. 0.18) → display % */
export function fmtIvPct(iv: number | null | undefined): string {
  if (iv == null || !Number.isFinite(iv)) return "—";
  return `${(iv * 100).toFixed(2)}%`;
}

export function fmtRatio(x: number | null | undefined, digits = 3): string {
  if (x == null || !Number.isFinite(x)) return "—";
  return x.toFixed(digits);
}

export function trendWords(code: string): "Bullish" | "Bearish" | "—" {
  if (code === "LB" || code === "SC") return "Bullish";
  if (code === "SB" || code === "LU") return "Bearish";
  return "—";
}
