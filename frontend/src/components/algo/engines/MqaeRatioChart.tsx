/**
 * The MQAE ("Ratio") panel's main chart — a replica of the dashboard's Ratio
 * tab (`RatioChartPage`: lightweight-charts, ratio amber then PCR green, the
 * 1m…30m / Full Day pills, header readout, Call/Put OI tooltip rows) fed by
 * the ENGINE's own series, with the engine overlays the reference chart had:
 * SMC swing labels as markers, order blocks as translucent boxes from their
 * origin to the right edge, and the rider trail as a dashed cyan line.
 *
 * Calculations stay the engine's: bucket pills re-aggregate the closed
 * 1-minute arrays client-side (`lastInBucket` = the level at bucket close,
 * exactly what the backend `time_bucket` path gives the Ratio tab), and Full
 * Day is the dashboard's cumulative-since-open mode over the raw OI totals.
 * Overlays are index-based in level-ratio units, so Full Day hides them.
 */
import { useMemo, useRef, useState } from "react";
import { LineStyle, type SeriesMarker, type UTCTimestamp } from "lightweight-charts";
import type { MqaeEvalResponse, MqaeParams } from "../../../types/algo";
import { ChartDrawingOverlay } from "../../ChartDrawingOverlay";
import { SERIES_COLORS } from "../../charts/chartTheme";
import {
  TimeSeriesChart,
  type ChartBox,
  type CrosshairInfo,
  type LinePoint,
  type LineSeriesSpec,
  type TimeSeriesChartHandle,
} from "../../charts/TimeSeriesChart";
import { bucketChartTime, cumulativeRatioSeries, lastInBucket } from "../../../utils/chartBuckets";
import { istIsoToChartTime } from "../../charts/chartTime";
import { lazyAnchor } from "../../charts/drawingAnchors";
import { compact, fmt4 } from "../../../utils/num";

const GREEN = "#22c55e";
const YELLOW = "#fbbf24";
const RIDER = "#39c5cf";
// Exact markArea tints of the previous ECharts overlay.
const OB_GREEN_FILL = "rgba(88,166,255,0.14)";
const OB_YELLOW_FILL = "rgba(245,158,11,0.12)";
// Ratio/PCR move in the 3rd-4th decimal minute to minute; two decimals made a
// narrow axis read "1.59" on every gridline.
const fmtLevel = (v: number) => v.toFixed(4);

// Timeframe pills — same labels/classes as the Ratio tab.
// Exported (2026-09-13) because the TIME-REPLAY slider in the parent steps at
// the SELECTED bucket, so the bucket had to stop being this component's private
// state. Full Day maps to 1 minute deliberately: it is not a coarse bucket at
// all, it is a cumulative-since-open view over the 1-minute data whose value
// changes every minute, so it scrubs minute by minute.
export const BUCKETS = ["1m", "5m", "10m", "15m", "30m", "full_day"] as const;
export type Bucket = (typeof BUCKETS)[number];
export const BUCKET_LABEL: Record<Bucket, string> = {
  "1m": "1m", "5m": "5m", "10m": "10m", "15m": "15m", "30m": "30m", full_day: "Full Day",
};
export const BUCKET_MIN: Record<Bucket, number> = {
  "1m": 1, "5m": 5, "10m": 10, "15m": 15, "30m": 30, full_day: 1,
};

interface Props {
  data: MqaeEvalResponse;
  params: MqaeParams;
  /** Drawing persistence scope (e.g. `${day}-${zone}`). */
  persistKey: string;
  /** Controlled by the panel so the replay slider can step at this interval. */
  bucket: Bucket;
  onBucketChange: (b: Bucket) => void;
}

function toPoints(times: UTCTimestamp[], values: number[]): LinePoint[] {
  const out: LinePoint[] = [];
  for (let i = 0; i < times.length; i++) {
    if (Number.isFinite(values[i])) out.push({ time: times[i], value: values[i] });
  }
  return out;
}

