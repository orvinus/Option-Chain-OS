import type { OptionChainFullRow } from "../types";

function nz(n: number | null | undefined): number {
  return n ?? 0;
}

/** Net = puts − calls (PE−CE style). */
export function netStyle(a: number, b: number): number {
  return a - b;
}

export function ratioSafe(num: number, den: number): number | null {
  if (den === 0) return null;
  return num / den;
}

/** Table 2 single-value bullish / bearish sums (absolute OI). */
export function totalBullishOi(rows: OptionChainFullRow[]): number {
  let s = 0;
  for (const r of rows) {
    if (r.call_trend === "LB" || r.call_trend === "SC") s += r.call_oi;
    if (r.put_trend === "SB" || r.put_trend === "LU") s += r.put_oi;
  }
  return s;
}

export function totalBearishOi(rows: OptionChainFullRow[]): number {
  let s = 0;
  for (const r of rows) {
    if (r.call_trend === "SB" || r.call_trend === "LU") s += r.call_oi;
    if (r.put_trend === "LB" || r.put_trend === "SC") s += r.put_oi;
  }
  return s;
}

export function totalBullishOiChg(rows: OptionChainFullRow[]): number {
  let s = 0;
  for (const r of rows) {
    if (r.call_trend === "LB" || r.call_trend === "SC") s += r.call_oi_change;
    if (r.put_trend === "SB" || r.put_trend === "LU") s += r.put_oi_change;
  }
  return s;
}

export function totalBearishOiChg(rows: OptionChainFullRow[]): number {
  let s = 0;
  for (const r of rows) {
    if (r.call_trend === "SB" || r.call_trend === "LU") s += r.call_oi_change;
    if (r.put_trend === "LB" || r.put_trend === "SC") s += r.put_oi_change;
  }
  return s;
}

export function sumOiByCallTrend(rows: OptionChainFullRow[], trend: string): number {
  return rows.filter((r) => r.call_trend === trend).reduce((s, r) => s + r.call_oi, 0);
}

export function sumOiByPutTrend(rows: OptionChainFullRow[], trend: string): number {
  return rows.filter((r) => r.put_trend === trend).reduce((s, r) => s + r.put_oi, 0);
}

export function sumOiChgByCallTrend(rows: OptionChainFullRow[], trend: string): number {
  return rows.filter((r) => r.call_trend === trend).reduce((s, r) => s + r.call_oi_change, 0);
}

export function sumOiChgByPutTrend(rows: OptionChainFullRow[], trend: string): number {
  return rows.filter((r) => r.put_trend === trend).reduce((s, r) => s + r.put_oi_change, 0);
}

/** OTM calls: CE where strike > spot. OTM puts: PE where strike < spot. */
export function partitionOtm(
  rows: OptionChainFullRow[],
  spot: number,
): { callOi: number; putOi: number; callOiChg: number; putOiChg: number; callVol: number; putVol: number } {
  let callOi = 0,
    putOi = 0,
    callOiChg = 0,
    putOiChg = 0,
    callVol = 0,
    putVol = 0;
  for (const r of rows) {
    if (r.strike > spot) {
      callOi += r.call_oi;
      callOiChg += r.call_oi_change;
      callVol += r.call_volume;
    }
    if (r.strike < spot) {
      putOi += r.put_oi;
      putOiChg += r.put_oi_change;
      putVol += r.put_volume;
    }
  }
  return { callOi, putOi, callOiChg, putOiChg, callVol, putVol };
}

/** ITM calls: CE strike < spot. ITM puts: PE strike > spot. */
export function partitionItm(
  rows: OptionChainFullRow[],
  spot: number,
): { callOi: number; putOi: number; callOiChg: number; putOiChg: number; callVol: number; putVol: number } {
  let callOi = 0,
    putOi = 0,
    callOiChg = 0,
    putOiChg = 0,
    callVol = 0,
    putVol = 0;
  for (const r of rows) {
    if (r.strike < spot) {
      callOi += r.call_oi;
      callOiChg += r.call_oi_change;
      callVol += r.call_volume;
    }
    if (r.strike > spot) {
      putOi += r.put_oi;
      putOiChg += r.put_oi_change;
      putVol += r.put_volume;
    }
  }
  return { callOi, putOi, callOiChg, putOiChg, callVol, putVol };
}

export function totalsRowCore(rows: OptionChainFullRow[]) {
  const totalCallOi = rows.reduce((s, r) => s + r.call_oi, 0);
  const totalPutOi = rows.reduce((s, r) => s + r.put_oi, 0);
  const totalCallChg = rows.reduce((s, r) => s + r.call_oi_change, 0);
  const totalPutChg = rows.reduce((s, r) => s + r.put_oi_change, 0);
  const totalCallVol = rows.reduce((s, r) => s + r.call_volume, 0);
  const totalPutVol = rows.reduce((s, r) => s + r.put_volume, 0);
  const totalCallLtp = rows.reduce((s, r) => s + nz(r.call_ltp), 0);
  const totalPutLtp = rows.reduce((s, r) => s + nz(r.put_ltp), 0);
  const totalCallLtpChg = rows.reduce((s, r) => s + nz(r.call_ltp_change), 0);
  const totalPutLtpChg = rows.reduce((s, r) => s + nz(r.put_ltp_change), 0);
  return {
    totalCallOi,
    totalPutOi,
    totalCallChg,
    totalPutChg,
    totalCallVol,
    totalPutVol,
    totalCallLtp,
    totalPutLtp,
    totalCallLtpChg,
    totalPutLtpChg,
  };
}
