import { useEffect, useMemo, useRef, useState } from "react";
import { AtmWindowSelect } from "../components/AtmWindowSelect";
import { DatePicker } from "../components/DatePicker";
import { ExpirySelect } from "../components/ExpirySelect";
import { ExportButton } from "../components/ExportButton";
import { KPIBar } from "../components/KPIBar";
import { OICandleChart } from "../components/OICandleChart";
import { OIChangeChart } from "../components/OIChangeChart";
import { ReplayController } from "../components/ReplayController";
import { SymbolSelect } from "../components/SymbolSelect";
import { TimeframeBar, TIMEFRAME_ITEMS } from "../components/TimeframeBar";
import {
  TimeSeriesChart,
  type LineSeriesSpec,
} from "../components/charts/TimeSeriesChart";
import { SERIES_COLORS } from "../components/charts/chartTheme";
import { istIsoToChartTime } from "../components/charts/chartTime";
import type { MarketContextValue } from "../hooks/useMarketContext";
import { useAvailableDates } from "../hooks/useAvailableDates";
import { useReplayFrames } from "../hooks/useReplayFrames";
import type {
  OIChangeResponse, OIChangeRow, OITimeseriesPoint, ReplayFrame, ReplayRow, Timeframe,
} from "../types";
import { buildReplayTable, buildSnapshotTable } from "../utils/exportData";
import { atmRound, effectiveAtmWindow } from "../utils/oiStrikeWindow";
import { compact, signedCompact } from "../utils/num";
import { changeColor, sideColor, sideLabel } from "../utils/ui";
import { callPutRatio } from "../utils/ratio";
import {
  isoForSessionMinuteOnDate, maxMinForDate, parseIstDateAndMinute, todayIstDate,
} from "../utils/sessionTime";

const STEPS = ["1m", "5m", "15m"] as const;
const ATM_MAX_WINDOW = 50;
/** Chart panels redraw at most this often during playback; numbers stay frame-exact. */
const CHART_THROTTLE_MS = 150;

// Lookback in minutes per timeframe preset. `full_day` is absent on purpose: it is
// the session-open baseline, which every frame already carries per strike as
// `*_oi_change`, so it needs no lookback at all.
const TF_MINUTES: Record<string, number> = {
  "1m": 1, "3m": 3, "5m": 5, "10m": 10, "15m": 15, "30m": 30,
  "1h": 60, "2h": 120, "3h": 180,
};

const fmtGreek = (v: number | null | undefined) => (v == null ? "—" : v.toFixed(3));
const safeRatio = (a: number, b: number): number | null => (b === 0 ? null : a / b);
const stepMinutes = (step: string): number => Number(step.replace("m", "")) || 1;

/**
 * Bar width in minutes for the time charts.
 *
 * `full_day` maps to the finest resolution available rather than one giant bar: the
 * charts are already anchored on the session open (`toChangeSinceOpen` baselines on
 * `points[0]`), so the finest series IS "cumulative across the whole day".
 *
 * Never finer than the replay Step — asking for 1-minute bars out of a 15-minute
 * frame set would draw one sample per bar and call it a candle.
 */
function barMinutes(tf: Timeframe, step: string): number {
  const want = tf === "full_day" ? 1 : TF_MINUTES[tf] ?? 1;
  return Math.max(want, stepMinutes(step));
}

/**
 * `value`, but applied at most every `ms` — always settling on the latest value.
 *
 * Only the CHART panels read this; the KPI bar, the grid, the levels strip and the chain
 * table stay frame-exact. Four charts are mounted, and each ECharts update rebuilds the
 * whole option object — for the two OI-change charts over a series that grows by a point
 * every frame, with a 200ms animation on top. At 15x that is up to 15 full rebuilds a
 * second, so they are capped at ~7 instead.
 *
 * This is precautionary, not a measured fix: a chart trailing the playhead by <=150ms is
 * invisible, while a stale number would not be, so the cheap side to err on is obvious.
 * If profiling later shows the redraws were never a problem, deleting this hook and
 * passing `index` straight through is safe — nothing else depends on the delay.
 */
function useThrottled<T>(value: T, ms: number): T {
  const [shown, setShown] = useState(value);
  const appliedAt = useRef(0);
  useEffect(() => {
    const since = performance.now() - appliedAt.current;
    if (since >= ms) {
      appliedAt.current = performance.now();
      setShown(value);
      return;
    }
    // A newer value cancels this and reschedules, so the last one always lands.
    const t = window.setTimeout(() => {
      appliedAt.current = performance.now();
      setShown(value);
    }, ms - since);
    return () => window.clearTimeout(t);
  }, [value, ms]);
  return shown;
}