export function MqaeRatioChart({ data, params, persistKey, bucket, onBucketChange }: Props) {
  const [hover, setHover] = useState<CrosshairInfo | null>(null);
  const [toolbarSlot, setToolbarSlot] = useState<HTMLDivElement | null>(null);
  const chartRef = useRef<TimeSeriesChartHandle>(null);
  const drawAnchor = useMemo(() => lazyAnchor(() => chartRef.current?.anchor), []);

  const isFullDay = bucket === "full_day";
  const iv = BUCKET_MIN[bucket];
  const hasTotals =
    Array.isArray(data.total_call_oi) &&
    Array.isArray(data.total_put_oi) &&
    data.total_call_oi.length === data.timestamps.length &&
    data.total_put_oi.length === data.timestamps.length;

  const { series, positions, boxes } = useMemo(() => {
    const meta = new Map<number, { call: number; put: number }>();
    let ratioData: LinePoint[] = [];
    let pcrData: LinePoint[] = [];
    const boxes: ChartBox[] = [];
    const extra: LineSeriesSpec[] = [];
    let ratioMarkers: SeriesMarker<UTCTimestamp>[] = [];
    let pcrMarkers: SeriesMarker<UTCTimestamp>[] = [];

    if (isFullDay && hasTotals) {
      const cum = cumulativeRatioSeries(data.timestamps, data.total_call_oi!, data.total_put_oi!);
      ratioData = cum.ratio;
      pcrData = cum.pcr;
      for (const [t, v] of cum.positions) meta.set(t, v);
    } else {
      const r = lastInBucket(data.timestamps, data.yellow_ratio, iv);
      const g = lastInBucket(data.timestamps, data.green_pcr, iv);
      ratioData = toPoints(r.times, r.values);
      pcrData = toPoints(g.times, g.values);
      if (hasTotals) {
        for (let i = 0; i < r.times.length; i++) {
          const src = r.indexOfLastMinute[i];
          meta.set(r.times[i], { call: data.total_call_oi![src], put: data.total_put_oi![src] });
        }
      }
    }

    // Engine overlays. The indices point into the series the models ACTUALLY
    // ran on (`engine_timestamps`, one per closed candle of the engine's
    // timeframe), so they land on the matching candles at every timeframe,
    // Full Day included. Drawn only when the chart shows that same timeframe
    // — a mismatch (the moment between a click and its re-evaluation) skips
    // them rather than placing them on the wrong candles.
    const engTs = data.engine_timestamps ?? data.timestamps;
    const engTf = data.timeframe ?? "1m";
    if (engTf === bucket && !(isFullDay && !hasTotals)) {
      const n = engTs.length;
      const timeAt = (idx: number) =>
        data.engine_timestamps ? istIsoToChartTime(engTs[idx]) : bucketChartTime(engTs[idx], iv);
      const mk = (swings: MqaeEvalResponse["green_swings"], color: string): SeriesMarker<UTCTimestamp>[] =>
        params.smc_on
          ? swings
              .filter((s) => s.index >= 0 && s.index < n)
              .map((s) => ({
                time: timeAt(s.index),
                position: s.type === "high" ? "aboveBar" : "belowBar",
                shape: "circle",
                size: 0.6,
                color,
                text: s.label,
              }))
          : [];
      pcrMarkers = mk(data.green_swings, GREEN);
      ratioMarkers = mk(data.yellow_swings, YELLOW);

      if (params.order_blocks_on) {
        for (const b of data.green_blocks) {
          if (b.origin_index < 0 || b.origin_index >= n) continue;
          boxes.push({ seriesId: "pcr", from: timeAt(b.origin_index), top: b.top, bottom: b.bot, fill: OB_GREEN_FILL });
        }
        for (const b of data.yellow_blocks) {
          if (b.origin_index < 0 || b.origin_index >= n) continue;
          boxes.push({ seriesId: "ratio", from: timeAt(b.origin_index), top: b.top, bottom: b.bot, fill: OB_YELLOW_FILL });
        }
      }

      if (params.trend_rider_on && data.green_rider.length > 0) {
        const rider = data.engine_timestamps
          ? lastInBucket(engTs, data.green_rider.map((r) => r.val), 1)
          : lastInBucket(data.timestamps, data.green_rider.map((r) => r.val), iv);
        extra.push({
          id: "rider",
          label: "Rider trail",
          priceFormat: fmtLevel,
          color: RIDER,
          lineWidth: 1,
          lineStyle: LineStyle.Dashed,
          hideInTooltip: true,
          lastValueVisible: false,
          data: toPoints(rider.times, rider.values),
        });
      }
    }

    const series: LineSeriesSpec[] = [
      {
        id: "ratio",
        label: "Ratio (Call ÷ Put)",
        color: SERIES_COLORS.ratio,
        data: ratioData,
        priceFormat: fmtLevel,
        markers: ratioMarkers,
      },
      {
        id: "pcr",
        label: "PCR — inverse (Put ÷ Call)",
        color: SERIES_COLORS.pcr,
        data: pcrData,
        priceFormat: fmtLevel,
        markers: pcrMarkers,
      },
      ...extra,
    ];
    return { series, positions: meta, boxes };
  }, [data, iv, bucket, isFullDay, hasTotals, params.smc_on, params.order_blocks_on, params.trend_rider_on]);

  // Legend readout: hover value, else the LAST PLOTTED value.
  const lastVal = (id: "ratio" | "pcr") => {
    const d = series.find((s) => s.id === id)?.data;
    return d && d.length ? d[d.length - 1].value : null;
  };
  const shown = hover?.time != null ? hover.values : { ratio: lastVal("ratio"), pcr: lastVal("pcr") };

  // Refit on a new contract / replay scrub / pill change — never on the live poll.
  const fitKey = `${data.symbol}|${data.expiry}|${data.replay_at ?? ""}|${bucket}|${data.timestamps[0] ?? ""}`;
  const atmText = params.strikes_atm_window < 0 ? "all strikes" : `ATM ± ${params.strikes_atm_window}`;

  return (
    <div className="panel p-3">
      <div className="flex items-center justify-between gap-3 px-1 pb-2 text-sm flex-wrap">
        <div className="font-medium text-accent">
          Ratio &amp; PCR over time · {BUCKET_LABEL[bucket]}
          {isFullDay && <span className="text-muted"> (cumulative since 09:15)</span>} · {atmText}
          {data.replay_at && <span className="text-amber-300/90"> · replay {data.replay_at}</span>}
        </div>
        <div className="flex items-center gap-4 font-mono text-xs">
          <span style={{ color: SERIES_COLORS.ratio }}>Ratio {fmt4(shown.ratio)}</span>
          <span style={{ color: SERIES_COLORS.pcr }}>PCR {fmt4(shown.pcr)}</span>
          <button
            type="button"
            onClick={() => chartRef.current?.resetView()}
            title="Fit all data on both axes — use it after dragging or zooming the chart"
            data-testid="mqae-chart-fit"
            className="pill"
          >
            Fit
          </button>
          <div ref={setToolbarSlot} className="flex items-center" />
        </div>
      </div>
      <div className="flex items-center justify-between gap-2 px-1 pb-2 flex-wrap">
        <div className="flex items-center gap-1">
          {BUCKETS.map((b) => {
            const disabled = b === "full_day" && !hasTotals;
            return (
              <button
                key={b}
                type="button"
                disabled={disabled}
                title={disabled ? "Full Day needs OI totals from the backend (upgrade pending)" : undefined}
                onClick={() => onBucketChange(b)}
                className={`pill ${bucket === b ? "pill-active" : ""} ${disabled ? "opacity-40 cursor-not-allowed" : ""}`}
              >
                {BUCKET_LABEL[b]}
              </button>
            );
          })}
        </div>
        <span className="text-[10.5px] text-muted" data-testid="mqae-engine-timeframe">
          {`all five features calculated on ${
            (data.timeframe ?? "1m") === "full_day"
              ? "Full Day (change since 09:15, 1-min steps)"
              : `${data.timeframe ?? "1m"} candles`
          } · periods count these candles · Save to use it in live trading`}
        </span>
      </div>
      <div className="relative">
        <TimeSeriesChart
          ref={chartRef}
          series={series}
          height={420}
          onCrosshair={setHover}
          fitKey={fitKey}
          boxes={boxes}
          extraTooltipRows={(t) => {
            const pos = positions.get(t);
            if (!pos) return [];
            return [
              { label: isFullDay ? "Call positions (cum)" : "Call OI", value: compact(pos.call), color: SERIES_COLORS.call },
              { label: isFullDay ? "Put positions (cum)" : "Put OI", value: compact(pos.put), color: SERIES_COLORS.put },
            ];
          }}
        >
          <ChartDrawingOverlay
            describeAt={(px, py) => chartRef.current?.describeAt(px, py) ?? null}
            persistKey={`mqae-${persistKey}`}
            toolbarContainer={toolbarSlot}
            anchor={drawAnchor}
          />
        </TimeSeriesChart>
        {series[0].data.length === 0 && series[1].data.length === 0 && (
          <div className="absolute inset-0 flex items-center justify-center text-xs text-muted pointer-events-none">
            No ratio data for this window.
          </div>
        )}
      </div>
    </div>
  );
}

