import ReactECharts from "echarts-for-react";
import type { EChartsOption } from "echarts";
import { useCallback, useMemo, useRef, useState } from "react";
import { ChartDrawingOverlay } from "./ChartDrawingOverlay";
import type { OITimeseriesPoint } from "../types";
import {
  baselineLabel,
  bucketToCandles,
  toChangeSinceOpen,
  toLineSeries,
  type OISide,
} from "../utils/oiCandles";
import { compact as compactNum } from "../utils/num";

interface Props {
  points: OITimeseriesPoint[] | null;
  side: OISide;
  /** Bar width in minutes; 1 renders a line. Widened from `ChartInterval` (1|5|10|15|30)
   *  so the Replay page can drive it from the shared 10 timeframe presets (3/60/120/180)
   *  — the value is only compared to 1 and handed to `bucketToCandles`, which is generic
   *  over minutes. `ChartIntervalBar` and the Charts tab still use `ChartInterval`. */
  interval: number;
  title: string;
  isLoading?: boolean;
  /** Real-time total OI (absolute, summed across the selected strikes) for this side. */
  liveTotal?: number | null;
  /** True when the live WS push is connected — drives the "live" pulse on the badge. */
  liveOn?: boolean;
}

// Call = red theme, Put = blue theme. Up candle (close ≥ open) = bright fill,
// down candle = muted fill — same palette family as OIChangeChart.
const THEME: Record<OISide, { up: string; down: string; upBorder: string; downBorder: string; line: string }> = {
  call: { up: "#f75c5c", down: "#7f3a3a", upBorder: "#ff8c8c", downBorder: "#a85555", line: "#f75c5c" },
  put: { up: "#4da6ff", down: "#2a4a7f", upBorder: "#7fc1ff", downBorder: "#3a6aaf", line: "#4da6ff" },
};