/** Last index in the ascending `ms` array whose value is <= `target`, searching at or
 *  below `hi`. Returns -1 when every candidate is later than `target`. */
function lastIndexAtOrBefore(ms: number[], target: number, hi: number): number {
  let lo = 0;
  let best = -1;
  let end = hi;
  while (lo <= end) {
    const mid = (lo + end) >> 1;
    if (ms[mid] <= target) { best = mid; lo = mid + 1; } else { end = mid - 1; }
  }
  return best;
}

interface GridRow {
  tf: Timeframe;
  label: string;
  callChange: number;
  putChange: number;
}

/** Strikes within the playhead's ATM window. Centred on the PLAYHEAD's spot, never the
 *  session close, so nothing on this page is computed from data the playhead hasn't
 *  reached yet. Falls back to the frame's own `atm` (backend supplies a
 *  straddle-minimum ATM for spot-less backfilled days) — without it, spot-less
 *  frames silently disabled the ATM±N filter and showed every strike. */
function windowFilter(
  current: ReplayFrame | undefined,
  atmWindow: number,
  strikeStep: number,
): (strike: number) => boolean {
  const w = effectiveAtmWindow(atmWindow);
  const atm =
    current?.spot != null ? atmRound(current.spot, strikeStep) : current?.atm ?? null;
  return (strike: number) => w < 0 || atm == null || Math.abs(strike - atm) <= w * strikeStep;
}

/**
 * Per-strike OI change over the trailing `tf`, as of the playhead, inside the ATM window.
 *
 * THE one place the replay page turns frames into a change. The multi-timeframe grid,
 * the KPI bar, the by-strike chart and the chain table all read this, so they cannot
 * disagree with each other.
 *
 * Computed from the frames the page has ALREADY downloaded — `/api/replay` ships the
 * complete per-strike book at every bucket, so every number here is subtraction, not a
 * request. The grid used to be a `/api/multi-timeframe?as_of=` call fired every 350ms;
 * each unique `as_of` is a cache miss costing ~3.4s server-side, so the requests queued
 * behind one another, landed out of order, and pinned the grid to whatever stale instant
 * happened to answer last.
 *
 * The arithmetic deliberately mirrors `OIChangeEngine._compute`:
 *   - baseline = the last frame at or before `playhead - tf`;
 *   - a strike with no baseline frame (it entered the window later via ATM drift) falls
 *     back to its session-open delta, exactly as the engine falls back to its
 *     earliest-today snapshot for strikes missing from the `then` map;
 *   - a lookback reaching past the session open collapses to the session-open baseline,
 *     which is what the engine's session floor does — and is what `full_day` always is.
 *
 * One deliberate difference: the engine picks its baseline off raw ticks
 * (`ts <= cutoff`), this picks a whole frame. When a stored snapshot lands exactly on
 * the cutoff second the engine includes it and we do not, so that row carries one Step
 * of extra lookback. Do NOT "fix" that by taking the next frame instead — a frame
 * labelled T holds ticks in (T - step, T], so the next one folds in ticks from AFTER the
 * lookback instant, which is the look-ahead the backend just stopped doing. Measured on
 * a real session at Step 1m: 30 of 40 sampled rows matched the engine to the lot, and
 * the misses were this boundary case.
 */
function windowedRows(
  frames: ReplayFrame[],
  frameMs: number[],
  index: number,
  tf: Timeframe,
  atmWindow: number,
  strikeStep: number,
): OIChangeRow[] {
  const current = frames[index];
  if (!current) return [];
  const inWindow = windowFilter(current, atmWindow, strikeStep);

  const mins = TF_MINUTES[tf];
  // `full_day` (mins undefined) and any lookback that predates the open both use the
  // backend's session-open baseline, already carried per strike as `*_oi_change`.
  const j = mins == null
    ? -1
    : lastIndexAtOrBefore(frameMs, frameMs[index] - mins * 60_000, index);
  const base = j < 0 ? null : new Map(frames[j].rows.map((r) => [r.strike, r]));

  const out: OIChangeRow[] = [];
  for (const r of current.rows) {
    if (!inWindow(r.strike)) continue;
    const b = base?.get(r.strike);
    out.push({
      strike: r.strike,
      call_oi: r.call_oi,
      put_oi: r.put_oi,
      call_oi_change: b ? r.call_oi - b.call_oi : r.call_oi_change,
      put_oi_change: b ? r.put_oi - b.put_oi : r.put_oi_change,
      // Replay frames carry no LTP; every consumer of these treats null as "unknown".
      call_ltp: null, put_ltp: null, call_ltp_change: null, put_ltp_change: null,
    });
  }
  return out;
}