/** Velocity oscillator: two area series around a zero line, same x-domain as the main chart. */
export function MqaeVelocityChart({ data, bucket = "1m" }: { data: MqaeEvalResponse; bucket?: string }) {
  const iv = bucket === "full_day" ? 1 : Number.parseInt(bucket, 10) || 1;
  const series = useMemo<LineSeriesSpec[]>(() => {
    // Velocity is computed on the engine's own candles (engine_timestamps).
    const vts = data.engine_timestamps ?? data.timestamps;
    const viv = data.engine_timestamps ? 1 : iv;
    const g = lastInBucket(vts, data.green_velocity, viv);
    const y = lastInBucket(vts, data.yellow_velocity, viv);
    const fmt = (v: number) => v.toFixed(4);
    return [
      {
        id: "vel_green",
        label: "Green velocity",
        color: GREEN,
        kind: "area",
        lineWidth: 1,
        data: toPoints(g.times, g.values),
        priceFormat: fmt,
        priceLines: [{ id: "zero", price: 0, color: "rgba(255,255,255,0.35)", lineStyle: LineStyle.Dotted }],
      },
      {
        id: "vel_yellow",
        label: "Yellow velocity",
        color: YELLOW,
        kind: "area",
        lineWidth: 1,
        data: toPoints(y.times, y.values),
        priceFormat: fmt,
      },
    ];
  }, [data, iv]);
  const fitKey = `${data.symbol}|${data.expiry}|${data.replay_at ?? ""}|${iv}|${data.timestamps[0] ?? ""}`;
  return (
    <div className="panel p-3">
      <div className="flex items-center justify-between px-1 pb-2 text-sm">
        <div className="font-medium text-accent">Velocity Oscillator</div>
        <span className="text-[10.5px] text-muted">rate of change · Green (PCR) vs Yellow (Ratio)</span>
      </div>
      <TimeSeriesChart series={series} height={140} fitKey={fitKey} />
    </div>
  );
}
