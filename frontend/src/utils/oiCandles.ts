import type { OITimeseriesPoint } from "../types";

/** Chart intervals: 1m renders a line; 5/10/15/30m render candlesticks. */
export type ChartInterval = 1 | 5 | 10 | 15 | 30;

export const CANDLE_INTERVALS: ChartInterval[] = [5, 10, 15, 30];
export const LINE_INTERVAL: ChartInterval = 1;

export type OISide = "call" | "put";

/** A 1-minute sample of total OI expressed as change-since-session-open. */
export interface ChangePoint {
  /** Epoch ms (for ordering only). */
  tMs: number;
  /** HH:MM (IST) label. */
  label: string;
  /** Total OI change vs the first point of the session (0-anchored, ±). */
  value: number;
}

/** OHLC candle: tuple ordered [open, close, low, high] to match ECharts. */
export interface Candle {
  label: string;
  ohlc: [number, number, number, number];
}

function pick(p: OITimeseriesPoint, side: OISide): number {
  return side === "call" ? p.total_call_oi : p.total_put_oi;
}

/** NSE/BSE regular session open (09:15 IST) in minutes-from-midnight. */
const SESSION_OPEN_MIN = 9 * 60 + 15;

/** Minutes-from-midnight from an "HH:MM" label. */
function labelToMin(label: string): number {
  const [hh, mm] = label.split(":").map(Number);
  return hh * 60 + mm;
}

function minToLabel(min: number): string {
  const hh = Math.floor(min / 60);
  const mm = min % 60;
  return `${String(hh).padStart(2, "0")}:${String(mm).padStart(2, "0")}`;
}

/**
 * Convert raw per-minute totals into a change-since-open series for one side.
 * The first stored bucket of the session is the baseline (value 0); every later
 * point is its total OI minus that baseline, so the series oscillates around 0.
 * Backend timestamps are IST ISO ("...T09:15:00+05:30"); the HH:MM label is
 * sliced directly to avoid timezone math.
 */
export function toChangeSinceOpen(
  points: OITimeseriesPoint[],
  side: OISide,
): ChangePoint[] {
  if (points.length === 0) return [];
  const base = pick(points[0], side);
  return points.map((p) => ({
    tMs: Date.parse(p.ts),
    label: p.ts.slice(11, 16),
    value: pick(p, side) - base,
  }));
}

/**
 * What the "Δ since …" baseline actually is. When the first stored bucket is
 * the session open (±1 min) this is "open"; when data starts later (backend
 * booted mid-session) the change series is baselined at that first bucket, and
 * labelling it "since open" would misstate the numbers.
 */
export function baselineLabel(points: OITimeseriesPoint[] | null): string {
  const first = points?.[0]?.ts.slice(11, 16);
  if (!first) return "open";
  return labelToMin(first) <= SESSION_OPEN_MIN + 1 ? "open" : first;
}

/**
 * Bucket a 1-minute change series into OHLC candles of `intervalMin` minutes,
 * anchored to the session open (09:15 → 09:15, 09:25, ... for 10m). Anchoring
 * at midnight instead put 09:15–09:29 into a candle labelled "09:00" for any
 * interval that does not divide 555 evenly (10m, 30m). Within each bucket:
 * open = first minute, close = last minute, low/high = min/max.
 */
export function bucketToCandles(
  series: ChangePoint[],
  intervalMin: number,
): Candle[] {
  if (series.length === 0 || intervalMin <= 1) {
    return series.map((p) => ({ label: p.label, ohlc: [p.value, p.value, p.value, p.value] }));
  }
  const buckets = new Map<number, ChangePoint[]>();
  for (const pt of series) {
    const key = Math.floor((labelToMin(pt.label) - SESSION_OPEN_MIN) / intervalMin);
    const arr = buckets.get(key);
    if (arr) arr.push(pt);
    else buckets.set(key, [pt]);
  }
  return [...buckets.keys()]
    .sort((a, b) => a - b)
    .map((key) => {
      const vals = buckets.get(key)!.map((p) => p.value);
      const open = vals[0];
      const close = vals[vals.length - 1];
      const low = Math.min(...vals);
      const high = Math.max(...vals);
      return {
        label: minToLabel(SESSION_OPEN_MIN + key * intervalMin),
        ohlc: [open, close, low, high] as [number, number, number, number],
      };
    });
}

/** Labels + values for the 1-minute line chart. */
export function toLineSeries(series: ChangePoint[]): { labels: string[]; values: number[] } {
  return { labels: series.map((p) => p.label), values: series.map((p) => p.value) };
}