/** The multi-timeframe grid: `windowedRows` summed, once per preset. */
function buildGridRows(
  frames: ReplayFrame[],
  frameMs: number[],
  index: number,
  atmWindow: number,
  strikeStep: number,
): GridRow[] {
  if (!frames[index]) return [];
  return TIMEFRAME_ITEMS.map(({ tf, label }) => {
    let callChange = 0;
    let putChange = 0;
    for (const r of windowedRows(frames, frameMs, index, tf, atmWindow, strikeStep)) {
      callChange += r.call_oi_change;
      putChange += r.put_oi_change;
    }
    return { tf, label, callChange, putChange };
  });
}

/**
 * Total Call/Put OI at every frame up to the playhead, over the playhead's ATM window —
 * the shape `/api/oi-timeseries` returns, so the Charts tab's components render it
 * unchanged. Levels, not changes: `toChangeSinceOpen` does that conversion itself and
 * anchors on the first point, which is the session open.
 */
function seriesPoints(
  frames: ReplayFrame[],
  index: number,
  atmWindow: number,
  strikeStep: number,
): OITimeseriesPoint[] {
  const current = frames[index];
  if (!current) return [];
  const inWindow = windowFilter(current, atmWindow, strikeStep);

  const out: OITimeseriesPoint[] = [];
  for (let k = 0; k <= index; k++) {
    let ce = 0;
    let pe = 0;
    for (const r of frames[k].rows) {
      if (!inWindow(r.strike)) continue;
      ce += r.call_oi;
      pe += r.put_oi;
    }
    out.push({
      ts: frames[k].ts,
      total_call_oi: ce,
      total_put_oi: pe,
      ratio: safeRatio(ce, pe),
      pcr: safeRatio(pe, ce),
    });
  }
  return out;
}

/**
 * Ratio + PCR lines at `barMin` resolution, mirroring the Ratio tab.
 *
 * `cumulative` (the Full Day pill) plots the ratio of the CHANGES since the session
 * open; every other width plots the ratio of the LEVELS at each bar's close. That split
 * is the Ratio tab's own behaviour (`RatioChartPage.tsx`), kept so the two read alike.
 * A bar's value is its LAST sample, matching the backend's `last(oi, ts)` per bucket.
 */
function ratioLines(points: OITimeseriesPoint[], barMin: number, cumulative: boolean): LineSeriesSpec[] {
  const ratioData: LineSeriesSpec["data"] = [];
  const pcrData: LineSeriesSpec["data"] = [];

  if (cumulative) {
    const baseCall = points[0]?.total_call_oi ?? 0;
    const basePut = points[0]?.total_put_oi ?? 0;
    for (const p of points) {
      const t = istIsoToChartTime(p.ts);
      const cumCall = p.total_call_oi - baseCall;
      const cumPut = p.total_put_oi - basePut;
      if (cumPut !== 0) ratioData.push({ time: t, value: cumCall / cumPut });
      if (cumCall !== 0) pcrData.push({ time: t, value: cumPut / cumCall });
    }
  } else {
    // Keep the last sample of each bar; the map preserves insertion order, and points
    // are already chronological, so overwriting yields the bar's close.
    const byBar = new Map<number, OITimeseriesPoint>();
    for (const p of points) {
      const sinceOpen = parseIstDateAndMinute(p.ts)?.sinceOpen ?? 0;
      byBar.set(Math.floor(sinceOpen / barMin), p);
    }
    for (const p of byBar.values()) {
      const t = istIsoToChartTime(p.ts);
      if (p.ratio != null) ratioData.push({ time: t, value: p.ratio });
      if (p.pcr != null) pcrData.push({ time: t, value: p.pcr });
    }
  }

  return [
    { id: "ratio", label: cumulative ? "Ratio (cum Call ÷ cum Put)" : "Ratio (Call ÷ Put)", color: SERIES_COLORS.ratio, data: ratioData, priceFormat: (v) => v.toFixed(2) },
    { id: "pcr", label: cumulative ? "PCR (cum Put ÷ cum Call)" : "PCR (Put ÷ Call)", color: SERIES_COLORS.pcr, data: pcrData, priceFormat: (v) => v.toFixed(2) },
  ];
}

