import { useEffect, useMemo, useState } from "react";
import { AtmWindowSelect } from "../components/AtmWindowSelect";
import { DatePicker } from "../components/DatePicker";
import { ExpirySelect } from "../components/ExpirySelect";
import { ExportButton } from "../components/ExportButton";
import { KPIBar } from "../components/KPIBar";
import { OIChangeChart } from "../components/OIChangeChart";
import { ReplayController } from "../components/ReplayController";
import { SymbolSelect } from "../components/SymbolSelect";
import {
  TimeSeriesChart,
  type LineSeriesSpec,
} from "../components/charts/TimeSeriesChart";
import { SERIES_COLORS } from "../components/charts/chartTheme";
import { istIsoToChartTime } from "../components/charts/chartTime";
import type { MarketContextValue } from "../hooks/useMarketContext";
import { useAvailableDates } from "../hooks/useAvailableDates";
import { useReplayFrames } from "../hooks/useReplayFrames";
import type { OIChangeResponse, ReplayFrame, ReplayRow } from "../types";
import { buildReplayTable } from "../utils/exportData";
import { effectiveAtmWindow, filterOiRowsByAtmWindow } from "../utils/oiStrikeWindow";
import { signedCompact } from "../utils/num";
import { callPutRatio, type DominantSide } from "../utils/ratio";
import { isoForSessionMinuteOnDate, maxMinForDate, todayIstDate } from "../utils/sessionTime";

const STEPS = ["1m", "5m", "15m"] as const;
const ATM_MAX_WINDOW = 50;
const STEP_MIN: Record<string, number> = { "1m": 1, "5m": 5, "15m": 15 };

// Timeframes for the synced multi-TF grid (minutes; null = full-day / first frame).
const MTF: { tf: string; label: string; mins: number | null }[] = [
  { tf: "1m", label: "1 Min", mins: 1 }, { tf: "3m", label: "3 Min", mins: 3 },
  { tf: "5m", label: "5 Min", mins: 5 }, { tf: "10m", label: "10 Min", mins: 10 },
  { tf: "15m", label: "15 Min", mins: 15 }, { tf: "30m", label: "30 Min", mins: 30 },
  { tf: "1h", label: "1 Hour", mins: 60 }, { tf: "2h", label: "2 Hour", mins: 120 },
  { tf: "3h", label: "3 Hour", mins: 180 }, { tf: "full_day", label: "Full Day", mins: null },
];

const fmtGreek = (v: number | null | undefined) => (v == null ? "—" : v.toFixed(3));
const changeColor = (v: number) => (v > 0 ? "text-emerald-400" : v < 0 ? "text-red-400" : "text-muted");
const sideColor = (s: DominantSide) =>
  s === "CALL" ? "text-emerald-400" : s === "PUT" ? "text-red-400" : "text-muted";
const sideLabel = (s: DominantSide) => (s === "CALL" ? "Call" : s === "PUT" ? "Put" : "Neutral");

/** Sum call/put OI over the ATM window for a frame's rows. */
function windowTotals(rows: ReplayRow[], spot: number | null, atmWindow: number, strikeStep: number) {
  const win = filterOiRowsByAtmWindow(rows, spot, atmWindow, strikeStep);
  let call = 0;
  let put = 0;
  for (const r of win) { call += r.call_oi; put += r.put_oi; }
  return { call, put };
}

/** Multi-timeframe OI change at the playhead, computed client-side from the frames. */
function computeMtf(
  frames: ReplayFrame[], index: number, stepMin: number, atmWindow: number, strikeStep: number,
): { tf: string; label: string; call: number; put: number }[] {
  const now = frames[index];
  if (!now) return [];
  const nowT = windowTotals(now.rows, now.spot, atmWindow, strikeStep);
  return MTF.map(({ tf, label, mins }) => {
    const back = mins == null ? index : Math.round(mins / stepMin);
    const thenFrame = frames[Math.max(0, index - back)];
    const thenT = windowTotals(thenFrame.rows, thenFrame.spot ?? now.spot, atmWindow, strikeStep);
    return { tf, label, call: nowT.call - thenT.call, put: nowT.put - thenT.put };
  });
}

