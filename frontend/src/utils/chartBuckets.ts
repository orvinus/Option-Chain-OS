/** Pure client-side re-aggregation helpers for the engine panels' charts.
 *
 * The engines run on CLOSED 1-minute buckets (index-aligned arrays). The
 * dashboard's Ratio tab shows the same lines at 5/10/15/30-minute cadence
 * (backend `time_bucket` + `last(oi)` = the level at bucket CLOSE) and a
 * "Full Day" cumulative mode (RatioChartPage). These helpers reproduce both
 * from the engine's 1-minute arrays so the panels agree with the dashboard
 * without a second fetch, and adapt the engine's Crore series to the
 * `oiCandles.ts` candle math used by the Charts tab.
 */
import type { UTCTimestamp } from "lightweight-charts";
import { istIsoToChartTime } from "../components/charts/chartTime";
import type { LinePoint } from "../components/charts/TimeSeriesChart";
import { labelToMin, minToLabel, SESSION_OPEN_MIN, type ChangePoint } from "./oiCandles";
import { compact } from "./num";

/** Session-anchored bucket ordinal (09:15 = bucket 0) for an IST ISO stamp. */
export function bucketKey(tsIso: string, intervalMin: number): number {
  if (intervalMin <= 1) return labelToMin(tsIso.slice(11, 16));
  return Math.floor((labelToMin(tsIso.slice(11, 16)) - SESSION_OPEN_MIN) / intervalMin);
}

/** ISO stamp of the bucket START holding `tsIso` (identity for 1-minute). */
export function bucketStartIso(tsIso: string, intervalMin: number): string {
  if (intervalMin <= 1) return tsIso;
  const key = bucketKey(tsIso, intervalMin);
  return `${tsIso.slice(0, 11)}${minToLabel(SESSION_OPEN_MIN + key * intervalMin)}${tsIso.slice(16)}`;
}

/** lightweight-charts time of the bucket START holding `tsIso`. */
export function bucketChartTime(tsIso: string, intervalMin: number): UTCTimestamp {
  return istIsoToChartTime(bucketStartIso(tsIso, intervalMin));
}

export interface BucketedSeries {
  times: UTCTimestamp[];
  values: number[];
  /** Index (into the 1-minute arrays) of the last minute of each bucket. */
  indexOfLastMinute: number[];
}

/**
 * Last closed minute per session-anchored bucket — the level at bucket close,
 * which is exactly what the backend `time_bucket` + `last(oi)` path gives the
 * Ratio tab for 5m/10m/15m/30m. Interval 1 is the identity.
 * `values` may be shorter than `timestamps` (an index beyond it is skipped).
 */
export function lastInBucket(timestamps: string[], values: number[], intervalMin: number): BucketedSeries {
  const times: UTCTimestamp[] = [];
  const vals: number[] = [];
  const idx: number[] = [];
  const n = Math.min(timestamps.length, values.length);
  if (intervalMin <= 1) {
    for (let i = 0; i < n; i++) {
      times.push(istIsoToChartTime(timestamps[i]));
      vals.push(values[i]);
      idx.push(i);
    }
    return { times, values: vals, indexOfLastMinute: idx };
  }
  let curKey: number | null = null;
  for (let i = 0; i < n; i++) {
    const key = bucketKey(timestamps[i], intervalMin);
    if (key !== curKey) {
      curKey = key;
      times.push(bucketChartTime(timestamps[i], intervalMin));
      vals.push(values[i]);
      idx.push(i);
    } else {
      vals[vals.length - 1] = values[i];
      idx[idx.length - 1] = i;
    }
  }
  return { times, values: vals, indexOfLastMinute: idx };
}

export interface CumulativeRatio {
  ratio: LinePoint[];
  pcr: LinePoint[];
  positions: Map<number, { call: number; put: number }>;
}

/**
 * The dashboard's "Full Day" mode (RatioChartPage): cumulative Call/Put OI
 * change since the first point; ratio = cumCall ÷ cumPut, PCR = cumPut ÷
 * cumCall; zero denominators are skipped so the line gaps.
 */
export function cumulativeRatioSeries(
  timestamps: string[],
  totalCall: number[],
  totalPut: number[],
): CumulativeRatio {
  const positions = new Map<number, { call: number; put: number }>();
  const ratio: LinePoint[] = [];
  const pcr: LinePoint[] = [];
  const n = Math.min(timestamps.length, totalCall.length, totalPut.length);
  if (n === 0) return { ratio, pcr, positions };
  const baseCall = totalCall[0];
  const basePut = totalPut[0];
  for (let i = 0; i < n; i++) {
    const t = istIsoToChartTime(timestamps[i]);
    const cumCall = totalCall[i] - baseCall;
    const cumPut = totalPut[i] - basePut;
    positions.set(t, { call: cumCall, put: cumPut });
    if (cumPut !== 0) ratio.push({ time: t, value: cumCall / cumPut });
    if (cumCall !== 0) pcr.push({ time: t, value: cumPut / cumCall });
  }
  return { ratio, pcr, positions };
}

/**
 * Adapt the engine's cumulative-Δ series (Crores, index-aligned with
 * `timestamps`) to `ChangePoint`s so `bucketToCandles` / `toLineSeries` from
 * `oiCandles.ts` work on it unchanged.
 */
export function toChangePointsCr(timestamps: string[], seriesCr: number[]): ChangePoint[] {
  const n = Math.min(timestamps.length, seriesCr.length);
  const out: ChangePoint[] = [];
  for (let i = 0; i < n; i++) {
    out.push({ tMs: Date.parse(timestamps[i]), label: timestamps[i].slice(11, 16), value: seriesCr[i] });
  }
  return out;
}

/** Crore value → the dashboard's compact label ("12.50L", "1.20Cr"). */
export function fmtCr(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  return compact(v * 1e7);
}
