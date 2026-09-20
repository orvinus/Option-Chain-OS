/** Single source of chart colors + base options for lightweight-charts panes.
 *
 * Reuses the exact palette the existing ECharts panes use (`OICandleChart`:
 * call red / put blue) so the new time-series panes agree visually with the
 * rest of the app.
 */
import { ColorType, CrosshairMode, type DeepPartial, type ChartOptions } from "lightweight-charts";

export const SERIES_COLORS = {
  call: "#f75c5c", // calls — red (matches OICandleChart)
  put: "#4da6ff", // puts — blue
  ratio: "#fbbf24", // amber
  pcr: "#22c55e", // green
  spot: "#e5e7eb", // near-white
} as const;

/** Base dark-theme options for a lightweight-charts pane (intraday IST axis). */
export function baseChartOptions(): DeepPartial<ChartOptions> {
  return {
    layout: {
      background: { type: ColorType.Solid, color: "transparent" },
      textColor: "#9ca3af",
      fontFamily: "Inter, system-ui, sans-serif",
    },
    grid: {
      vertLines: { color: "rgba(45,55,72,0.35)" },
      horzLines: { color: "rgba(45,55,72,0.35)" },
    },
    crosshair: {
      mode: CrosshairMode.Normal,
      vertLine: { color: "#3b82f6", width: 1, style: 3, labelBackgroundColor: "#1f2937" },
      horzLine: { color: "#3b82f6", width: 1, style: 3, labelBackgroundColor: "#1f2937" },
    },
    rightPriceScale: { borderColor: "rgba(45,55,72,0.8)" },
    timeScale: {
      borderColor: "rgba(45,55,72,0.8)",
      timeVisible: true,
      secondsVisible: false,
    },
    handleScroll: true,
    handleScale: true,
    autoSize: true,
  };
}

// ── Shared ECharts chrome ─────────────────────────────────────────────────
// Values copied VERBATIM from `OIChangeChart` / `OICandleChart` so any panel
// built on them renders in the dashboard's exact visual language. Typed
// loosely (`as const`) so callers spread them into ECharts option objects
// without fighting the library's union types.

/** Sensibull colour convention: CE = red, PE = blue; ATM brighter; negatives muted. */
export const OI_BAR_COLORS = {
  CE: "#f75c5c",
  CE_ATM: "#ff8c00",
  CE_NEG: "#7f3a3a",
  PE: "#4da6ff",
  PE_ATM: "#00d4b4",
  PE_NEG: "#2a4a7f",
} as const;

/** Call = red theme, Put = blue theme. Up candle = bright fill, down = muted. */
export const OI_CANDLE_THEME: Record<
  "call" | "put",
  { up: string; down: string; upBorder: string; downBorder: string; line: string }
> = {
  call: { up: "#f75c5c", down: "#7f3a3a", upBorder: "#ff8c8c", downBorder: "#a85555", line: "#f75c5c" },
  put: { up: "#4da6ff", down: "#2a4a7f", upBorder: "#7fc1ff", downBorder: "#3a6aaf", line: "#4da6ff" },
};

export const ECHARTS_TOOLTIP_BOX = {
  backgroundColor: "rgba(10,15,30,0.97)",
  borderColor: "#1e3a5f",
  borderWidth: 1,
  textStyle: { color: "#e5e7eb", fontSize: 12 },
} as const;

export const ECHARTS_CATEGORY_XAXIS = {
  axisLine: { lineStyle: { color: "#2d3748" } },
  axisTick: { show: false },
  axisLabel: { color: "#6b7280", fontSize: 10 },
  splitLine: { show: false },
} as const;

/** Value-axis chrome; the caller supplies `axisLabel.formatter` (e.g. `compact`). */
export const ECHARTS_VALUE_YAXIS = {
  splitLine: { lineStyle: { color: "rgba(45,55,72,0.6)" } },
  axisLabel: { color: "#6b7280", fontSize: 11 },
} as const;

export const ECHARTS_LEGEND_BOTTOM = {
  bottom: 0,
  textStyle: { color: "#9ca3af", fontSize: 12 },
  itemHeight: 10,
  itemWidth: 16,
  itemGap: 24,
} as const;

/** Mouse-wheel zoom + drag-pan along the x axis (no extra UI chrome). */
export const ECHARTS_DATAZOOM_INSIDE = [{ type: "inside" as const, xAxisIndex: 0, filterMode: "none" as const }];

/** Dashed zero line so the "0 ±" axis reads clearly (OICandleChart). */
export const ECHARTS_ZERO_MARKLINE = {
  symbol: "none" as const,
  silent: true,
  lineStyle: { color: "rgba(255,255,255,0.35)", type: "dashed" as const, width: 1 },
  label: { show: false },
  data: [{ yAxis: 0 }],
};

/**
 * The OIChangeChart ATM mark-line style — dashed white line with a boxed
 * label — for any level line that should read like the dashboard's ATM marker.
 */
export function atmStyleMarkLine<T>(data: T[], formatter: string | ((p: unknown) => string)) {
  return {
    symbol: "none" as const,
    silent: true,
    lineStyle: { color: "rgba(255,255,255,0.85)", type: "dashed" as const, width: 1.5 },
    label: {
      show: true,
      position: "insideStartTop" as const,
      formatter,
      color: "#ffffff",
      backgroundColor: "rgba(15,23,42,0.92)",
      borderColor: "#3b82f6",
      borderWidth: 1,
      padding: [4, 6] as [number, number],
      borderRadius: 6,
      fontSize: 11,
    },
    data,
  };
}

/** One tooltip row in the OIChangeChart markup (8×8 swatch, grey label, white bold value). */
export function tooltipRowHtml(color: string, label: string, valueHtml: string): string {
  return (
    `<div style="display:flex;align-items:center;gap:6px;margin-top:3px">` +
    `<span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${color}"></span>` +
    `<span style="color:#9ca3af">${label}:</span> <b style="color:#fff">${valueHtml}</b>` +
    `</div>`
  );
}

export const OI_CHART_HEIGHT = 500;
export const OI_CHART_GRID = { left: 16, right: 16, top: 20, bottom: 60, containLabel: true } as const;