export function ReplayPage({ mc }: { mc: MarketContextValue }) {
  const {
    authenticated, health, symbol, symbolGroups, switching, symbolError, handleSymbolChange,
    expiry, setExpiry, expiries, expiryError, fnoEligible, symbolDisplay, activeEntry,
    atmWindow, setAtmWindow,
  } = mc;
  const strikeStep = activeEntry?.strike_step ?? 50;

  const [selectedDate, setSelectedDate] = useState<string | null>(null);
  const [step, setStep] = useState<string>("1m");
  const [index, setIndex] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(2);

  const avail = useAvailableDates(
    fnoEligible ? symbol : null,
    expiry,
    authenticated && fnoEligible && !!expiry,
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
    enabled: authenticated && fnoEligible && !!expiry && !!date,
    withGreeks: true,
  });

  // Reset the playhead whenever a new frame set loads.
  useEffect(() => {
    setIndex(0);
    setPlaying(false);
  }, [frames]);

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

  // Synced multi-timeframe grid at the playhead (client-side, from the frames payload).
  const mtfRows = useMemo(
    () => computeMtf(frames, index, STEP_MIN[step] ?? 1, atmWindow, strikeStep),
    [frames, index, step, atmWindow, strikeStep],
  );

  // Synthetic OIChangeResponse for the by-strike chart + KPI bar (reuse as-is).
  const frameData: OIChangeResponse | null = useMemo(() => {
    if (!current) return null;
    return {
      timeframe: "range",
      expiry: expiry ?? "",
      spot: current.spot,
      asof: current.ts,
      total_call_oi_change: current.total_call_oi_change,
      total_put_oi_change: current.total_put_oi_change,
      rows: current.rows.map((r) => ({
        strike: r.strike,
        call_oi: r.call_oi,
        put_oi: r.put_oi,
        call_oi_change: r.call_oi_change,
        put_oi_change: r.put_oi_change,
        call_ltp: null,
        put_ltp: null,
        call_ltp_change: null,
        put_ltp_change: null,
      })),
    };
  }, [current, expiry]);

  // Ratio/PCR line up to the current playhead.
  const ratioSeries: LineSeriesSpec[] = useMemo(() => {
    const upto = frames.slice(0, index + 1);
    const ratioData = upto
      .filter((f) => f.ratio != null)
      .map((f) => ({ time: istIsoToChartTime(f.ts), value: f.ratio as number }));
    const pcrData = upto
      .filter((f) => f.pcr != null)
      .map((f) => ({ time: istIsoToChartTime(f.ts), value: f.pcr as number }));
    return [
      { id: "ratio", label: "Ratio", color: SERIES_COLORS.ratio, data: ratioData, priceFormat: (v) => v.toFixed(2) },
      { id: "pcr", label: "PCR", color: SERIES_COLORS.pcr, data: pcrData, priceFormat: (v) => v.toFixed(2) },
    ];
  }, [frames, index]);

  const onPlayPause = () => {
    if (!playing && index >= frames.length - 1) setIndex(0);
    setPlaying((p) => !p);
  };

  return (
    <div className="min-h-screen w-full max-w-[1500px] mx-auto px-4 md:px-6 py-3">
      <div className="panel px-4 py-3 flex flex-wrap items-center gap-3 mb-4">
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
              <span className="text-xs text-muted">Step</span>
              {STEPS.map((s) => (
                <button key={s} type="button" className={`pill ${step === s ? "pill-active" : ""}`} onClick={() => setStep(s)}>
                  {s}
                </button>
              ))}
            </div>
            <div className="flex items-center gap-2">
              <span className="text-xs text-muted">ATM ±</span>
              <AtmWindowSelect value={atmWindow} max={ATM_MAX_WINDOW} onChange={setAtmWindow} />
            </div>
            <div className="ml-auto">
              <ExportButton
                getTable={() => (frames.length ? buildReplayTable(frames) : null)}
                filenameBase={`replay_${symbol}_${date ?? "day"}_${step}`}
                disabled={frames.length === 0}
              />
            </div>
          </>
        )}
      </div>

      {!fnoEligible ? (
        <div className="panel p-6 text-sm text-muted">No F&amp;O contracts for {symbolDisplay}.</div>
      ) : (
        <div className="flex flex-col gap-4">
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

          {error && <div className="panel p-4 text-sm text-red-400">Failed to load replay: {error}</div>}
          {!error && frames.length === 0 && (
            <div className="panel p-6 text-sm text-muted text-center">
              {loading ? "Loading replay frames…" : "No stored data for this session/window."}
            </div>
          )}

          {frames.length > 0 && (
            <>
              <KPIBar data={frameData} mode="change" atmWindow={atmWindow} liveSpot={current?.spot ?? null} strikeStep={strikeStep} />
              <OIChangeChart
                data={frameData}
                mode="change"
                atmWindow={atmWindow}
                underlyingLabel={symbolDisplay}
                liveSpot={current?.spot ?? null}
                strikeStep={strikeStep}
              />
              <div className="panel p-3">
                <div className="px-1 pb-2 text-sm font-medium text-accent">Ratio &amp; PCR (through playhead)</div>
                <TimeSeriesChart series={ratioSeries} height={260} />
              </div>

              {/* Synced multi-timeframe grid — same clock, computed from the frames payload */}
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
                    {mtfRows.map((r) => {
                      const rr = callPutRatio(r.call, r.put);
                      return (
                      <tr key={r.tf} className="border-b border-border/40">
                        <td className="text-left py-1.5 px-3 text-foreground">{r.label}</td>
                        <td className={`text-right py-1.5 px-3 ${changeColor(r.call)}`}>{signedCompact(r.call)}</td>
                        <td className={`text-right py-1.5 px-3 ${changeColor(r.put)}`}>{signedCompact(r.put)}</td>
                        <td className="text-right py-1.5 px-3 text-foreground tabular-nums">{rr.text}</td>
                        <td className={`text-right py-1.5 px-3 font-semibold ${sideColor(rr.side)}`}>{sideLabel(rr.side)}</td>
                        <td className="text-right py-1.5 px-3 text-muted">{current?.spot != null ? current.spot.toFixed(1) : "—"}</td>
                        <td className="text-right py-1.5 px-3 text-muted">{current?.atm ?? "—"}</td>
                      </tr>
                      );
                    })}
                  </tbody>
                </table>
                <p className="px-3 pt-2 text-[10px] text-muted">
                  Ratio (normalized Call : Put) and Side (dominant OI-Δ side) are per timeframe; Spot and ATM are playhead levels.
                  Windowed change ({atmWindow < 0 ? "all strikes" : `ATM ± ${effectiveAtmWindow(atmWindow)}`}) vs the frame {STEP_MIN[step] ?? 1}m×steps back, from the single replay payload.
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
