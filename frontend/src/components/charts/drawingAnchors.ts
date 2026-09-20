/**
 * Data-space anchors for `ChartDrawingOverlay`.
 *
 * An anchor converts between a pixel on the overlay and a (time, price) point
 * on the host chart, so a drawing is stored as the level and moment it marks
 * and is re-projected whenever the chart pans, zooms, rescales or reloads.
 * Without one, the overlay keeps its old behaviour: shapes stay at a fixed
 * fraction of the canvas.
 *
 * `time` is in whatever unit the anchor chooses — it is only ever round-tripped
 * through the same anchor. Intraday charts use seconds since IST midnight, so
 * a drawing keeps its clock time across sessions and bucket sizes.
 */
import type { IChartApi, ISeriesApi, Logical, SeriesType } from "lightweight-charts";

export interface DrawingAnchor {
  xToTime(px: number): number | null;
  yToPrice(py: number): number | null;
  timeToX(time: number): number | null;
  priceToY(price: number): number | null;
  /** Cheap fingerprint of the current view; a change means "re-project". */
  viewKey(): string | null;
}

const DAY_S = 86400;

/** Fractional position of `t` in ascending `times`, extrapolated past the ends. */
function indexOfTime(times: number[], t: number): number | null {
  const n = times.length;
  if (n === 0) return null;
  if (n === 1) return t === times[0] ? 0 : null;
  if (t <= times[0]) return (t - times[0]) / (times[1] - times[0] || 1);
  if (t >= times[n - 1]) return n - 1 + (t - times[n - 1]) / (times[n - 1] - times[n - 2] || 1);
  let lo = 0;
  let hi = n - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (times[mid] <= t) lo = mid;
    else hi = mid;
  }
  return lo + (t - times[lo]) / (times[hi] - times[lo] || 1);
}

/** Time at fractional index `i` of ascending `times`, extrapolated past the ends. */
function timeOfIndex(times: number[], i: number): number | null {
  const n = times.length;
  if (n === 0) return null;
  if (n === 1) return times[0];
  if (i <= 0) return times[0] + i * (times[1] - times[0]);
  if (i >= n - 1) return times[n - 1] + (i - (n - 1)) * (times[n - 1] - times[n - 2]);
  const lo = Math.floor(i);
  return times[lo] + (i - lo) * (times[lo + 1] - times[lo]);
}

/**
 * lightweight-charts. `getSeries` must return the series whose bars define the
 * time axis (all series on these charts share one set of bar times).
 */
export function lwcAnchor(
  getChart: () => IChartApi | null,
  getSeries: () => ISeriesApi<SeriesType> | null,
  { timeOfDay }: { timeOfDay: boolean },
): DrawingAnchor {
  const barTimes = (): number[] => {
    const s = getSeries();
    if (!s) return [];
    return s.data().map((d) => d.time as number);
  };
  return {
    xToTime(px) {
      const chart = getChart();
      if (!chart) return null;
      const logical = chart.timeScale().coordinateToLogical(px);
      if (logical == null) return null;
      const t = timeOfIndex(barTimes(), logical as number);
      if (t == null) return null;
      return timeOfDay ? ((t % DAY_S) + DAY_S) % DAY_S : t;
    },
    timeToX(time) {
      const chart = getChart();
      if (!chart) return null;
      const times = barTimes();
      if (times.length === 0) return null;
      const abs = timeOfDay ? Math.floor(times[0] / DAY_S) * DAY_S + time : time;
      const idx = indexOfTime(times, abs);
      if (idx == null) return null;
      const x = chart.timeScale().logicalToCoordinate(idx as Logical);
      return x == null ? null : (x as number);
    },
    yToPrice(py) {
      const p = getSeries()?.coordinateToPrice(py);
      return p == null || !Number.isFinite(p) ? null : (p as number);
    },
    priceToY(price) {
      const y = getSeries()?.priceToCoordinate(price);
      return y == null ? null : (y as number);
    },
    viewKey() {
      const chart = getChart();
      const s = getSeries();
      if (!chart || !s) return null;
      const r = chart.timeScale().getVisibleLogicalRange();
      return `${r?.from}|${r?.to}|${s.priceToCoordinate(0)}|${s.priceToCoordinate(1)}`;
    },
  };
}

/** Minimal slice of the ECharts instance API the anchor needs. */
interface EChartsLike {
  convertToPixel(finder: { gridIndex: number }, value: number[]): number[] | number;
  convertFromPixel(finder: { gridIndex: number }, value: number[]): number[] | number;
}

const hhmmToSec = (label: string): number => {
  const [h, m] = label.split(":").map(Number);
  return h * 3600 + m * 60;
};

/** ECharts with a category x-axis of "HH:MM" labels (the OI change charts). */
export function echartsCategoryAnchor(
  getInstance: () => EChartsLike | null,
  labels: string[],
): DrawingAnchor {
  const times = labels.map(hhmmToSec);
  const px = (ec: EChartsLike, idx: number, value: number): number[] | null => {
    try {
      const r = ec.convertToPixel({ gridIndex: 0 }, [idx, value]);
      return Array.isArray(r) ? r : null;
    } catch {
      return null;
    }
  };
  // Category positions are linear in the index: derive the mapping from 0 and 1.
  const xScale = (ec: EChartsLike): { x0: number; dx: number } | null => {
    if (labels.length < 2) return null;
    const a = px(ec, 0, 0);
    const b = px(ec, 1, 0);
    if (!a || !b || !Number.isFinite(a[0]) || !Number.isFinite(b[0]) || b[0] === a[0]) return null;
    return { x0: a[0], dx: b[0] - a[0] };
  };
  return {
    xToTime(p) {
      const ec = getInstance();
      const sc = ec && xScale(ec);
      if (!sc) return null;
      return timeOfIndex(times, (p - sc.x0) / sc.dx);
    },
    timeToX(time) {
      const ec = getInstance();
      const sc = ec && xScale(ec);
      if (!sc) return null;
      const idx = indexOfTime(times, time);
      return idx == null ? null : sc.x0 + idx * sc.dx;
    },
    yToPrice(py) {
      const ec = getInstance();
      if (!ec) return null;
      try {
        const r = ec.convertFromPixel({ gridIndex: 0 }, [0, py]);
        const v = Array.isArray(r) ? r[1] : null;
        return v != null && Number.isFinite(v) ? v : null;
      } catch {
        return null;
      }
    },
    priceToY(price) {
      const ec = getInstance();
      const r = ec && px(ec, 0, price);
      return r && Number.isFinite(r[1]) ? r[1] : null;
    },
    viewKey() {
      const ec = getInstance();
      const sc = ec && xScale(ec);
      if (!ec || !sc) return null;
      return `${sc.x0}|${sc.dx}|${px(ec, 0, 0)?.[1]}|${px(ec, 0, 1)?.[1]}|${labels.length}`;
    },
  };
}

/** Defers to an anchor that only exists after mount (e.g. behind a component ref). */
export function lazyAnchor(get: () => DrawingAnchor | null | undefined): DrawingAnchor {
  return {
    xToTime: (p) => get()?.xToTime(p) ?? null,
    yToPrice: (p) => get()?.yToPrice(p) ?? null,
    timeToX: (t) => get()?.timeToX(t) ?? null,
    priceToY: (p) => get()?.priceToY(p) ?? null,
    viewKey: () => get()?.viewKey() ?? null,
  };
}
