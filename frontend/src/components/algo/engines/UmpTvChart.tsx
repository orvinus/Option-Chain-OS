/**
 * Ultra Master Pro premium chart on TradingView's own chart engine
 * (lightweight-charts) — with the Pine script's COMPLETE visual contract:
 *
 *  - level lines per type in the Pine palette (1H-STRUCT red solid w2,
 *    DISCOVERY green dashed, BRIDGE cyan dashed, MEDIAN dotted in the
 *    configurable median colour/width), gated by the four Show toggles
 *  - the script's signature translucent ZONE BOXES (±zonePct around every
 *    rendered level, border ~40% alpha / fill ~18% alpha) via a canvas
 *    overlay — RENDERING only, the engine always keeps the full level set
 *  - entry labels with Pine's per-scenario colours (S1A/S2A/S3A green,
 *    S1B/S2B/S3B teal, S1C amber, S3C orange above-bar, R1/R2 cyan/lime),
 *    exit labels with Pine's colours and placements (TARGET green above,
 *    stops red/orange below), trail ✦ gold and System-B ◈ cyan labels with
 *    their prices — honouring Show Entry Signals / Show Trail Labels
 *  - Step-4 guide lines while in trade (entry dotted green, Base SL dashed
 *    red) plus the platform's Q-ladder/NB/MAX-SL/trail lines, honouring
 *    Show SL Lines
 *  - native pan/zoom (zoom state survives refreshes), crosshair, OHLC
 *    legend, and the platform drawing toolbar
 */
import { useEffect, useMemo, useRef, useState } from "react";
import {
  createChart,
  LineStyle,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type SeriesMarker,
  type UTCTimestamp,
} from "lightweight-charts";
import { baseChartOptions } from "../../charts/chartTheme";
import { ChartDrawingOverlay } from "../../ChartDrawingOverlay";
import { lwcAnchor } from "../../charts/drawingAnchors";
import type { UmpDisplayInterval, UmpEvalResponse, UmpParams } from "../../../types/algo";
import { istToChartTime, snapToCandle } from "./umpReplay";

/** Display-interval pills (§3), plus "entry" = the zone's own entry timeframe.
 *
 *  HIDDEN-2026-09-11 (user request): only 1m · 5m · 15m · 1h · 1d · 1W are
 *  shown, in BOTH Algo Config and Backtesting — this component renders the
 *  pills for both pages. Everything else (1s, 5s, 15s, 30s, 3m, 10m, 30m,
 *  2h, 3h) is hidden from the UI ONLY. Nothing is removed from the backend:
 *  `GET /api/algo/engines/ump?interval=…` still serves every one of them, the
 *  1-second series is still fetched, and `UMP_DISPLAY_INTERVALS` in
 *  types/algo.ts still lists the full set.
 *
 *  TO UNLOCK ALL TIMEFRAMES: swap each pair below (comment the short row,
 *  un-comment the full row) and delete the `HIDDEN_INTERVALS` guard in
 *  UmpPanel.tsx. Those are the only two files involved. */
const INTERVAL_GROUPS: UmpDisplayInterval[][] = [
  // ["1s", "5s", "15s", "30s"],                    // HIDDEN-2026-09-11 — full seconds row
  // ["1m", "3m", "5m", "10m", "15m", "30m"],       // HIDDEN-2026-09-11 — full minutes row
  ["1m", "5m", "15m"],
  // ["1h", "2h", "3h", "1d", "1W"],                // HIDDEN-2026-09-11 — full hours/day/week row
  ["1h", "1d", "1W"],
];

function intervalLabel(sec: number): string {
  if (sec < 60) return `${sec}-second`;
  if (sec < 3600) return `${sec / 60}-minute`;
  if (sec < 86400) return `${sec / 3600}-hour`;
  if (sec < 604800) return "daily (session)";
  return "weekly (Mon-anchored)";
}

const HEX = /^#[0-9a-fA-F]{6}$/;

