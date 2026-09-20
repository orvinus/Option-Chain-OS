/** Reusable lightweight-charts wrapper for time-series panes (OI / ratio / PCR).
 *
 * Imperative lifecycle: the chart + series are created ONCE and disposed on
 * unmount — never remounted on data change. Data updates diff by series id and
 * call `setData` (full swap) which lightweight-charts handles efficiently; live
 * appends should pass the whole array (the lib no-ops unchanged points). A
 * `ResizeObserver` keeps it fluid; crosshair moves are surfaced to `onCrosshair`
 * so the host page can render a linked legend readout (the OHLC/value line the
 * old ECharts panes lacked), and a built-in floating tooltip follows the cursor
 * with the per-series value at the hovered x — matching the ECharts axis tooltip
 * on the Charts page.
 *
 * Additive extensions (all optional; defaults keep the Ratio / Replay pages
 * byte-identical): per-series line width/style, area series, markers, price
 * lines and tooltip hiding; chart-level `fitKey` (refit only when it changes so
 * a user's zoom survives polling), `boxes` painted on a canvas overlay from
 * price/time coordinates every frame (the UmpTvChart zone painter generalised),
 * `children` rendered inside the relative container (hosts a drawing overlay)
 * and `describeAt` / `anchor` handles for `ChartDrawingOverlay`.
 */
import { forwardRef, useEffect, useImperativeHandle, useRef, type ReactNode } from "react";
import {
  createChart,
  LineStyle,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type SeriesMarker,
  type UTCTimestamp,
} from "lightweight-charts";
import { baseChartOptions } from "./chartTheme";
import { lwcAnchor, type DrawingAnchor } from "./drawingAnchors";

export interface LinePoint {
  time: UTCTimestamp;
  value: number;
}

export interface SeriesPriceLine {
  id: string;
  price: number;
  color: string;
  title?: string;
  lineStyle?: LineStyle;
  lineWidth?: 1 | 2 | 3 | 4;
}

export interface LineSeriesSpec {
  id: string;
  label: string;
  color: string;
  data: LinePoint[];
  /** Optional value formatter for the price scale + crosshair label. */
  priceFormat?: (v: number) => string;
  /** Default 2. */
  lineWidth?: 1 | 2 | 3 | 4;
  /** Default Solid. */
  lineStyle?: LineStyle;
  /** "area" fills under the line with a translucent tint of `color`. Default "line". */
  kind?: "line" | "area";
  /** Markers must sit on a time present in `data` (the lib drops the rest). */
  markers?: SeriesMarker<UTCTimestamp>[];
  priceLines?: SeriesPriceLine[];
  /** Skip this series in the floating tooltip / crosshair values. */
  hideInTooltip?: boolean;
  /** Default true. */
  lastValueVisible?: boolean;
}

export interface CrosshairInfo {
  time: UTCTimestamp | null;
  values: Record<string, number | null>;
}

/** A translucent rectangle in (time, price) space, clipped to the pane. */
export interface ChartBox {
  /** Series whose price scale positions the box vertically. */
  seriesId: string;
  from: UTCTimestamp;
  /** Undefined = extend to the right edge of the pane. */
  to?: UTCTimestamp;
  top: number;
  bottom: number;
  fill: string;
  stroke?: string;
}

export interface TimeSeriesChartHandle {
  /** Pixel (relative to the pane) → readable value + IST HH:MM (ChartDrawingOverlay contract). */
  describeAt(px: number, py: number): { value: string; time: string | null } | null;
  /** Fit all data again on both axes (undoes any drag / zoom). */
  resetView(): void;
  /** Pixel <-> (IST time of day, price) for ChartDrawingOverlay. */
  anchor: DrawingAnchor;
}

/** A drag on the price axis switches the library's price autoscale off, and nothing turns it back on — later data can then
 *  sit entirely outside the frozen range and the chart looks blank. Every
 *  refit therefore restores autoscale as well as the time range. */
function fitBothAxes(chart: IChartApi): void {
  chart.priceScale("right").applyOptions({ autoScale: true });
  chart.timeScale().fitContent();
}

