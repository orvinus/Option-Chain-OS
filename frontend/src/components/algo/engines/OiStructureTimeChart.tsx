/**
 * The OI Structure ("OI Change") panel's time-axis chart, drawn in the
 * dashboard OI Change tab's visual language: ECharts, CE red / PE blue
 * palette, legend at the bottom `[PE OI Chg, CE OI Chg]`, the shared tooltip
 * box, zero line, y forced through 0, compact Cr labels, wheel zoom + drag
 * pan, 500px panel. `ChartIntervalBar` drives it: 1 Min = two lines with a
 * faint area (the Charts tab's line look), 5/10/15/30 = grouped CE/PE
 * candles (`bucketToCandles`, session-anchored) — drawn with a `custom`
 * series because ECharts' candlestick series never lays out side by side
 * (every series is centred on the category), so PE sits left and CE right of
 * each bucket exactly like the dashboard's grouped bars.
 *
 * Engine overlays on top: range-zone bands with ATM-style label lines for
 * hi/lo (+ the breakout level), swing labels (HH/HL/LH/LL/EQH/EQL) and, in
 * the 5-minute mode, the RED-5m pin on the last candle of a side whose
 * red-candle rule fired. Calculations stay the engine's — nothing here
 * recomputes a signal.
 */
import ReactECharts from "echarts-for-react";
import type { EChartsOption, SeriesOption } from "echarts";
import { useCallback, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { EngineSwing, OiStructureEvalResponse } from "../../../types/algo";
import { ChartDrawingOverlay } from "../../ChartDrawingOverlay";
import { echartsCategoryAnchor } from "../../charts/drawingAnchors";
import {
  atmStyleMarkLine,
  ECHARTS_CATEGORY_XAXIS,
  ECHARTS_DATAZOOM_INSIDE,
  ECHARTS_LEGEND_BOTTOM,
  ECHARTS_TOOLTIP_BOX,
  ECHARTS_VALUE_YAXIS,
  ECHARTS_ZERO_MARKLINE,
  OI_BAR_COLORS,
  OI_CANDLE_THEME,
  OI_CHART_GRID,
  OI_CHART_HEIGHT,
  tooltipRowHtml,
} from "../../charts/chartTheme";
import { bucketStartIso, fmtCr, toChangePointsCr } from "../../../utils/chartBuckets";
import {
  bucketToCandles,
  labelToMin,
  SESSION_OPEN_MIN,
  toLineSeries,
  type Candle,
  type ChartInterval,
} from "../../../utils/oiCandles";

interface Props {
  data: OiStructureEvalResponse;
  /** ONE side per chart (2026-09-13 quad layout, per docs/reference/oi_smc_engine.html). */
  side: SideKey;
  interval: ChartInterval;
  /** Drawing persistence scope — MUST be unique per chart, or all four share
   *  one drawing store and annotations bleed between them. */
  persistKey: string;
  engine5mOn: boolean;
  /** Structure overlay: swing labels + range zone + breakout line. Only the
   *  1-Minute charts expose this, matching the reference. */
  showStructure?: boolean;
  height?: number;
  /** Rendered in the header, LEFT of the drawing toolbar. Must be a laid-out
   *  slot rather than an absolutely-positioned sibling: the toolbar already
   *  owns the top-right corner, so anything floated there lands on top of it. */
  toggle?: ReactNode;
}

type SideKey = "call" | "put";
const PE_NAME = "PE OI Chg";
const CE_NAME = "CE OI Chg";
const SIDE_NAME: Record<SideKey, string> = { call: CE_NAME, put: PE_NAME };
const SIDE_COLOR: Record<SideKey, string> = { call: OI_BAR_COLORS.CE, put: OI_BAR_COLORS.PE };
// Palette-consistent zone tints (call red / put blue).
const SIDE_TINT: Record<SideKey, { fill: string; border: string }> = {
  call: { fill: "rgba(247,92,92,0.08)", border: "rgba(247,92,92,0.3)" },
  put: { fill: "rgba(77,166,255,0.08)", border: "rgba(77,166,255,0.3)" },
};
// One side per chart (2026-09-13): the candle is CENTRED in its band and takes
// most of it. The old 0.375 with a half-width shift existed only so two sides
// could share one axis; keeping it would render thin sticks pushed off centre.
const CANDLE_FRACTION = 0.62;

const swingColor = (s: EngineSwing) =>
  s.label === "EQH" || s.label === "EQL" ? "#f59e0b" : s.type === "high" ? "#f87171" : "#4ade80";

// ECharts custom-series render API (the subset used here).
interface RenderApi {
  value(dim: number): number;
  coord(v: number[]): number[];
  size(v: number[]): number[];
}
interface RenderParams {
  coordSys: { x: number; y: number; width: number; height: number };
}

/** One side's grouped candles as a custom series: data = [x, open, close, low, high]. */
function candleSeries(side: SideKey, candles: Candle[]) {
  const theme = OI_CANDLE_THEME[side];
  const shift = 0; // centred: one side per chart
  return {
    name: SIDE_NAME[side],
    type: "custom" as const,
    color: SIDE_COLOR[side],
    clip: true,
    data: candles.map((c, i) => [i, c.ohlc[0], c.ohlc[1], c.ohlc[2], c.ohlc[3]]),
    encode: { x: 0, y: [1, 2, 3, 4] },
    renderItem: (params: RenderParams, api: RenderApi) => {
      const x = api.value(0);
      const o = api.value(1);
      const c = api.value(2);
      const l = api.value(3);
      const h = api.value(4);
      const band = api.size([1, 0])[0];
      const w = Math.max(band * CANDLE_FRACTION, 1);
      const cx = api.coord([x, 0])[0] + shift * w;
      const yO = api.coord([x, o])[1];
      const yC = api.coord([x, c])[1];
      const yL = api.coord([x, l])[1];
      const yH = api.coord([x, h])[1];
      const up = c >= o;
      const fill = up ? theme.up : theme.down;
      const stroke = up ? theme.upBorder : theme.downBorder;
      const cs = params.coordSys;
      const inside = cx + w / 2 >= cs.x && cx - w / 2 <= cs.x + cs.width;
      if (!inside) return null;
      return {
        type: "group",
        children: [
          { type: "line", shape: { x1: cx, y1: yH, x2: cx, y2: yL }, style: { stroke, lineWidth: 1 } },
          {
            type: "rect",
            shape: { x: cx - w / 2, y: Math.min(yO, yC), width: w, height: Math.max(Math.abs(yO - yC), 1) },
            style: { fill, stroke, lineWidth: 1 },
          },
        ],
      };
    },
  };
}

export function OiStructureTimeChart({
  data, side, interval, persistKey, engine5mOn, showStructure = true, height, toggle,
}: Props) {
  const isLine = interval === 1;
  const isCall = side === "call";
  const chartRef = useRef<ReactECharts>(null);
  const [toolbarSlot, setToolbarSlot] = useState<HTMLDivElement | null>(null);

  const callPts = useMemo(() => toChangePointsCr(data.timestamps, data.call_series_cr), [data.timestamps, data.call_series_cr]);
  const putPts = useMemo(() => toChangePointsCr(data.timestamps, data.put_series_cr), [data.timestamps, data.put_series_cr]);

  // "open" when the series starts at 09:15 (±1 min); the first bucket otherwise.
  const sinceLabel = useMemo(() => {
    const first = data.timestamps[0]?.slice(11, 16);
    if (!first) return "open";
    return labelToMin(first) <= SESSION_OPEN_MIN + 1 ? "open" : first;
  }, [data.timestamps]);

  const sidePts = isCall ? callPts : putPts;
  const xLabels = useMemo(
    () => (isLine ? toLineSeries(sidePts).labels : bucketToCandles(sidePts, interval).map((c) => c.label)),
    [sidePts, isLine, interval],
  );

  const drawAnchor = useMemo(
    () => echartsCategoryAnchor(() => chartRef.current?.getEchartsInstance() ?? null, xLabels),
    [xLabels],
  );

  // Pixel → data readout for the drawing overlay (OICandleChart pattern).
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
      return { value: fmtCr(coord[1]), time };
    },
    [xLabels],
  );

  const option: EChartsOption = useMemo(() => {
    const n = data.timestamps.length;
    const labelIndex = new Map<string, number>();
    xLabels.forEach((l, i) => labelIndex.set(l, i));
    // Engine index (1-minute) → x category index in the current mode.
    const xOf = (idx: number): number | null => {
      if (idx < 0 || idx >= n) return null;
      if (isLine) return idx;
      return labelIndex.get(bucketStartIso(data.timestamps[idx], interval).slice(11, 16)) ?? null;
    };

    // ── overlays: zone bands, ATM-style level lines, breakout line, zero line,
    //    swing labels, RED-5m pins — all on ONE scatter series so the data
    //    series stay plain in both modes.
    const areas: unknown[] = [];
    const levels: Record<string, unknown>[] = [];
    const z = isCall ? data.call_zone : data.put_zone;
    const br = isCall ? data.call_breakout : data.put_breakout;
    if (showStructure && z) {
      areas.push([
        { yAxis: z.lo, itemStyle: { color: SIDE_TINT[side].fill, borderColor: SIDE_TINT[side].border } },
        { yAxis: z.hi },
      ]);
      const tag = side.toUpperCase();
      levels.push({ yAxis: z.hi, name: `${tag} RANGE HI ${fmtCr(z.hi)}`, label: { position: "insideStartTop" } });
      levels.push({ yAxis: z.lo, name: `${tag} RANGE LO ${fmtCr(z.lo)}`, label: { position: "insideStartTop" } });
      if (br) {
        levels.push({
          yAxis: br === "up" ? z.hi : z.lo,
          name: `${tag} BREAKOUT ${br.toUpperCase()}`,
          lineStyle: { color: SIDE_COLOR[side], type: "solid", width: 1.5 },
          label: { position: "insideStartBottom", borderColor: SIDE_COLOR[side] },
        });
      }
    }
    // The zero line is NOT structure — it is the axis reference, and it stays
    // on screen when the Structure toggle is off.
    levels.push({ yAxis: 0, lineStyle: ECHARTS_ZERO_MARKLINE.lineStyle, label: { show: false } });

    const swingData = (showStructure ? (isCall ? data.call_swings : data.put_swings) : [])
      .map((s) => ({ s, x: xOf(s.index) }))
      .filter((e): e is { s: EngineSwing; x: number } => e.x != null)
      .map(({ s, x }) => ({
        value: [x, s.value],
        itemStyle: { color: swingColor(s) },
        label: {
          show: true,
          position: s.type === "high" ? "top" : "bottom",
          fontSize: 8.5,
          color: swingColor(s),
          fontWeight: "bold",
          formatter: s.label,
        },
      }));

    const sideCandles = isLine ? [] : bucketToCandles(sidePts, interval);
    const pins: unknown[] = [];
    if (interval === 5) {
      const red = isCall ? data.call_red_5m : data.put_red_5m;
      // Index -2, the last FULLY CLOSED candle — the engine's own rule
      // (oi_structure.check_red_candle) and the reference
      // (docs/reference/oi_smc_engine.html: candles[candles.length-2]).
      // Pinning -1 marked a still-forming candle whose colour can still flip,
      // so the chart could show a pin the engine never acted on.
      if (red && sideCandles.length >= 2) {
        const lastClosed = sideCandles[sideCandles.length - 2];
        pins.push({
          coord: [sideCandles.length - 2, lastClosed.ohlc[3]],
          itemStyle: { color: isCall ? "#ef4444" : "#3b82f6" },
          label: { formatter: `RED 5m ${side.toUpperCase()}` },
        });
      }
    }

    const overlay = {
      name: "swings",
      type: "scatter",
      symbolSize: 7,
      data: swingData,
      z: 5,
      markArea: areas.length ? { silent: true, data: areas } : undefined,
      markLine: atmStyleMarkLine(levels, "{b}"),
      markPoint: pins.length
        ? {
            symbol: "pin",
            symbolSize: 44,
            label: { show: true, fontSize: 8, color: "#fff", fontWeight: "bold" },
            data: pins,
          }
        : undefined,
    } as unknown as SeriesOption;

    const base = {
      backgroundColor: "transparent",
      animation: true,
      animationDuration: 200,
      grid: OI_CHART_GRID,
      // One entry: a two-entry legend on a one-series chart renders a
      // permanently greyed-out toggle, and it costs 60px of a 220px panel.
      legend: {
        ...ECHARTS_LEGEND_BOTTOM,
        show: false,
        data: [{ name: SIDE_NAME[side], itemStyle: { color: SIDE_COLOR[side] } }],
      },
      tooltip: {
        trigger: "axis" as const,
        axisPointer: { type: "cross" as const, label: { backgroundColor: "#1f2937" } },
        ...ECHARTS_TOOLTIP_BOX,
        formatter: (params: unknown) => {
          const arr = params as Array<{ axisValue: string; seriesName: string; seriesType: string; data: unknown }>;
          const rows = arr.filter((p) => p.seriesType === "line" || p.seriesType === "custom");
          if (!rows.length) return "";
          const body = rows
            .map((p) => {
              const color = SIDE_COLOR[side];
              if (p.seriesType === "line") return tooltipRowHtml(color, p.seriesName, fmtCr(p.data as number));
              // Custom candle data: [idx, open, close, low, high].
              const [, open, close, low, high] = (p.data as number[]) ?? [];
              const row = (k: string, v: number) =>
                `<div style="display:flex;justify-content:space-between;gap:14px"><span style="color:#9ca3af">${k}</span><b style="color:#fff">${fmtCr(v)}</b></div>`;
              return (
                tooltipRowHtml(color, p.seriesName, fmtCr(close)) +
                `<div style="margin-left:14px">${row("Open", open)}${row("High", high)}${row("Low", low)}${row("Close", close)}</div>`
              );
            })
            .join("");
          return `<div style="font-size:12px;min-width:170px"><div style="margin-bottom:4px"><b style="color:#fff">${rows[0].axisValue}</b></div>${body}</div>`;
        },
      },
      xAxis: {
        type: "category" as const,
        data: xLabels,
        ...ECHARTS_CATEGORY_XAXIS,
        boundaryGap: true,
        axisLabel: { ...ECHARTS_CATEGORY_XAXIS.axisLabel, interval: Math.ceil(xLabels.length / 12) },
      },
      yAxis: {
        type: "value" as const,
        ...ECHARTS_VALUE_YAXIS,
        name: `OI Δ since ${sinceLabel}`,
        nameTextStyle: { color: "#6b7280", fontSize: 10 },
        min: (v: { min: number }) => Math.min(0, v.min),
        max: (v: { max: number }) => Math.max(0, v.max),
        axisLabel: { ...ECHARTS_VALUE_YAXIS.axisLabel, formatter: (val: number) => fmtCr(val) },
      },
      dataZoom: ECHARTS_DATAZOOM_INSIDE,
    };

    const lineSeries = (side: SideKey, values: number[]) => ({
      name: SIDE_NAME[side],
      type: "line" as const,
      data: values,
      showSymbol: false,
      smooth: false,
      lineStyle: { color: OI_CANDLE_THEME[side].line, width: 1.5 },
      itemStyle: { color: OI_CANDLE_THEME[side].line },
      areaStyle: { color: OI_CANDLE_THEME[side].line, opacity: 0.08 },
    });

    const series: SeriesOption[] = isLine
      ? [lineSeries(side, toLineSeries(sidePts).values), overlay]
      : [candleSeries(side, sideCandles) as unknown as SeriesOption, overlay];

    return { ...base, series } satisfies EChartsOption;
  }, [data, side, isCall, interval, isLine, sidePts, xLabels, sinceLabel, showStructure]);

  const hasData = data.timestamps.length > 0;
  const red = isCall ? data.call_red_5m : data.put_red_5m;
  const swings = isCall ? data.call_swings : data.put_swings;
  const breakout = isCall ? data.call_breakout : data.put_breakout;
  const sideLabel = isCall ? "Call" : "Put";
  const chartH = height ?? OI_CHART_HEIGHT;

  return (
    <div className="panel p-4 relative">
      <div className="flex items-center gap-2 mb-2 flex-wrap">
        <span className="inline-block w-2.5 h-2.5 rounded-sm" style={{ background: SIDE_COLOR[side] }} />
        <h3 className="text-sm font-semibold text-foreground">
          {sideLabel} OI Change · {isLine ? "1 Min" : `${interval} Min`}
        </h3>
        {/* Reference title suffix: "· 16 swings, breakout DOWN". */}
        {isLine && (
          <span className="text-[11px] text-muted">
            · {swings.length} swings{breakout ? `, breakout ${breakout.toUpperCase()}` : ""}
          </span>
        )}
        {sinceLabel !== "open" && (
          <span
            className="text-[10px] text-amber-300/90"
            title="Series starts after 09:15 — the 0-baseline is its first bucket, and session-anchored candles may not match the engine's 5-bar chunking."
          >
            Δ from {sinceLabel} (data start)
          </span>
        )}
        <div className="ml-auto flex items-center gap-3">
          {toggle}
          <div ref={setToolbarSlot} className="flex items-center" />
        </div>
      </div>

      {!hasData ? (
        <div className="flex items-center justify-center text-muted text-sm" style={{ height: chartH }}>
          No OI series for this window.
        </div>
      ) : (
        <div className="relative" style={{ height: chartH }}>
          <ReactECharts
            ref={chartRef}
            option={option}
            notMerge
            lazyUpdate={false}
            style={{ height: chartH, width: "100%" }}
            opts={{ renderer: "canvas" }}
          />
          <ChartDrawingOverlay describeAt={describeAt} persistKey={`oistruct-${persistKey}`} toolbarContainer={toolbarSlot} anchor={drawAnchor} />
        </div>
      )}

      {/* Reference footer (docs/reference/oi_smc_engine.html): the 5-Minute
          charts carry the fixed red-candle rule; the 1-Minute charts carry the
          structure readout in their title instead. */}
      {!isLine && interval === 5 && (
        <div className="mt-2 text-[10.5px] text-muted">
          Last candle: {red ? `RED -> routes to ${sideLabel}` : "Green -> ignored"}
          {engine5mOn ? "" : " · 5m rule OFF"}
        </div>
      )}
    </div>
  );
}