export function OICandleChart({ points, side, interval, title, isLoading = false, liveTotal, liveOn = false }: Props) {
  const theme = THEME[side];
  const isLine = interval === 1;
  const chartRef = useRef<ReactECharts>(null);
  const totalLabel = side === "call" ? "Total Call OI" : "Total Put OI";

  const series = useMemo(() => toChangeSinceOpen(points ?? [], side), [points, side]);
  // "open" when data starts at 09:15; the actual first-bucket time otherwise
  // (e.g. backend booted mid-session) so the axis never misstates the baseline.
  const sinceLabel = useMemo(() => baselineLabel(points), [points]);

  // X-axis labels (HH:MM) for mapping a pointer's category index back to a time.
  const xLabels = useMemo(
    () => (isLine ? toLineSeries(series).labels : bucketToCandles(series, interval).map((c) => c.label)),
    [series, isLine, interval],
  );

  // Pixel → data readout used by the drawing overlay to show the value/level being
  // drawn. Uses ECharts' grid coordinate conversion (Y = OI Δ, X = time bucket).
  const describeAt = useCallback(
    (px: number, py: number): { value: string; time: string | null } | null => {
      const ec = chartRef.current?.getEchartsInstance();
      if (!ec) return null;
      let coord: number[] | null = null;
      try {
        const r = ec.convertFromPixel({ gridIndex: 0 }, [px, py]) as number[] | number;
        coord = Array.isArray(r) ? r : null;
      } catch {
        return null;
      }
      if (!coord || coord.length < 2 || !Number.isFinite(coord[1])) return null;
      const xi = Math.round(coord[0]);
      const time = xi >= 0 && xi < xLabels.length ? xLabels[xi] : null;
      return { value: compactNum(coord[1]), time };
    },
    [xLabels],
  );

  const option: EChartsOption = useMemo(() => {
    // Keep the zero line on-chart so the "0 ±" axis reads clearly.
    const zeroMarkLine = {
      symbol: "none" as const,
      silent: true,
      lineStyle: { color: "rgba(255,255,255,0.35)", type: "dashed" as const, width: 1 },
      label: { show: false },
      data: [{ yAxis: 0 }],
    };

    const baseAxis = {
      yAxis: {
        type: "value" as const,
        name: `OI Δ since ${sinceLabel}`,
        nameTextStyle: { color: "#6b7280", fontSize: 10 },
        // Force 0 into the visible range regardless of data sign.
        min: (v: { min: number }) => Math.min(0, v.min),
        max: (v: { max: number }) => Math.max(0, v.max),
        splitLine: { lineStyle: { color: "rgba(45,55,72,0.6)" } },
        axisLabel: { color: "#6b7280", fontSize: 11, formatter: (val: number) => compactNum(val) },
      },
      grid: { left: 16, right: 16, top: 28, bottom: 48, containLabel: true },
      backgroundColor: "transparent",
      animation: true,
      animationDuration: 200,
      // Mouse-wheel zoom + drag-pan along the time axis.
      dataZoom: [{ type: "inside" as const, xAxisIndex: 0, filterMode: "none" as const }],
    };

    if (isLine) {
      const { labels, values } = toLineSeries(series);
      return {
        ...baseAxis,
        tooltip: {
          trigger: "axis",
          backgroundColor: "rgba(10,15,30,0.97)",
          borderColor: "#1e3a5f",
          borderWidth: 1,
          textStyle: { color: "#e5e7eb", fontSize: 12 },
          formatter: (params: unknown) => {
            const arr = params as Array<{ axisValue: string; data: number }>;
            if (!arr?.length) return "";
            return `<div style="font-size:12px"><div style="margin-bottom:2px">${arr[0].axisValue}</div>` +
              `<b style="color:#fff">${compactNum(arr[0].data ?? 0)}</b></div>`;
          },
        },
        xAxis: {
          type: "category",
          data: labels,
          axisLine: { lineStyle: { color: "#2d3748" } },
          axisTick: { show: false },
          axisLabel: { color: "#6b7280", fontSize: 10, interval: Math.ceil(labels.length / 12) },
        },
        series: [
          {
            name: title,
            type: "line",
            data: values,
            showSymbol: false,
            smooth: false,
            lineStyle: { color: theme.line, width: 1.5 },
            areaStyle: { color: theme.line, opacity: 0.08 },
            markLine: zeroMarkLine,
          },
        ],
      } satisfies EChartsOption;
    }

    const candles = bucketToCandles(series, interval);
    return {
      ...baseAxis,
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross" },
        backgroundColor: "rgba(10,15,30,0.97)",
        borderColor: "#1e3a5f",
        borderWidth: 1,
        textStyle: { color: "#e5e7eb", fontSize: 12 },
        formatter: (params: unknown) => {
          const arr = params as Array<{ axisValue: string; data: number[] }>;
          if (!arr?.length) return "";
          // Candlestick data arrives as [idx, open, close, low, high].
          const d = arr[0].data ?? [];
          const [, open, close, low, high] = d;
          const row = (k: string, val: number) =>
            `<div style="display:flex;justify-content:space-between;gap:14px"><span style="color:#9ca3af">${k}</span><b style="color:#fff">${compactNum(val)}</b></div>`;
          return `<div style="font-size:12px;min-width:150px"><div style="margin-bottom:3px">${arr[0].axisValue}</div>` +
            row("Open", open) + row("High", high) + row("Low", low) + row("Close", close) + `</div>`;
        },
      },
      xAxis: {
        type: "category",
        data: candles.map((c) => c.label),
        axisLine: { lineStyle: { color: "#2d3748" } },
        axisTick: { show: false },
        axisLabel: { color: "#6b7280", fontSize: 10, interval: Math.ceil(candles.length / 12) },
      },
      series: [
        {
          name: title,
          type: "candlestick",
          data: candles.map((c) => c.ohlc),
          itemStyle: {
            color: theme.up,
            color0: theme.down,
            borderColor: theme.upBorder,
            borderColor0: theme.downBorder,
          },
          markLine: zeroMarkLine,
        },
      ],
    } satisfies EChartsOption;
  }, [series, interval, isLine, title, theme, sinceLabel]);

  const hasData = (points?.length ?? 0) > 0;
  // Slot beside the heading that hosts the drawing toolbar (portaled from the
  // overlay) so it no longer floats over the chart data.
  const [toolbarSlot, setToolbarSlot] = useState<HTMLDivElement | null>(null);

  return (
    <div className="panel p-4 relative">
      <div className="flex items-center gap-2 mb-2">
        <span className="inline-block w-2.5 h-2.5 rounded-sm" style={{ background: theme.line }} />
        <h3 className="text-sm font-semibold text-foreground">{title}</h3>
        <span className="text-[11px] text-muted">{isLine ? "1m line" : `${interval}m candles`}</span>
        {sinceLabel !== "open" && (
          <span
            className="text-[10px] text-amber-300/90"
            title="No stored data between 09:15 and this time — the 0-baseline is the first stored bucket, not the session open."
          >
            Δ from {sinceLabel} (data start)
          </span>
        )}
        <div ref={setToolbarSlot} className="ml-auto flex items-center" />
      </div>

      {isLoading && (
        <div className="absolute inset-0 flex items-center justify-center z-10 bg-black/30 rounded-lg">
          <div className="flex items-center gap-2 bg-surface/90 px-4 py-2 rounded-full border border-border text-sm text-foreground">
            <svg className="w-4 h-4 animate-spin text-accent" viewBox="0 0 24 24" fill="none">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4l3-3-3-3v4a8 8 0 00-8 8h4z" />
            </svg>
            Loading…
          </div>
        </div>
      )}

      {!hasData && !isLoading ? (
        <div className="flex items-center justify-center text-muted text-sm" style={{ height: 420 }}>
          No OI time-series yet for this window.
        </div>
      ) : (
        <div className="relative" style={{ height: 420 }}>
          <ReactECharts
            ref={chartRef}
            option={option}
            notMerge
            lazyUpdate={false}
            style={{ height: 420, width: "100%" }}
            opts={{ renderer: "canvas" }}
          />
          <ChartDrawingOverlay describeAt={describeAt} persistKey={`candle-${side}`} toolbarContainer={toolbarSlot} />

          {liveTotal != null && (
            <div
              className="absolute bottom-2 right-2 z-20 flex flex-col items-end rounded-md border border-border bg-slate-900/85 backdrop-blur-sm px-2.5 py-1 shadow-lg"
              style={{ pointerEvents: "none" }}
            >
              <div className="flex items-center gap-1.5">
                <span
                  className={`w-1.5 h-1.5 rounded-full ${liveOn ? "animate-pulse" : "opacity-50"}`}
                  style={{ background: theme.line }}
                />
                <span className="text-[10px] uppercase tracking-wider text-muted">{totalLabel}</span>
              </div>
              <span className="font-mono text-base font-bold tabular-nums" style={{ color: theme.line }}>
                {compactNum(liveTotal)}
              </span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