/** Pine level palette; median colour/width come from the Visual settings. */
function levelStyle(type: number, medianColor: string, medianWidth: number) {
  switch (type) {
    case 1:
      return { color: "#FF1744", style: LineStyle.Solid, width: 2 as const, icon: "◆" };
    case 2:
      return { color: "#00E676", style: LineStyle.Dashed, width: 1 as const, icon: "▲" };
    case 4:
      return {
        color: HEX.test(medianColor) ? medianColor : "#FFD700",
        style: LineStyle.Dotted,
        width: (Math.min(4, Math.max(1, Math.round(medianWidth))) as 1 | 2 | 3 | 4),
        icon: "✦",
      };
    default:
      return { color: "#00E5FF", style: LineStyle.Dashed, width: 1 as const, icon: "◇" };
  }
}

function typeShown(type: number, v: UmpParams["visual"]): boolean {
  if (type === 1) return v.show_struct;
  if (type === 2) return v.show_discovery;
  if (type === 4) return v.show_median;
  return v.show_bridge;
}

// Pine entry-label palette (f_store calls L845-1034). Exported so the
// per-scenario switches in the settings card share the marker colours.
export const ENTRY_STYLE: Record<string, { color: string; above: boolean }> = {
  S1A: { color: "#00E676", above: false },
  S1B: { color: "#00BCD4", above: false },
  S1C: { color: "#FFC107", above: false },
  S2A: { color: "#00E676", above: false },
  S2B: { color: "#00BCD4", above: false },
  S3A: { color: "#00E676", above: false },
  S3B: { color: "#00BCD4", above: false },
  S3C: { color: "#FF9800", above: true },   // Pine's one above-bar entry label
  R1: { color: "#18FFFF", above: false },
  R2: { color: "#76FF03", above: false },
};

// Pine exit-label palette + placement (L1195-1313: up=true → below bar).
const EXIT_STYLE: Record<string, { color: string; above: boolean }> = {
  MAX_SL: { color: "#FF1744", above: false },
  TRAIL_EXIT: { color: "#FF9800", above: false },
  SB_EXIT: { color: "#FF9800", above: false },
  TARGET: { color: "#00E676", above: true },
  BASE_SL: { color: "#FF1744", above: false },
};

const TRAIL_KINDS: Record<string, { color: string; prefix: string }> = {
  TRAIL_SET: { color: "#FFC107", prefix: "✦ " },
  TRAIL_RAISE: { color: "#FFD700", prefix: "✦ " },
  SB_SET: { color: "#18B6D6", prefix: "" },   // engine text already carries ◈
  SB_RAISE: { color: "#18B6D6", prefix: "" },
};

interface OhlcRow {
  time: UTCTimestamp;
  open: number;
  high: number;
  low: number;
  close: number;
}

