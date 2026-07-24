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
 */
import { useEffect, useRef } from "react";
import {
  createChart,
  LineStyle,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import { baseChartOptions } from "./chartTheme";

export interface LinePoint {
  time: UTCTimestamp;
  value: number;
}

export interface LineSeriesSpec {
  id: string;
  label: string;
  color: string;
  data: LinePoint[];
  /** Optional value formatter for the price scale + crosshair label. */
  priceFormat?: (v: number) => string;
}

export interface CrosshairInfo {
  time: UTCTimestamp | null;
  values: Record<string, number | null>;
}

interface Props {
  series: LineSeriesSpec[];
  height?: number;
  onCrosshair?: (info: CrosshairInfo) => void;
  /** Refit the visible range to the data on the next update. */
  fitContent?: boolean;
  /** Floating value tooltip that follows the cursor (matches the ECharts panes). Default on. */
  showTooltip?: boolean;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** Format a chart time (real epoch + baked-in IST offset) as an IST wall-clock label. */
function fmtIstAxisTime(t: UTCTimestamp): string {
  const d = new Date((t as number) * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  // The +5:30 offset is baked into the value (see chartTime.ts), so read UTC parts.
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
}

export function TimeSeriesChart({ series, height = 320, onCrosshair, fitContent, showTooltip = true }: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<Map<string, ISeriesApi<"Line">>>(new Map());
  const tooltipRef = useRef<HTMLDivElement | null>(null);
  const onCrosshairRef = useRef(onCrosshair);
  onCrosshairRef.current = onCrosshair;
  // Latest specs (labels/colors/formatters) for the imperative crosshair handler.
  const specRef = useRef(series);
  specRef.current = series;
  const showTooltipRef = useRef(showTooltip);
  showTooltipRef.current = showTooltip;
  const heightRef = useRef(height);
  heightRef.current = height;

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
      const values: Record<string, number | null> = {};
      for (const [id, s] of seriesRef.current.entries()) {
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
      if (!rows) {
        tip.style.display = "none";
        return;
      }
      tip.innerHTML =
        `<div style="margin-bottom:4px;color:#e5e7eb">${fmtIstAxisTime(param.time as UTCTimestamp)}</div>${rows}`;
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

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      seriesRef.current.clear();
    };
    // height is applied on create; a change is rare and handled via applyOptions below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Reconcile series (add/remove/update) whenever the spec changes.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const map = seriesRef.current;
    const wanted = new Set(series.map((s) => s.id));

    // Remove series no longer present.
    for (const [id, s] of Array.from(map.entries())) {
      if (!wanted.has(id)) {
        chart.removeSeries(s);
        map.delete(id);
      }
    }

    // Add / update.
    for (const spec of series) {
      let s = map.get(spec.id);
      if (!s) {
        s = chart.addLineSeries({
          color: spec.color,
          lineWidth: 2,
          lineStyle: LineStyle.Solid,
          priceLineVisible: false,
          lastValueVisible: true,
          ...(spec.priceFormat
            ? { priceFormat: { type: "custom", formatter: spec.priceFormat, minMove: 0.0001 } }
            : {}),
        });
        map.set(spec.id, s);
      } else {
        s.applyOptions({ color: spec.color });
      }
      s.setData(spec.data);
    }

    if (fitContent) chart.timeScale().fitContent();
  }, [series, fitContent]);

  useEffect(() => {
    chartRef.current?.applyOptions({ height });
  }, [height]);

  return (
    <div ref={containerRef} style={{ width: "100%", height, position: "relative" }}>
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
}
