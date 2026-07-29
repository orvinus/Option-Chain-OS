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