interface Props {
  series: LineSeriesSpec[];
  height?: number;
  onCrosshair?: (info: CrosshairInfo) => void;
  /** Refit the visible range to the data on the next update. */
  fitContent?: boolean;
  /** Refit ONLY when this key changes (a poll refresh keeps the user's zoom). */
  fitKey?: string;
  /** Floating value tooltip that follows the cursor (matches the ECharts panes). Default on. */
  showTooltip?: boolean;
  /**
   * Extra rows appended to the floating tooltip below the per-series rows, keyed
   * off the hovered chart time (epoch seconds, +IST offset baked in — same value
   * as a series point's `time`). Used to show underlying positions alongside ratios.
   */
  extraTooltipRows?: (timeSec: number) => Array<{ label: string; value: string; color?: string }>;
  boxes?: ChartBox[];
  /** Rendered inside the relative container (e.g. a `ChartDrawingOverlay`). */
  children?: ReactNode;
}

type AnySeries = ISeriesApi<"Line"> | ISeriesApi<"Area">;

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** Format a chart time (real epoch + baked-in IST offset) as an IST wall-clock label. */
function fmtIstAxisTime(t: UTCTimestamp): string {
  const d = new Date((t as number) * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  // The +5:30 offset is baked into the value (see chartTime.ts), so read UTC parts.
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
}

function fmtIstHhmm(t: number): string {
  const d = new Date(t * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
}

export const TimeSeriesChart = forwardRef<TimeSeriesChartHandle, Props>(function TimeSeriesChart(
  { series, height = 320, onCrosshair, fitContent, fitKey, showTooltip = true, extraTooltipRows, boxes, children },
  ref,
) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<Map<string, AnySeries>>(new Map());
  const kindRef = useRef<Map<string, "line" | "area">>(new Map());
  const priceLinesRef = useRef<Map<string, IPriceLine[]>>(new Map());
  const boxCanvasRef = useRef<HTMLCanvasElement | null>(null);
  const boxesRef = useRef<ChartBox[] | undefined>(boxes);
  boxesRef.current = boxes;
  const lastFitKeyRef = useRef<string | undefined>(undefined);
  const tooltipRef = useRef<HTMLDivElement | null>(null);
  const onCrosshairRef = useRef(onCrosshair);
  onCrosshairRef.current = onCrosshair;
  // Latest specs (labels/colors/formatters) for the imperative crosshair handler.
  const specRef = useRef(series);
  specRef.current = series;
  const showTooltipRef = useRef(showTooltip);
  showTooltipRef.current = showTooltip;
  const extraRowsRef = useRef(extraTooltipRows);
  extraRowsRef.current = extraTooltipRows;
  const heightRef = useRef(height);
  heightRef.current = height;

  // Shared-axis bar times come from the first tooltip-visible series (all
  // series on these charts are sampled on the same timestamps).
  const anchorRef = useRef<DrawingAnchor | null>(null);
  if (!anchorRef.current) {
    anchorRef.current = lwcAnchor(
      () => chartRef.current,
      () => {
        const first = specRef.current.find((s) => !s.hideInTooltip) ?? specRef.current[0];
        return (first && seriesRef.current.get(first.id)) || null;
      },
      { timeOfDay: true },
    );
  }

  useImperativeHandle(
    ref,
    () => ({
      anchor: anchorRef.current!,
      describeAt(px, py) {
        const chart = chartRef.current;
        if (!chart) return null;
        const first = specRef.current.find((s) => !s.hideInTooltip) ?? specRef.current[0];
        const s = first ? seriesRef.current.get(first.id) : undefined;
        if (!s) return null;
        const price = s.coordinateToPrice(py);
        if (price == null || !Number.isFinite(price)) return null;
        const t = chart.timeScale().coordinateToTime(px);
        const value = first?.priceFormat ? first.priceFormat(price as number) : (price as number).toFixed(2);
        return { value, time: typeof t === "number" ? fmtIstHhmm(t) : null };
      },
      resetView() {
        if (chartRef.current) fitBothAxes(chartRef.current);
      },
    }),
    [],
  );

  // Create the chart once.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const chart = createChart(el, { ...baseChartOptions(), height });
    chartRef.current = chart;

    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      if (w) chart.applyOptions({ width: Math.floor(w) });
    });
    ro.observe(el);

    chart.subscribeCrosshairMove((param) => {
      const cb = onCrosshairRef.current;
      const tip = tooltipRef.current;
      // Off-chart / no data under the cursor: clear the linked readout + hide tooltip.
      if (param.time == null || !param.point) {
        cb?.({ time: null, values: {} });
        if (tip) tip.style.display = "none";
        return;
      }
      const hidden = new Set(specRef.current.filter((s) => s.hideInTooltip).map((s) => s.id));
      const values: Record<string, number | null> = {};
      for (const [id, s] of seriesRef.current.entries()) {
        if (hidden.has(id)) continue;
        const point = param.seriesData.get(s) as { value?: number } | undefined;
        values[id] = point?.value ?? null;
      }
      cb?.({ time: param.time as UTCTimestamp, values });

      if (!tip || !showTooltipRef.current) {
        if (tip) tip.style.display = "none";
        return;
      }
      // Floating tooltip: time header + one colored row per series with a value.
      const rows = specRef.current
        .map((spec) => {
          if (spec.hideInTooltip) return "";
          const v = values[spec.id];
          if (v == null) return "";
          const val = spec.priceFormat ? spec.priceFormat(v) : v.toFixed(2);
          return (
            `<div style="display:flex;justify-content:space-between;gap:16px;align-items:center">` +
            `<span style="display:flex;align-items:center;gap:6px;color:#9ca3af">` +
            `<span style="width:8px;height:8px;border-radius:2px;background:${spec.color};display:inline-block"></span>${spec.label}` +
            `</span><b style="color:${spec.color}">${val}</b></div>`
          );
        })
        .join("");
      // Optional caller-supplied rows (e.g. underlying Call/Put positions).
      const extra = extraRowsRef.current?.(param.time as UTCTimestamp) ?? [];
      const extraRows = extra
        .map(
          (r) =>
            `<div style="display:flex;justify-content:space-between;gap:16px;align-items:center">` +
            `<span style="color:#9ca3af">${r.label}</span>` +
            `<b style="color:${r.color ?? "#e5e7eb"}">${r.value}</b></div>`,
        )
        .join("");
      const sep = rows && extraRows ? `<div style="height:4px"></div>` : "";
      if (!rows && !extraRows) {
        tip.style.display = "none";
        return;
      }
      tip.innerHTML =
        `<div style="margin-bottom:4px;color:#e5e7eb">${fmtIstAxisTime(param.time as UTCTimestamp)}</div>${rows}${sep}${extraRows}`;
      tip.style.display = "block";
      // Position near the cursor, flipping/clamping to stay inside the pane.
      const pad = 12;
      const cw = el.clientWidth;
      const h = heightRef.current;
      const tw = tip.offsetWidth;
      const th = tip.offsetHeight;
      let left = param.point.x + pad;
      if (left + tw + pad > cw) left = param.point.x - tw - pad;
      left = Math.max(4, left);
      let top = param.point.y + pad;
      if (top + th + pad > h) top = param.point.y - th - pad;
      top = Math.max(4, top);
      tip.style.left = `${left}px`;
      tip.style.top = `${top}px`;
    });

    // Box painter: translucent (time, price) rectangles repainted every frame
    // from price→pixel coordinates so pan/zoom/autoscale stay glued. Idle when
    // no boxes are requested (the canvas is simply cleared).
    let raf = 0;
    const paint = () => {
      raf = requestAnimationFrame(paint);
      const canvas = boxCanvasRef.current;
      if (!canvas) return;
      const list = boxesRef.current;
      const w = el.clientWidth;
      const hpx = el.clientHeight;
      if (canvas.width !== w || canvas.height !== hpx) {
        canvas.width = w;
        canvas.height = hpx;
      }
      const ctx = canvas.getContext("2d");
      if (!ctx) return;
      ctx.clearRect(0, 0, w, hpx);
      if (!list || list.length === 0) return;
      const ts = chart.timeScale();
      const rightEdge = w - (chart.priceScale("right").width() || 0);
      for (const b of list) {
        const s = seriesRef.current.get(b.seriesId);
        if (!s) continue;
        const yTop = s.priceToCoordinate(b.top);
        const yBot = s.priceToCoordinate(b.bottom);
        const x0 = ts.timeToCoordinate(b.from);
        if (yTop == null || yBot == null || x0 == null) continue;
        const x1 = b.to != null ? ts.timeToCoordinate(b.to) : null;
        const xEnd = x1 != null ? (x1 as number) : rightEdge;
        const y = Math.min(yTop, yBot);
        const bh = Math.abs(yBot - yTop);
        const x = Math.min(x0 as number, xEnd);
        const bw = Math.abs(xEnd - (x0 as number));
        if (y > hpx || y + bh < 0 || x > w || x + bw < 0) continue;
        ctx.fillStyle = b.fill;
        ctx.fillRect(x, y, bw, bh);
        if (b.stroke) {
          ctx.strokeStyle = b.stroke;
          ctx.lineWidth = 1;
          ctx.strokeRect(x + 0.5, y + 0.5, Math.max(bw - 1, 0), Math.max(bh - 1, 0));
        }
      }
    };
    raf = requestAnimationFrame(paint);

    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      seriesRef.current.clear();
      kindRef.current.clear();
      priceLinesRef.current.clear();
    };
    // height is applied on create; a change is rare and handled via applyOptions below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Reconcile series (add/remove/update) whenever the spec changes.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const map = seriesRef.current;
    const kinds = kindRef.current;
    const wanted = new Set(series.map((s) => s.id));

    // Remove series no longer present (or whose type changed).
    for (const [id, s] of Array.from(map.entries())) {
      const spec = series.find((x) => x.id === id);
      const kind = spec?.kind ?? "line";
      if (!wanted.has(id) || kinds.get(id) !== kind) {
        chart.removeSeries(s);
        map.delete(id);
        kinds.delete(id);
        priceLinesRef.current.delete(id);
      }
    }

    // Add / update.
    for (const spec of series) {
      const kind = spec.kind ?? "line";
      let s = map.get(spec.id);
      if (!s) {
        const common = {
          lineWidth: spec.lineWidth ?? 2,
          lineStyle: spec.lineStyle ?? LineStyle.Solid,
          priceLineVisible: false,
          lastValueVisible: spec.lastValueVisible ?? true,
          ...(spec.priceFormat
            ? { priceFormat: { type: "custom" as const, formatter: spec.priceFormat, minMove: 0.0001 } }
            : {}),
        };
        s =
          kind === "area"
            ? chart.addAreaSeries({
                ...common,
                lineColor: spec.color,
                topColor: `${spec.color}26`,
                bottomColor: "transparent",
              })
            : chart.addLineSeries({ ...common, color: spec.color });
        map.set(spec.id, s);
        kinds.set(spec.id, kind);
      } else if (kind === "area") {
        (s as ISeriesApi<"Area">).applyOptions({
          lineColor: spec.color,
          topColor: `${spec.color}26`,
          lineWidth: spec.lineWidth ?? 2,
          lineStyle: spec.lineStyle ?? LineStyle.Solid,
          lastValueVisible: spec.lastValueVisible ?? true,
        });
      } else {
        (s as ISeriesApi<"Line">).applyOptions({
          color: spec.color,
          lineWidth: spec.lineWidth ?? 2,
          lineStyle: spec.lineStyle ?? LineStyle.Solid,
          lastValueVisible: spec.lastValueVisible ?? true,
        });
      }
      s.setData(spec.data);

      // Markers (sorted; the lib requires ascending times).
      const markers = [...(spec.markers ?? [])].sort((a, b) => (a.time as number) - (b.time as number));
      s.setMarkers(markers);

      // Price lines: remove all, re-create (same pattern as UmpTvChart).
      for (const pl of priceLinesRef.current.get(spec.id) ?? []) s.removePriceLine(pl);
      const created: IPriceLine[] = [];
      for (const pl of spec.priceLines ?? []) {
        if (!Number.isFinite(pl.price)) continue;
        created.push(
          s.createPriceLine({
            price: pl.price,
            color: pl.color,
            title: pl.title ?? "",
            lineStyle: pl.lineStyle ?? LineStyle.Dotted,
            lineWidth: pl.lineWidth ?? 1,
            axisLabelVisible: true,
          }),
        );
      }
      priceLinesRef.current.set(spec.id, created);
    }

    if (fitContent) chart.timeScale().fitContent();
    if (fitKey !== undefined && fitKey !== lastFitKeyRef.current) {
      lastFitKeyRef.current = fitKey;
      fitBothAxes(chart);
    }
  }, [series, fitContent, fitKey]);

  useEffect(() => {
    chartRef.current?.applyOptions({ height });
  }, [height]);

  return (
    <div ref={containerRef} style={{ width: "100%", height, position: "relative" }}>
      <canvas
        ref={boxCanvasRef}
        style={{ position: "absolute", inset: 0, zIndex: 5, pointerEvents: "none" }}
      />
      {children}
      <div
        ref={tooltipRef}
        style={{
          display: "none",
          position: "absolute",
          zIndex: 20,
          pointerEvents: "none",
          background: "rgba(10,15,30,0.97)",
          border: "1px solid #1e3a5f",
          borderRadius: 6,
          padding: "6px 9px",
          fontSize: 12,
          lineHeight: 1.5,
          whiteSpace: "nowrap",
          boxShadow: "0 4px 14px rgba(0,0,0,0.4)",
          color: "#e5e7eb",
        }}
      />
    </div>
  );
});