export function ReplayPage({ mc }: { mc: MarketContextValue }) {
  const {
    dataReady, health, symbol, symbolGroups, switching, symbolError, handleSymbolChange,
    expiry, setExpiry, expiries, expiryError, fnoEligible, symbolDisplay, activeEntry,
    atmWindow, setAtmWindow,
  } = mc;
  const strikeStep = activeEntry?.strike_step ?? 50;

  const [selectedDate, setSelectedDate] = useState<string | null>(null);
  const [step, setStep] = useState<string>("1m");
  const [index, setIndex] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(2);
  // Drives EVERY panel on the page. Two meanings, each the one its source tab already
  // uses: a lookback for the change panels (KPI, by-strike, grid, chain) and a bar width
  // for the time charts (Call/Put OI, Ratio) — so nothing here reads differently from
  // the tab it came from.
  const [timeframe, setTimeframe] = useState<Timeframe>("5m");

  const avail = useAvailableDates(
    fnoEligible ? symbol : null,
    expiry,
    dataReady && fnoEligible && !!expiry,
  );
  const date = selectedDate ?? avail.latest ?? todayIstDate(health);

  const startTs = date ? isoForSessionMinuteOnDate(date, 0) : null;
  const endTs = date ? isoForSessionMinuteOnDate(date, maxMinForDate(date, health)) : null;

  const { frames, loading, error } = useReplayFrames({
    symbol: fnoEligible ? symbol : null,
    expiry,
    startTs,
    endTs,
    step,
    enabled: dataReady && fnoEligible && !!expiry && !!date,
    withGreeks: true,
  });

  // Frame timestamps as epoch ms, once per frame set — the grid's baseline lookup is
  // a binary search over these rather than string comparisons on every playhead move.
  const frameMs = useMemo(() => frames.map((f) => Date.parse(f.ts)), [frames]);

  // A Step change re-fetches at a different resolution; keep the playhead on the same
  // INSTANT across it, so the same moment can be compared at 1m and 15m. Set only by
  // the Step handler and consumed once — a symbol/date/expiry change is a different
  // session and correctly rewinds to the open.
  const keepPlayheadMs = useRef<number | null>(null);
  useEffect(() => {
    setPlaying(false);
    const want = keepPlayheadMs.current;
    keepPlayheadMs.current = null;
    if (want == null || frameMs.length === 0) {
      setIndex(0);
      return;
    }
    const j = lastIndexAtOrBefore(frameMs, want, frameMs.length - 1);
    setIndex(j < 0 ? 0 : j);
  }, [frameMs]);

  // Single animation clock: advance the playhead while playing at 1000ms / speed
  // per frame; auto-pause at the end. rAF + accumulator keeps it smooth.
  useEffect(() => {
    if (!playing || frames.length === 0) return;
    let raf = 0;
    let last = performance.now();
    let acc = 0;
    const frameMs = 1000 / speed;
    const tick = (now: number) => {
      acc += now - last;
      last = now;
      if (acc >= frameMs) {
        const steps = Math.floor(acc / frameMs);
        acc -= steps * frameMs;
        setIndex((i) => {
          const ni = Math.min(i + steps, frames.length - 1);
          if (ni >= frames.length - 1) setPlaying(false);
          return ni;
        });
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [playing, speed, frames.length]);

  const current = frames[index] ?? null;

  // ATM-strike greeks at the playhead (present only once the populator has run).
  const atmGreeks = useMemo(() => {
    if (!current || current.atm == null) return null;
    const row = current.rows.find((r) => r.strike === current.atm)
      ?? current.rows.reduce<ReplayRow | null>((best, r) =>
        best == null || Math.abs(r.strike - (current.atm as number)) < Math.abs(best.strike - (current.atm as number)) ? r : best, null);
    if (!row) return null;
    const has = row.call_iv != null || row.call_delta != null;
    return has ? row : null;
  }, [current]);

  // Multi-timeframe grid at the playhead, derived from the frames already in memory —
  // no network, so it tracks the playhead exactly even at 15x. See `buildGridRows`.
  const gridRows = useMemo(
    () => buildGridRows(frames, frameMs, index, atmWindow, strikeStep),
    [frames, frameMs, index, atmWindow, strikeStep],
  );
  const gridSpot = current?.spot ?? null;
  const gridAtm = current?.atm ?? null;
  const tfLabel = TIMEFRAME_ITEMS.find((i) => i.tf === timeframe)?.label ?? timeframe;

  // Per-strike rows at the SELECTED timeframe — the by-strike chart, the KPI bar and the
  // chain table all read this, so all three agree with the grid row for the same preset.
  const tfRows = useMemo(
    () => windowedRows(frames, frameMs, index, timeframe, atmWindow, strikeStep),
    [frames, frameMs, index, timeframe, atmWindow, strikeStep],
  );

  // Synthetic OIChangeResponse — exactly what KPIBar and OIChangeChart consume, so both
  // Dashboard components render the replay playhead with no changes of their own.
  const frameData: OIChangeResponse | null = useMemo(() => {
    if (!current) return null;
    let totalCall = 0;
    let totalPut = 0;
    for (const r of tfRows) { totalCall += r.call_oi_change; totalPut += r.put_oi_change; }
    return {
      timeframe,
      expiry: expiry ?? "",
      spot: current.spot,
      asof: current.ts,
      total_call_oi_change: totalCall,
      total_put_oi_change: totalPut,
      rows: tfRows,
    };
  }, [current, expiry, timeframe, tfRows]);

  // Level totals up to the playhead, in the shape the Charts tab's components expect.
  // Throttled: this is the only series that GROWS with the playhead, so it is what made
  // playback expensive. See `useThrottled`.
  const chartIndex = useThrottled(index, CHART_THROTTLE_MS);
  const points = useMemo(
    () => seriesPoints(frames, chartIndex, atmWindow, strikeStep),
    [frames, chartIndex, atmWindow, strikeStep],
  );
  // The by-strike chart is ECharts too, but its data is a fixed ~25 rows; it rides the
  // same throttle so all four charts advance together rather than visibly out of step.
  const chartFrameData: OIChangeResponse | null = useMemo(() => {
    const f = frames[chartIndex];
    if (!f) return null;
    const rows = windowedRows(frames, frameMs, chartIndex, timeframe, atmWindow, strikeStep);
    let totalCall = 0;
    let totalPut = 0;
    for (const r of rows) { totalCall += r.call_oi_change; totalPut += r.put_oi_change; }
    return {
      timeframe, expiry: expiry ?? "", spot: f.spot, asof: f.ts,
      total_call_oi_change: totalCall, total_put_oi_change: totalPut, rows,
    };
  }, [frames, frameMs, chartIndex, timeframe, atmWindow, strikeStep, expiry]);
  const barMin = barMinutes(timeframe, step);
  const ratioSeries: LineSeriesSpec[] = useMemo(
    () => ratioLines(points, barMin, timeframe === "full_day"),
    [points, barMin, timeframe],
  );

  // Point-in-time levels at the playhead (the Multi-TF tab's strip). Sums cover the same
  // ATM window as everything else, so Ratio here matches the grid's level view.
  const levels = useMemo(() => {
    let ce = 0;
    let pe = 0;
    for (const r of tfRows) { ce += r.call_oi; pe += r.put_oi; }
    return { ce, pe, ratio: safeRatio(ce, pe), pcr: safeRatio(pe, ce) };
  }, [tfRows]);

  const onPlayPause = () => {
    if (!playing && index >= frames.length - 1) setIndex(0);
    setPlaying((p) => !p);
  };

  return (
    <div className="min-h-screen w-full max-w-[1500px] mx-auto px-4 md:px-6 py-3">
      {/* ONE pinned control bar: filters, timeframe lens and transport together, so the
          replay can be paused, scrubbed and re-scoped from anywhere on the page.
          Sticky works here because nothing between this and the viewport sets
          `overflow` (index.css only gives html/body/#root a height) — do not wrap this
          in an overflow-* container. The wrapper is opaque on purpose: `.panel` is
          bg-panel/80 + backdrop-blur, and charts would scroll through it. */}
      <div className="sticky top-0 z-30 -mx-4 md:-mx-6 -mt-3 px-4 md:px-6 pt-3 pb-3 bg-bg">
        <div className="panel px-4 py-3 flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-3">
            <SymbolSelect
              groups={symbolGroups}
              value={symbol}
              onChange={(s) => void handleSymbolChange(s)}
              switching={switching}
              error={symbolError}
            />
            {fnoEligible && (
              <>
                <DatePicker value={selectedDate} available={avail.dates} onChange={setSelectedDate} />
                <ExpirySelect expiries={expiries} value={expiry} onChange={setExpiry} error={expiryError} />
                <div className="flex items-center gap-1">
                  <span className="text-xs text-muted" title="How finely playback advances through the session.">Step</span>
                  {STEPS.map((s) => (
                    <button
                      key={s}
                      type="button"
                      className={`pill ${step === s ? "pill-active" : ""}`}
                      onClick={() => {
                        if (s === step) return;
                        keepPlayheadMs.current = frameMs[index] ?? null;
                        setStep(s);
                      }}
                    >
                      {s}
                    </button>
                  ))}
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-xs text-muted">ATM ±</span>
                  <AtmWindowSelect value={atmWindow} max={ATM_MAX_WINDOW} onChange={setAtmWindow} />
                </div>
                <div className="ml-auto">
                  {/* Filename date comes from the LOADED FRAMES, never the picker:
                      exporting while a new date was still loading used to save the
                      previous session under the new date's name — the source of 27
                      mislabeled files in the 2026-08-11 data-quality report. */}
                  <ExportButton
                    getTable={() => (frames.length ? buildReplayTable(frames) : null)}
                    filenameBase={`replay_${symbol}_${frames[0]?.ts.slice(0, 10) ?? "no-data"}_${step}`}
                    disabled={loading || frames.length === 0}
                  />
                </div>
              </>
            )}
          </div>

          {fnoEligible && (
            <>
              <div className="flex flex-wrap items-center gap-2">
                <span
                  className="text-xs text-muted"
                  title="Drives every panel below: the lookback for the change panels, the bar width for the time charts."
                >
                  Timeframe
                </span>
                <TimeframeBar value={timeframe} onChange={setTimeframe} />
              </div>

              <ReplayController
                playing={playing}
                index={index}
                total={frames.length}
                speed={speed}
                currentTs={current?.ts ?? null}
                onPlayPause={onPlayPause}
                onRestart={() => { setIndex(0); setPlaying(false); }}
                onSeek={(i) => { setIndex(i); }}
                onSkip={(d) => setIndex((i) => Math.max(0, Math.min(frames.length - 1, i + d)))}
                onSpeed={setSpeed}
              />
            </>
          )}
        </div>
      </div>

      {!fnoEligible ? (
        <div className="panel p-6 text-sm text-muted">No F&amp;O contracts for {symbolDisplay}.</div>
      ) : (
        <div className="flex flex-col gap-4">
          {error && <div className="panel p-4 text-sm text-red-400">Failed to load replay: {error}</div>}
          {!error && frames.length === 0 && (
            <div className="panel p-6 text-sm text-muted text-center">
              {loading ? "Loading replay frames…" : "No stored data for this session/window."}
            </div>
          )}

          {frames.length > 0 && (
            <>
              {/* Point-in-time levels at the playhead (the Multi-TF tab's strip). */}
              <div className="panel px-4 py-3 flex flex-wrap gap-x-10 gap-y-3">
                <Level label="Spot" value={gridSpot != null ? gridSpot.toFixed(2) : "—"} />
                <Level label="ATM" value={gridAtm != null ? String(gridAtm) : "—"} />
                <Level label="Ratio (C÷P)" value={levels.ratio != null ? levels.ratio.toFixed(2) : "—"} />
                <Level label="PCR (P÷C)" value={levels.pcr != null ? levels.pcr.toFixed(2) : "—"} />
                <Level label="Total Call OI" value={compact(levels.ce)} />
                <Level label="Total Put OI" value={compact(levels.pe)} />
                <div className="ml-auto self-center text-[10px] text-muted">
                  levels as of {current?.ts.slice(11, 19) ?? "—"} · {atmWindow < 0 ? "full chain" : `ATM ± ${effectiveAtmWindow(atmWindow)}`}
                </div>
              </div>

              <KPIBar data={frameData} mode="change" atmWindow={atmWindow} liveSpot={current?.spot ?? null} strikeStep={strikeStep} />
              <OIChangeChart
                data={chartFrameData}
                mode="change"
                atmWindow={atmWindow}
                underlyingLabel={symbolDisplay}
                liveSpot={current?.spot ?? null}
                strikeStep={strikeStep}
              />

              {/* The Charts tab's two panels, drawn up to the playhead. `points` are
                  levels; OICandleChart converts to change-since-open itself. */}
              <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
                <OICandleChart points={points} side="call" interval={barMin} title={`Call OI Change · ${tfLabel}`} isLoading={loading && points.length === 0} />
                <OICandleChart points={points} side="put" interval={barMin} title={`Put OI Change · ${tfLabel}`} isLoading={loading && points.length === 0} />
              </div>

              <div className="panel p-3">
                <div className="px-1 pb-2 text-sm font-medium text-accent">
                  Ratio &amp; PCR through playhead · {tfLabel}
                  {timeframe === "full_day" && <span className="text-muted font-normal"> (cumulative since open)</span>}
                </div>
                <TimeSeriesChart series={ratioSeries} height={260} />
                <p className="px-3 pt-2 text-[10px] text-muted">
                  {timeframe === "full_day"
                    ? "Ratio of the OI CHANGES accumulated since the session open, matching the Ratio tab's Full Day mode."
                    : `Ratio of the OI LEVELS at each ${barMin}m bar's close, matching the Ratio tab's bucket mode.`}
                  {barMin !== (TF_MINUTES[timeframe] ?? barMin) &&
                    ` Bars are ${barMin}m rather than ${TF_MINUTES[timeframe]}m — the replay Step is the finest resolution available.`}
                </p>
              </div>

              {/* Multi-timeframe grid — derived from the frames in memory, so it tracks
                  the playhead frame-for-frame with no requests at all. */}
              <div className="panel p-3 overflow-x-auto">
                <div className="px-1 pb-2 text-sm font-medium text-accent">Multi-timeframe OI change @ playhead</div>
                <table className="w-full text-sm font-mono">
                  <thead>
                    <tr className="text-xs text-muted border-b border-border">
                      <th className="text-left py-1.5 px-3">Timeframe</th>
                      <th className="text-right py-1.5 px-3">Call OI Δ</th>
                      <th className="text-right py-1.5 px-3">Put OI Δ</th>
                      <th className="text-right py-1.5 px-3">Ratio</th>
                      <th className="text-right py-1.5 px-3">Side</th>
                      <th className="text-right py-1.5 px-3">Spot</th>
                      <th className="text-right py-1.5 px-3">ATM</th>
                    </tr>
                  </thead>
                  <tbody>
                    {gridRows.length === 0 ? (
                      <tr>
                        <td colSpan={7} className="py-4 px-3 text-center text-xs text-muted">
                          {loading ? "Loading replay frames…" : "No book at the playhead yet."}
                        </td>
                      </tr>
                    ) : gridRows.map((r) => {
                      const rr = callPutRatio(r.callChange, r.putChange);
                      const lit = r.tf === timeframe;
                      return (
                      <tr
                        key={r.tf}
                        className={`border-b border-border/40 ${lit ? "bg-accent/10" : ""}`}
                      >
                        <td className={`text-left py-1.5 px-3 border-l-2 ${lit ? "border-accent text-white font-semibold" : "border-transparent text-foreground"}`}>
                          {r.label}
                        </td>
                        <td className={`text-right py-1.5 px-3 ${changeColor(r.callChange)}`}>{signedCompact(r.callChange)}</td>
                        <td className={`text-right py-1.5 px-3 ${changeColor(r.putChange)}`}>{signedCompact(r.putChange)}</td>
                        <td className="text-right py-1.5 px-3 text-foreground tabular-nums">{rr.text}</td>
                        <td className={`text-right py-1.5 px-3 font-semibold ${sideColor(rr.side)}`}>{sideLabel(rr.side)}</td>
                        <td className="text-right py-1.5 px-3 text-muted">{gridSpot != null ? gridSpot.toFixed(1) : "—"}</td>
                        <td className="text-right py-1.5 px-3 text-muted">{gridAtm ?? "—"}</td>
                      </tr>
                      );
                    })}
                  </tbody>
                </table>
                <p className="px-3 pt-2 text-[10px] text-muted">
                  Ratio (normalized Call : Put) and Side (smaller-signed OI-Δ side) per timeframe, as of the
                  playhead instant. Each row is the book at the playhead minus the book one timeframe earlier,
                  on the same session-open baseline the OI Change and Multi-TF tabs use. Sums cover{" "}
                  {atmWindow < 0 ? "the full chain" : `strikes ATM ± ${effectiveAtmWindow(atmWindow)}`}.
                  A lookback reaching past 09:15 is clamped to the open, which is why the longest timeframes
                  agree with Full Day early in a session.
                </p>
                <p className="px-3 pt-1 text-[10px] text-muted">
                  Resolved at the replay&apos;s own Step, so a baseline lands on a whole {step} bucket rather than
                  the exact second. Against the Multi-TF tab at Step 1m that is an exact match except where a
                  stored snapshot fell on the boundary second, which shifts one row by one minute of data. The
                  earlier bucket is used deliberately — the later one would fold in ticks from after the
                  lookback instant.
                </p>
              </div>

              {/* The numbers behind the by-strike chart, row by row. Same `tfRows` the
                  chart and KPI bar read, so the three can never disagree. */}
              <div className="panel p-3 overflow-x-auto">
                <div className="px-1 pb-2 flex items-center gap-3">
                  <span className="text-sm font-medium text-accent">Option chain @ playhead · {tfLabel}</span>
                  <div className="ml-auto">
                    {/* Date from the playhead frame itself + the ATM-window setting in
                        the name: exports made with different windows have wildly
                        different totals and must be distinguishable at a glance. */}
                    <ExportButton
                      getTable={() => (frameData ? buildSnapshotTable(frameData) : null)}
                      filenameBase={`chain_${symbol}_${current?.ts.slice(0, 10) ?? "no-data"}_${timeframe}_${current?.ts.slice(11, 16).replace(":", "") ?? ""}_w${atmWindow < 0 ? "All" : atmWindow}`}
                      disabled={loading || !frameData || tfRows.length === 0}
                    />
                  </div>
                </div>
                <table className="w-full text-sm font-mono">
                  <thead>
                    <tr className="text-xs text-muted border-b border-border">
                      <th className="text-right py-1.5 px-3">Call OI</th>
                      <th className="text-right py-1.5 px-3">Call OI Δ</th>
                      <th className="text-center py-1.5 px-3">Strike</th>
                      <th className="text-right py-1.5 px-3">Put OI Δ</th>
                      <th className="text-right py-1.5 px-3">Put OI</th>
                    </tr>
                  </thead>
                  <tbody>
                    {tfRows.map((r) => {
                      const isAtm = gridAtm != null && r.strike === gridAtm;
                      return (
                        <tr key={r.strike} className={`border-b border-border/40 ${isAtm ? "bg-accent/10" : ""}`}>
                          <td className="text-right py-1.5 px-3 text-muted">{compact(r.call_oi)}</td>
                          <td className={`text-right py-1.5 px-3 ${changeColor(r.call_oi_change)}`}>{signedCompact(r.call_oi_change)}</td>
                          <td className={`text-center py-1.5 px-3 ${isAtm ? "text-white font-semibold" : "text-foreground"}`}>
                            {r.strike}{isAtm ? " ·ATM" : ""}
                          </td>
                          <td className={`text-right py-1.5 px-3 ${changeColor(r.put_oi_change)}`}>{signedCompact(r.put_oi_change)}</td>
                          <td className="text-right py-1.5 px-3 text-muted">{compact(r.put_oi)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
                <p className="px-3 pt-2 text-[10px] text-muted">
                  OI levels at the playhead; Δ measured over the trailing {tfLabel}
                  {timeframe === "full_day" ? "" : " (clamped to the session open when the lookback predates 09:15)"}.
                </p>
              </div>

              {/* ATM greeks at the playhead (from greeks_snapshots via ?with_greeks) */}
              <div className="panel p-3">
                <div className="px-1 pb-2 text-sm font-medium text-accent">
                  ATM greeks @ playhead {current?.atm != null ? `· ${current.atm}` : ""}
                </div>
                {atmGreeks ? (
                  <div className="flex flex-wrap gap-x-8 gap-y-2 text-sm font-mono">
                    <Greek label="Call IV" value={atmGreeks.call_iv != null ? `${(atmGreeks.call_iv * 100).toFixed(1)}%` : "—"} />
                    <Greek label="Put IV" value={atmGreeks.put_iv != null ? `${(atmGreeks.put_iv * 100).toFixed(1)}%` : "—"} />
                    <Greek label="Call Δ" value={fmtGreek(atmGreeks.call_delta)} />
                    <Greek label="Put Δ" value={fmtGreek(atmGreeks.put_delta)} />
                    <Greek label="Gamma" value={fmtGreek(atmGreeks.call_gamma)} />
                    <Greek label="Call Θ" value={fmtGreek(atmGreeks.call_theta)} />
                    <Greek label="Put Θ" value={fmtGreek(atmGreeks.put_theta)} />
                    <Greek label="Vega" value={fmtGreek(atmGreeks.call_vega)} />
                  </div>
                ) : (
                  <p className="px-1 text-xs text-muted">
                    No stored greeks for this session yet — greeks are persisted going forward, so
                    replays of sessions recorded after this deploy will show them here.
                  </p>
                )}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function Greek({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col">
      <span className="text-[10px] uppercase tracking-wide text-muted">{label}</span>
      <span className="text-foreground">{value}</span>
    </div>
  );
}

function Level({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col">
      <span className="text-[10px] uppercase tracking-wide text-muted">{label}</span>
      <span className="text-lg font-mono text-foreground">{value}</span>
    </div>
  );
}