export function UmpTvChart({
  data,
  params,
  liveBar,
  interval = "entry",
  onIntervalChange,
  replaying = false,
}: {
  data: UmpEvalResponse;
  params: UmpParams;
  /** Display interval selector (§3); the engine's evaluation never changes. */
  interval?: UmpDisplayInterval;
  onIntervalChange?: (v: UmpDisplayInterval) => void;
  /** True while the replay playhead drives `data` — enables auto-follow. */
  replaying?: boolean;
  /** The FORMING display bar from /ws/algo-stream (the entry-TF bar, or the
   *  1-minute bar when the display interval is 1m): the bucket's IST start plus
   *  its running OHLC. Applied incrementally via series.update() so the candle
   *  grows tick by tick instead of the whole series being replaced on every
   *  refresh (which is what made it jump in 30-second steps). Time conversion
   *  happens here so callers never need this module's private helpers. */
  liveBar?: { ts: string; o: number; h: number; l: number; c: number } | null;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const zoneCanvasRef = useRef<HTMLCanvasElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candlesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const priceLinesRef = useRef<IPriceLine[]>([]);
  const legendRef = useRef<HTMLDivElement | null>(null);
  const lastFitKeyRef = useRef<string>("");
  const lastDataKeyRef = useRef<string>("");
  const lastBucketRef = useRef<number>(0);
  const prevLenRef = useRef<number>(0);
  const liveBarRef = useRef<UTCTimestamp | null>(null);
  const liveBarAtRef = useRef<number>(0);
  const lastBarSigRef = useRef<string>("");
  const [toolbarSlot, setToolbarSlot] = useState<HTMLElement | null>(null);
  const drawAnchor = useMemo(
    () => lwcAnchor(() => chartRef.current, () => candlesRef.current, { timeOfDay: false }),
    [],
  );
  // Zone boxes drawn each frame from the latest data+params (refs so the
  // rAF painter never captures stale props).
  const zonesRef = useRef<{ price: number; type: number }[]>([]);
  const paramsRef = useRef(params);
  paramsRef.current = params;

  const bucketSec = data.candles_interval || data.entry_timeframe_min * 60 || 300;
  const candles: OhlcRow[] = useMemo(
    () =>
      (data.candles ?? data.entry_candles ?? [])
        .map((c) => ({
          time: istToChartTime(c.ts),
          open: c.o,
          high: c.h,
          low: c.l,
          close: c.c,
        }))
        .sort((a, b) => (a.time as number) - (b.time as number)),
    [data.candles, data.entry_candles],
  );
  const candleTimes = useMemo(() => candles.map((c) => c.time as number), [candles]);

  /**
   * Bucket starts a marker may attach to: every CLOSED candle plus, during
   * replay, the FORMING one.
   *
   * The forming bar is painted by `series.update()` and is a real bar on the
   * chart, but it is deliberately absent from `candles` so the closed slice
   * stays stable (rebuilding it every frame would discard the growing bar).
   * Snapping against `candles` alone therefore pushed any marker inside the
   * forming candle BACK onto the previous one — an entry appeared a candle
   * early and then visibly jumped forward the moment its candle closed.
   */
  const snapTimes = useMemo(() => {
    if (!liveBar) return candleTimes;
    const t = istToChartTime(liveBar.ts) as number;
    const last = candleTimes[candleTimes.length - 1];
    return last == null || t > last ? [...candleTimes, t] : candleTimes;
  }, [candleTimes, liveBar]);

  // Create the chart ONCE.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const chart = createChart(el, {
      ...baseChartOptions(),
      height: 460,
      timeScale: {
        borderColor: "rgba(45,55,72,0.8)",
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 6,
      },
    });
    chartRef.current = chart;
    const series = chart.addCandlestickSeries({
      upColor: "#22c55e",
      downColor: "#ef4444",
      borderUpColor: "#22c55e",
      borderDownColor: "#ef4444",
      wickUpColor: "#22c55e",
      wickDownColor: "#ef4444",
      priceLineVisible: false,
    });
    candlesRef.current = series;

    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      if (w) chart.applyOptions({ width: Math.floor(w) });
    });
    ro.observe(el);

    const renderLegend = (row: OhlcRow | null) => {
      const tip = legendRef.current;
      if (!tip) return;
      if (!row) {
        tip.innerHTML = "";
        return;
      }
      const chg = row.open !== 0 ? ((row.close - row.open) / row.open) * 100 : 0;
      const col = row.close >= row.open ? "#22c55e" : "#ef4444";
      const f = (v: number) => `₹${v.toFixed(2)}`;
      tip.innerHTML =
        `<span style="color:#9ca3af">O</span> <b style="color:${col}">${f(row.open)}</b> ` +
        `<span style="color:#9ca3af">H</span> <b style="color:${col}">${f(row.high)}</b> ` +
        `<span style="color:#9ca3af">L</span> <b style="color:${col}">${f(row.low)}</b> ` +
        `<span style="color:#9ca3af">C</span> <b style="color:${col}">${f(row.close)}</b> ` +
        `<b style="color:${col}">${chg >= 0 ? "+" : ""}${chg.toFixed(2)}%</b>`;
    };
    chart.subscribeCrosshairMove((param) => {
      const s = candlesRef.current;
      if (!s) return;
      const bar = param.time != null ? (param.seriesData.get(s) as OhlcRow | undefined) : undefined;
      const all = (s.data() as OhlcRow[]) ?? [];
      renderLegend(bar ?? (all.length ? all[all.length - 1] : null));
    });

    // ── Zone-box painter (Pine L643-649): a translucent ±zonePct band
    // around every RENDERED level. Painted every frame from price→pixel
    // coordinates so pan/zoom/autoscale stay glued; ~40 rects, negligible.
    let raf = 0;
    const paint = () => {
      raf = requestAnimationFrame(paint);
      const canvas = zoneCanvasRef.current;
      const s = candlesRef.current;
      if (!canvas || !s || !el) return;
      const w = el.clientWidth;
      const hpx = el.clientHeight;
      if (canvas.width !== w || canvas.height !== hpx) {
        canvas.width = w;
        canvas.height = hpx;
      }
      const ctx = canvas.getContext("2d");
      if (!ctx) return;
      ctx.clearRect(0, 0, w, hpx);
      const p = paramsRef.current;
      const zp = p.visual.zone_width_pct / 100;
      for (const z of zonesRef.current) {
        const st = levelStyle(z.type, p.visual.median_color, p.visual.median_line_width);
        const yTop = s.priceToCoordinate(z.price * (1 + zp));
        const yBot = s.priceToCoordinate(z.price * (1 - zp));
        if (yTop == null || yBot == null) continue;
        const y = Math.min(yTop, yBot);
        const bh = Math.abs(yBot - yTop);
        if (y > hpx || y + bh < 0) continue;
        ctx.fillStyle = `${st.color}2E`;      // ≈18% fill (Pine bgcolor transp 82)
        ctx.strokeStyle = `${st.color}66`;    // ≈40% border (Pine transp 60)
        ctx.lineWidth = 1;
        ctx.fillRect(0, y, w, bh);
        ctx.strokeRect(0.5, y + 0.5, w - 1, bh - 1);
      }
    };
    raf = requestAnimationFrame(paint);

    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      candlesRef.current = null;
      priceLinesRef.current = [];
    };
  }, []);

  // Data + levels + markers reconcile on every eval refresh AND settings edit.
  useEffect(() => {
    const chart = chartRef.current;
    const series = candlesRef.current;
    if (!chart || !series) return;
    const v = params.visual;
    const e = params.entry;

    // Replace the whole series ONLY when its content actually changes —
    // a new contract, or a newly CLOSED bar arriving from the eval refresh.
    // Calling setData on every render is what stopped the forming candle from
    // ever animating: each repaint discarded the in-progress bar.
    const contentKey =
      `${data.symbol}|${data.strike}|${data.option_type}|${bucketSec}|` +
      `${candles.length}|${candles[0]?.time ?? 0}|${candles[candles.length - 1]?.time ?? 0}`;
    if (contentKey !== lastDataKeyRef.current) {
      lastDataKeyRef.current = contentKey;
      // TradingView-style auto-follow during replay: keep following the
      // newest bar only if the previous newest bar was on screen; a user who
      // scrolled away keeps their view.
      const vr = chart.timeScale().getVisibleLogicalRange();
      const wasFollowing = replaying && vr != null && vr.to >= prevLenRef.current - 1.5;
      series.setData(candles);
      prevLenRef.current = candles.length;
      if (wasFollowing) chart.timeScale().scrollToPosition(6, false);
      const lb = candles[candles.length - 1];
      lastBarSigRef.current = lb ? `${lb.time}|${lb.open}|${lb.high}|${lb.low}|${lb.close}` : "";
    } else if (candles.length > 0) {
      // Same candles, but the LAST one moved: the 30-second refresh carries
      // the still-forming bucket. The key above ignores prices, so this update
      // used to be dropped — and whenever the stream's live candle was
      // unavailable the chart sat still for a whole 5-minute candle
      // (reported 2026-09-23). Applied in place; skipped only while a FRESH
      // stream bar already covers this candle (it is newer than the refresh).
      const lb = candles[candles.length - 1];
      const sig = `${lb.time}|${lb.open}|${lb.high}|${lb.low}|${lb.close}`;
      if (sig !== lastBarSigRef.current) {
        lastBarSigRef.current = sig;
        const streamCovers =
          liveBarRef.current != null &&
          (liveBarRef.current as number) >= (lb.time as number) &&
          performance.now() - liveBarAtRef.current < 15_000;
        if (!streamCovers) {
          try {
            series.update(lb);
          } catch {
            /* a newer bar is already on the series — the next setData corrects */
          }
        }
      }
    }
    if (bucketSec !== lastBucketRef.current) {
      // Interval switch: seconds on the axis for sub-minute candles, and a
      // one-off refit only when nothing of the new series is on screen.
      lastBucketRef.current = bucketSec;
      chart.applyOptions({ timeScale: { secondsVisible: bucketSec < 60 } });
      const vr = chart.timeScale().getVisibleRange();
      const anyVisible =
        vr != null &&
        candles.some((c) => (c.time as number) >= (vr.from as number) && (c.time as number) <= (vr.to as number));
      if (!anyVisible && candles.length > 0) chart.timeScale().fitContent();
    }
    // Snap an engine instant to the display candle that contains it —
    // including the forming one (see `snapTimes`).
    const snap = (iso: string): UTCTimestamp | null => {
      const i = snapToCandle(snapTimes, istToChartTime(iso) as number);
      return i < 0 ? null : (snapTimes[i] as UTCTimestamp);
    };

    for (const pl of priceLinesRef.current) series.removePriceLine(pl);
    priceLinesRef.current = [];
    const addLine = (
      price: number | null | undefined,
      title: string,
      color: string,
      style: LineStyle,
      width: 1 | 2 | 3 | 4 = 1,
    ) => {
      if (price == null || !Number.isFinite(price)) return;
      priceLinesRef.current.push(
        series.createPriceLine({
          price,
          title,
          color,
          lineStyle: style,
          lineWidth: width,
          axisLabelVisible: true,
        }),
      );
    };

    // Level lines + the zone-box list — Show toggles filter RENDERING ONLY
    // (Pine's toggles gate the drawing loop, never outP: the engine always
    // trades the full set).
    const shownZones: { price: number; type: number }[] = [];
    for (const lv of data.levels ?? []) {
      if (!typeShown(lv.type, v)) continue;
      const st = levelStyle(lv.type, v.median_color, v.median_line_width);
      addLine(lv.price, `${st.icon} ${lv.name}`, st.color, st.style, st.width);
      shownZones.push({ price: lv.price, type: lv.type });
    }
    zonesRef.current = shownZones;

    // Active-trade context (present only while in a trade, per the API).
    const z = data.active_zone;
    if (z) {
      addLine(z.zone_top, "ZT", "#3b82f6", LineStyle.Dotted);
      addLine(z.upper_median, "UM", "#3b82f6", LineStyle.Dotted);
      addLine(z.lower_median, "LM", "#3b82f6", LineStyle.Dotted);
      addLine(z.zone_bottom, "ZB", "#3b82f6", LineStyle.Dotted);
    }
    const q = data.q_levels;
    if (q && e.show_sl_lines) {
      addLine(q.q1, "Q1", "#f59e0b", LineStyle.Dotted);
      addLine(q.q2, "Q2", "#f59e0b", LineStyle.Dotted);
      addLine(q.q3, "Q3", "#f59e0b", LineStyle.Dotted);
      addLine(q.nb, "NB TARGET", "#22c55e", LineStyle.Dashed, 2);
    }
    if (e.show_sl_lines) {
      // Pine Step-4 guide lines (in-trade only): entry dotted green, Base SL
      // dashed red — plus the platform's live stop lines.
      addLine(data.entry_price, "ENTRY", "#00E676", LineStyle.Dotted);
      addLine(data.base_level, "BASE SL", "#FF1744", LineStyle.Dashed);
      addLine(data.max_sl, "MAX SL", "#ef4444", LineStyle.Dashed, 2);
      addLine(data.trail_sl, "TRAIL", "#f59e0b", LineStyle.Solid, 2);
      addLine(data.sb_trail_sl, "ZONE TRAIL", "#06b6d4", LineStyle.Dashed);
    }

    // Markers — built entirely from engine events (entry, exit, trail, SB),
    // Pine colours and placements. Show Entry Signals gates ALL of them
    // (Pine's signal redraw is inside `if showEntrySignals`); trail/System-B
    // labels additionally need Show Trail Labels — exactly the script's
    // store-time + draw-time gating.
    const markers: SeriesMarker<UTCTimestamp>[] = [];
    if (e.show_entry_signals) {
      for (const ev of data.events ?? []) {
        const t = snap(ev.ts);
        if (t == null) continue;
        const entry = ENTRY_STYLE[ev.kind];
        const exit = EXIT_STYLE[ev.kind];
        const trail = TRAIL_KINDS[ev.kind];
        if (entry) {
          markers.push({
            time: t,
            position: entry.above ? "aboveBar" : "belowBar",
            color: entry.color,
            shape: entry.above ? "arrowDown" : "arrowUp",
            text: `▲ ${ev.kind} ₹${ev.price.toFixed(2)}`,
          });
        } else if (exit) {
          markers.push({
            time: t,
            position: exit.above ? "aboveBar" : "belowBar",
            color: exit.color,
            shape: exit.above ? "arrowUp" : "arrowDown",
            text: ev.text.startsWith("✗") || ev.text.startsWith("✦") || ev.text.startsWith("◆") || ev.text.startsWith("◈")
              ? ev.text
              : `${ev.kind} ₹${ev.price.toFixed(2)}`,
          });
        } else if (trail && e.show_trail_labels) {
          markers.push({
            time: t,
            position: "aboveBar",
            color: trail.color,
            shape: "circle",
            text: `${trail.prefix}${ev.text}`,
          });
        }
      }
    }
    markers.sort((a, b) => (a.time as number) - (b.time as number));
    series.setMarkers(markers);

    // Contract-only key: as-of cursor changes (date/at) must NOT refit —
    // the user's pan/zoom survives every replay refresh. A NEW contract
    // opens zoomed to the expiry-to-expiry week (focus_window), with the
    // full stored life scrollable to the left — exactly the TV experience.
    const fitKey = `${data.symbol}|${data.strike}|${data.option_type}`;
    if (fitKey !== lastFitKeyRef.current) {
      lastFitKeyRef.current = fitKey;
      const fw = data.focus_window;
      if (fw && candles.length > 0) {
        const from = istToChartTime(`${fw.from}T09:15:00`);
        const to = istToChartTime(`${fw.to}T15:30:00`);
        const anyInside = candles.some(
          (c) => (c.time as number) >= (from as number) && (c.time as number) <= (to as number)
        );
        if (anyInside) {
          chart.timeScale().setVisibleRange({ from, to });
        } else {
          chart.timeScale().fitContent();
        }
      } else {
        chart.timeScale().fitContent();
      }
    }
  }, [candles, candleTimes, snapTimes, data, params, bucketSec, replaying]);

  // ── the forming bar, applied incrementally ────────────────────────────
  // lightweight-charts replaces the last bar when `time` matches and appends
  // when it is newer — which is exactly "grow, then roll over". No setData,
  // no refit, so pan/zoom and the drawing overlay are untouched.
  useEffect(() => {
    const series = candlesRef.current;
    if (!series || !liveBar) return;
    const t = istToChartTime(liveBar.ts);
    const last = candles[candles.length - 1];
    // Never rewrite history: only the last CLOSED bar or a brand-new one.
    if (last && (t as number) < (last.time as number)) return;
    try {
      series.update({ time: t, open: liveBar.o, high: liveBar.h, low: liveBar.l, close: liveBar.c });
      liveBarRef.current = t;
      liveBarAtRef.current = performance.now();
    } catch {
      /* a stale bar during a contract switch — the next setData corrects it */
    }
  }, [liveBar, candles]);

  return (
    <div className="panel px-3 py-2">
      <div className="flex items-center gap-2 flex-wrap mb-1">
        <span className="text-[12px] font-bold text-gray-100">
          {data.symbol} {data.strike} {data.option_type} · {intervalLabel(bucketSec)} premium
        </span>
        <span className="text-[10px] text-muted" title="The candle the engine evaluates entries on (per-zone setting)">
          entry TF {data.entry_timeframe_min}m
        </span>
        {/* HIDDEN-2026-09-11 — the sub-minute half of the tooltip below is
            commented out along with the seconds pills. Restore it together:
            title="Display interval only — the engine keeps evaluating its entry timeframe. Sub-minute = today's 1-second ticks; minute and longer are anchored at 09:15 like TradingView's NSE bars." */}
        {onIntervalChange && (
          <span
            className="flex items-center gap-1 flex-wrap"
            title="Display interval only — the engine keeps evaluating its entry timeframe. Candles are anchored at 09:15 like TradingView's NSE bars."
          >
            {INTERVAL_GROUPS.map((group, gi) => (
              <span key={gi} className="flex items-center gap-0.5 border border-border/40 rounded-full px-1">
                {group.map((key) => (
                  <button
                    key={key}
                    type="button"
                    className={`pill text-[10px] py-0.5 px-2 ${interval === key ? "pill-active" : ""}`}
                    onClick={() => onIntervalChange(key)}
                  >
                    {key}
                  </button>
                ))}
              </span>
            ))}
            <button
              type="button"
              className={`pill text-[10px] py-0.5 ${interval === "entry" ? "pill-active" : ""}`}
              onClick={() => onIntervalChange("entry")}
            >
              entry ({data.entry_timeframe_min}m)
            </button>
          </span>
        )}
        {data.candles_note && (
          <span className="text-[10px] text-amber-300">{data.candles_note}</span>
        )}
        {replaying && (
          <span className="text-[10px] font-bold px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-300">REPLAY</span>
        )}
        <span className="text-[10px] text-accent/90 font-semibold">
          {data.first_session_date} → {data.session_date} · {data.session_dates.length}{" "}
          session{data.session_dates.length === 1 ? "" : "s"}
        </span>
        <span className="text-[10px] text-muted">
          drag = pan · wheel = zoom · double-click = reset · drawing tools →
        </span>
        <div ref={setToolbarSlot} className="ml-auto" />
      </div>
      <div ref={containerRef} style={{ width: "100%", height: 460, position: "relative" }}>
        <canvas
          ref={zoneCanvasRef}
          style={{
            position: "absolute",
            inset: 0,
            zIndex: 5,
            pointerEvents: "none",
          }}
        />
        <div
          ref={legendRef}
          style={{
            position: "absolute",
            zIndex: 15,
            top: 6,
            left: 8,
            fontSize: 11,
            pointerEvents: "none",
            background: "rgba(10,15,30,0.55)",
            borderRadius: 4,
            padding: "2px 6px",
          }}
        />
        <ChartDrawingOverlay persistKey={`ump-${data.day}-${data.zone}`} toolbarContainer={toolbarSlot} anchor={drawAnchor} />
      </div>
      <div className="text-[9.5px] text-muted mt-1">
        Pine-parity rendering: zone boxes ±{params.visual.zone_width_pct}% around every
        shown level, per-scenario entry colours, TARGET green above / stops red below,
        ✦ trail and ◈ System-B labels. The Show toggles hide drawings only — the engine
        always evaluates the full level set, exactly like the script.
      </div>
    </div>
  );
}
