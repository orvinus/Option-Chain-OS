/**
 * Backtest day replay — scrub one simulated session minute by minute.
 *
 * Everything is fetched ONCE (the day bundle + one whole-day UMP evaluation
 * per trade for its premium candles); every playhead move after that is pure
 * client-side subtraction — never a network call (the ReplayPage rule).
 *
 * Timestamps in the bundle carry the simulated IST wall clock in their HH:MM
 * positions (the runner stores naive-IST instants), so reconstruction is a
 * simple string comparison on the "HH:MM" slice.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import ReactECharts from "echarts-for-react";
import { algoApi } from "../../../api/algoRest";
import type {
  BacktestDayBundle,
  BacktestTradeRow,
  MqaeEvalResponse,
  OiStructureEvalResponse,
  UmpEvalResponse,
  Weekday,
  ZoneId,
} from "../../../types/algo";
import { exitReasonLabel } from "../../../types/algo";
import { CalcNote, Card } from "../controls";
import { DecisionTraceCard, describeDecision } from "../DecisionTrace";
import type { DecisionIndexRow, DecisionRow } from "../../../types/algo";
import { ReplayController } from "../../ReplayController";
import { levelsAt } from "../engines/umpReplay";
import type { EngineKey } from "../engines/EnginesPanel";

const TOTAL_MINUTES = 385; // 09:15 … 15:39

export interface EngineDeepLink {
  engine: EngineKey;
  day: Weekday;
  zone: ZoneId;
  expiry: string;
  date: string;
  at: string;
  configVersion: number | null;
  /** Pin evaluations to this run's FROZEN config (works even when the run
   *  was launched from the sandbox and has no version number). */
  runId?: number;
  /** The traded contract (a trade-row deep link) — seeds the UMP strike selector. */
  strike?: number;
  optionType?: "CE" | "PE";
}

interface Props {
  runId: number;
  date: string;
  onBack: () => void;
  onOpenEngine: (link: EngineDeepLink) => void;
}

function minuteHHMM(i: number): string {
  const t = 9 * 60 + 15 + i;
  return `${String(Math.floor(t / 60)).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`;
}

/** "…T10:35:00…" → "10:35" (the simulated IST wall clock). */
function hhmmOf(ts: string): string {
  return ts.slice(11, 16);
}

const STATE_STYLE: Record<string, string> = {
  idle: "text-muted",
  no_trade: "text-muted",
  hunting: "text-amber-300",
  in_trade: "text-pe",
  gated: "text-ce",
  killed: "text-ce",
  weekend: "text-muted",
  no_strike_in_band: "text-amber-300",
  no_engine_data: "text-amber-300",
};

const READING_CLS: Record<string, string> = {
  CALL: "text-pe",
  PUT: "text-ce",
  NO_TRADE: "text-muted",
};

const LEVEL_COLOR: Record<number, string> = {
  1: "#f87171", 2: "#fbbf24", 3: "#34d399", 4: "#818cf8",
};

export function BacktestDayReplay({ runId, date, onBack, onOpenEngine }: Props) {
  const [bundle, setBundle] = useState<BacktestDayBundle | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [umpEvals, setUmpEvals] = useState<Record<number, UmpEvalResponse>>({});
  // Whole-day OI + PCR/Ratio series under the run's frozen config — fetched
  // once; the playhead truncates them client-side.
  const [oiEval, setOiEval] = useState<OiStructureEvalResponse | null>(null);
  const [mqEval, setMqEval] = useState<MqaeEvalResponse | null>(null);
  const [index, setIndex] = useState(TOTAL_MINUTES - 1);
  const totalRef = useRef(TOTAL_MINUTES);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(5);
  const [focusTrade, setFocusTrade] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const b = await algoApi.backtestDayBundle(runId, date);
        if (cancelled) return;
        setBundle(b);
        setError(null);
        totalRef.current = b.minutes_per_day ?? TOTAL_MINUTES;
        setIndex(totalRef.current - 1);
        setFocusTrade(b.trades.length > 0 ? b.trades[0].id : null);
        // The day's OI-change and PCR/Ratio series (zone params from the
        // run's config; the first traded zone is representative).
        const chartZone = b.trades[0]?.zone_id ?? b.zones[0]?.zone_id ?? "Z1";
        try {
          setOiEval(await algoApi.oiStructureEval({
            day: b.day, zone: chartZone, date, expiry: b.expiry ?? undefined,
            configRun: runId,
          }));
        } catch { setOiEval(null); }
        try {
          setMqEval(await algoApi.mqaeEval({
            day: b.day, zone: chartZone, date, expiry: b.expiry ?? undefined,
            configRun: runId,
          }));
        } catch { setMqEval(null); }
        // One whole-day UMP eval per traded contract → premium candles for
        // the chart; truncation at the playhead happens client-side.
        const evals: Record<number, UmpEvalResponse> = {};
        for (const t of b.trades) {
          if (t.strike == null || !b.expiry) continue;
          try {
            evals[t.id] = await algoApi.umpEval({
              day: b.day,
              zone: t.zone_id,
              date,
              expiry: b.expiry,
              strike: t.strike,
              optionType: (t.side === "CALL" ? "CE" : "PE") as "CE" | "PE",
              // Pin by RUN (frozen doc) — correct even for sandbox/inline
              // runs whose config_version is null.
              configRun: runId,
              history: true,
            });
          } catch {
            /* chart falls back to events-only */
          }
        }
        if (!cancelled) setUmpEvals(evals);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [runId, date]);

  // rAF playback loop (ReplayPage's accumulator pattern — speed = minutes/s).
  const playRef = useRef({ playing, speed });
  playRef.current = { playing, speed };
  useEffect(() => {
    if (!playing) return undefined;
    let raf = 0;
    let last = performance.now();
    let acc = 0;
    const tick = (now: number) => {
      const frameMs = 1000 / playRef.current.speed;
      acc += now - last;
      last = now;
      const steps = Math.floor(acc / frameMs);
      if (steps > 0) {
        acc -= steps * frameMs;
        setIndex((i) => {
          const ni = Math.min(i + steps, totalRef.current - 1);
          if (ni >= totalRef.current - 1) setPlaying(false);
          return ni;
        });
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [playing]);

  const hhmm = minuteHHMM(index);
  // The date's own session length (375 minutes before 2026-08-03).
  const totalMinutes = bundle?.minutes_per_day ?? TOTAL_MINUTES;

  // Decision at the playhead (compact index is in the bundle; the full
  // record is fetched once per minute while paused/scrubbing).
  const decisionIdx = useMemo<DecisionIndexRow | null>(() => {
    const rows = bundle?.decisions_index ?? [];
    let best: DecisionIndexRow | null = null;
    for (const r of rows) {
      if (hhmmOf(r.ts) <= hhmm) best = r;
      else break;
    }
    return best;
  }, [bundle, hhmm]);
  const [decisionFull, setDecisionFull] = useState<DecisionRow | null>(null);
  useEffect(() => {
    if (!bundle || !decisionIdx || playing) return undefined;
    let alive = true;
    void (async () => {
      try {
        const r = await algoApi.backtestDecisionAt(runId, bundle.trade_date, hhmmOf(decisionIdx.ts));
        if (alive) setDecisionFull(r);
      } catch {
        if (alive) setDecisionFull(null);
      }
    })();
    return () => {
      alive = false;
    };
  }, [bundle, decisionIdx, playing, runId]);

  // ── playhead reconstruction (pure derivation over the bundle) ──
  const nowState = useMemo(() => {
    if (!bundle) return null;
    let cur = null;
    for (const s of bundle.status_timeline) {
      if (hhmmOf(s.ts) <= hhmm) cur = s;
      else break;
    }
    return cur;
  }, [bundle, hhmm]);

  const readings = useMemo(() => {
    if (!bundle) return {} as Record<string, Record<string, string>>;
    const out: Record<string, Record<string, string>> = {};
    for (const s of bundle.signals) {
      if (hhmmOf(s.ts) > hhmm) continue;
      (out[s.zone_id] ??= {})[s.indicator] = s.reading;
    }
    return out;
  }, [bundle, hhmm]);

  const visibleAlerts = useMemo(
    () => (bundle ? bundle.alerts.filter((a) => hhmmOf(a.ts) <= hhmm) : []),
    [bundle, hhmm]
  );

  const activeZone = useMemo(() => {
    if (!bundle) return null;
    return (
      bundle.zones.find((z) => z.start <= hhmm && hhmm < z.end) ?? null
    );
  }, [bundle, hhmm]);

  const equityNow = useMemo(() => {
    if (!bundle) return null;
    let eq = bundle.equity.day_start;
    for (const p of bundle.equity.points) {
      if (hhmmOf(p.ts) <= hhmm) eq = p.equity;
    }
    return eq;
  }, [bundle, hhmm]);

  const focused: BacktestTradeRow | null = useMemo(
    () => bundle?.trades.find((t) => t.id === focusTrade) ?? null,
    [bundle, focusTrade]
  );

  // Vertical entry/exit markers + CALL/PUT signal background bands shared by
  // the OI and Ratio charts.
  const tradeMarkLines = useMemo(() => {
    if (!bundle) return [];
    const lines: object[] = [];
    for (const t of bundle.trades) {
      const ein = hhmmOf(t.entry_ts);
      if (ein <= hhmm) {
        lines.push({
          xAxis: ein,
          label: { formatter: "\u25b2 " + t.side + " in", fontSize: 9, color: "#34d399" },
          lineStyle: { color: "#34d399", width: 1.5 },
        });
      }
      if (t.exit_ts && hhmmOf(t.exit_ts) <= hhmm) {
        lines.push({
          xAxis: hhmmOf(t.exit_ts),
          label: { formatter: "\u25bc out (" + exitReasonLabel(t.exit_reason) + ")", fontSize: 9, color: "#f87171" },
          lineStyle: { color: "#f87171", width: 1.5 },
        });
      }
    }
    return lines;
  }, [bundle, hhmm]);

  const signalBands = useMemo(() => {
    // Combined-signal background: green while CALL, red while PUT.
    if (!bundle) return [];
    const combined = bundle.signals
      .filter((s) => s.indicator === "combined")
      .map((s) => ({ t: hhmmOf(s.ts), r: s.reading }));
    const bands: [object, object][] = [];
    let openT: string | null = null;
    let openR = "";
    for (const c of combined) {
      if (c.t > hhmm) break;
      if (openT != null && (c.r !== openR || c.r === "NO_TRADE")) {
        bands.push([
          { xAxis: openT, itemStyle: { color: openR === "CALL" ? "rgba(52,211,153,0.10)" : "rgba(248,113,113,0.10)" } },
          { xAxis: c.t },
        ]);
        openT = null;
      }
      if (openT == null && (c.r === "CALL" || c.r === "PUT")) {
        openT = c.t;
        openR = c.r;
      }
    }
    if (openT != null) {
      bands.push([
        { xAxis: openT, itemStyle: { color: openR === "CALL" ? "rgba(52,211,153,0.10)" : "rgba(248,113,113,0.10)" } },
        { xAxis: hhmm },
      ]);
    }
    return bands;
  }, [bundle, hhmm]);

  const oiOption = useMemo(() => {
    if (!oiEval) return null;
    const n = oiEval.timestamps.filter((t) => hhmmOf(t) <= hhmm).length;
    if (n < 2) return null;
    const cats = oiEval.timestamps.slice(0, n).map(hhmmOf);
    return {
      animation: false,
      grid: { left: 48, right: 16, top: 22, bottom: 24 },
      tooltip: { trigger: "axis" },
      legend: {
        data: ["Call OI \u0394 (Cr)", "Put OI \u0394 (Cr)"],
        textStyle: { color: "#94a3b8", fontSize: 10 },
        top: 0,
      },
      xAxis: { type: "category", data: cats, axisLabel: { fontSize: 9, color: "#94a3b8" } },
      yAxis: {
        type: "value",
        scale: true,
        axisLabel: { fontSize: 9, color: "#94a3b8" },
        splitLine: { lineStyle: { color: "rgba(148,163,184,0.12)" } },
      },
      series: [
        {
          name: "Call OI \u0394 (Cr)",
          type: "line",
          data: oiEval.call_series_cr.slice(0, n),
          showSymbol: false,
          lineStyle: { width: 2, color: "#34d399" },
          markLine: { symbol: "none", silent: true, data: tradeMarkLines },
          markArea: { silent: true, data: signalBands },
        },
        {
          name: "Put OI \u0394 (Cr)",
          type: "line",
          data: oiEval.put_series_cr.slice(0, n),
          showSymbol: false,
          lineStyle: { width: 2, color: "#f87171" },
        },
      ],
    };
  }, [oiEval, hhmm, tradeMarkLines, signalBands]);

  const ratioOption = useMemo(() => {
    if (!mqEval) return null;
    const n = mqEval.timestamps.filter((t) => hhmmOf(t) <= hhmm).length;
    if (n < 2) return null;
    const cats = mqEval.timestamps.slice(0, n).map(hhmmOf);
    return {
      animation: false,
      grid: { left: 48, right: 16, top: 22, bottom: 24 },
      tooltip: { trigger: "axis" },
      legend: {
        data: ["PCR (Green)", "Ratio (Yellow)"],
        textStyle: { color: "#94a3b8", fontSize: 10 },
        top: 0,
      },
      xAxis: { type: "category", data: cats, axisLabel: { fontSize: 9, color: "#94a3b8" } },
      yAxis: {
        type: "value",
        scale: true,
        axisLabel: { fontSize: 9, color: "#94a3b8" },
        splitLine: { lineStyle: { color: "rgba(148,163,184,0.12)" } },
      },
      series: [
        {
          name: "PCR (Green)",
          type: "line",
          data: mqEval.green_pcr.slice(0, n),
          showSymbol: false,
          lineStyle: { width: 2, color: "#22c55e" },
          markLine: { symbol: "none", silent: true, data: tradeMarkLines },
          markArea: { silent: true, data: signalBands },
        },
        {
          name: "Ratio (Yellow)",
          type: "line",
          data: mqEval.yellow_ratio.slice(0, n),
          showSymbol: false,
          lineStyle: { width: 2, color: "#fbbf24" },
        },
      ],
    };
  }, [mqEval, hhmm, tradeMarkLines, signalBands]);

  const chartOption = useMemo(() => {
    if (!bundle || !focused) return null;
    const ev = umpEvals[focused.id];
    const capture = focused.ump;
    const candles = (ev?.entry_candles ?? []).filter((c) => hhmmOf(c.ts) <= hhmm);
    if (candles.length === 0) return null;
    const cats = candles.map((c) => hhmmOf(c.ts));
    // No look-ahead: the ladder as it stood at the playhead (levels_history),
    // else the trade's captured snapshot, else the end-of-day set.
    const levelsNow = levelsAt(ev?.levels_history, `${bundle.trade_date}T${hhmm}:59`) ?? capture?.levels ?? ev?.levels ?? [];
    const levels = levelsNow.map((lv) => ({
      yAxis: lv.price,
      label: {
        formatter: lv.name,
        position: "insideEndTop" as const,
        color: LEVEL_COLOR[lv.type] ?? "#94a3b8",
        fontSize: 9,
      },
      lineStyle: { color: LEVEL_COLOR[lv.type] ?? "#94a3b8", width: 1, type: "dashed" as const },
    }));
    const events = capture?.events ?? ev?.events ?? [];
    const marks = events
      .map((e) => {
        const t = e.ts.length >= 16 ? e.ts.slice(11, 16) : e.ts.slice(0, 5);
        if (t > hhmm) return null;
        // Snap to the covering 5m candle.
        let ci = -1;
        for (let i = 0; i < cats.length; i++) if (cats[i] <= t) ci = i;
        if (ci < 0) return null;
        const isExit = ["TRAIL_EXIT", "SB_EXIT", "MAX_SL", "TARGET", "BASE_SL"].includes(e.kind);
        return {
          coord: [ci, e.price],
          value: e.kind,
          symbol: isExit ? "diamond" : "triangle",
          symbolSize: 9,
          itemStyle: { color: isExit ? "#f87171" : "#34d399" },
          label: { show: true, formatter: e.kind, fontSize: 8, position: "top" as const },
        };
      })
      .filter(Boolean);
    return {
      animation: false,
      grid: { left: 48, right: 90, top: 16, bottom: 52 },
      tooltip: { trigger: "axis" },
      dataZoom: [
        { type: "inside", xAxisIndex: 0, filterMode: "none" },
        {
          type: "slider", xAxisIndex: 0, filterMode: "none",
          height: 16, bottom: 4,
          borderColor: "#374151", backgroundColor: "#111827",
          fillerColor: "rgba(59,130,246,0.15)",
          handleStyle: { color: "#3b82f6" },
          textStyle: { color: "#6b7280", fontSize: 8 },
        },
      ],
      xAxis: {
        type: "category",
        data: cats,
        axisLabel: { fontSize: 9, color: "#94a3b8" },
      },
      yAxis: {
        type: "value",
        scale: true,
        axisLabel: { fontSize: 9, color: "#94a3b8" },
        splitLine: { lineStyle: { color: "rgba(148,163,184,0.12)" } },
      },
      series: [
        {
          type: "candlestick",
          data: candles.map((c) => [c.o, c.c, c.l, c.h]),
          itemStyle: {
            color: "#22c55e", color0: "#ef4444",
            borderColor: "#22c55e", borderColor0: "#ef4444",
          },
          markLine: { symbol: "none", silent: true, data: levels },
          markPoint: { data: marks },
        },
      ],
    };
  }, [bundle, focused, umpEvals, hhmm]);

  if (error) {
    return (
      <div className="flex flex-col gap-3">
        <button type="button" className="pill text-xs self-start" onClick={onBack}>
          ← Back to run
        </button>
        <div className="text-sm text-ce">{error}</div>
      </div>
    );
  }
  if (!bundle) {
    return <div className="text-sm text-muted">Loading day bundle…</div>;
  }

  const deepLink = (engine: EngineKey, zone?: ZoneId, contract?: { strike: number; optionType: "CE" | "PE" }) =>
    onOpenEngine({
      engine,
      day: bundle.day as Weekday,
      zone: (zone ?? activeZone?.zone_id ?? "Z1") as ZoneId,
      expiry: bundle.expiry ?? "",
      date: bundle.trade_date,
      at: hhmm,
      configVersion: bundle.config_version,
      runId: bundle.run_id,
      strike: contract?.strike,
      optionType: contract?.optionType,
    });

  return (
    <div className="flex flex-col gap-4">
      {/* transport */}
      <div className="panel px-4 py-3 flex flex-col gap-2">
        <div className="flex items-center gap-3 flex-wrap">
          <button type="button" className="pill text-xs" onClick={onBack}>
            ← Run #{runId}
          </button>
          <span className="text-[12.5px] font-bold text-gray-100">
            Day Replay — {bundle.trade_date} ({bundle.day})
          </span>
          <span className="text-[10.5px] text-muted">
            {bundle.symbol} · expiry {bundle.expiry}
            {bundle.config_version != null ? ` · config v${bundle.config_version}` : ""}
          </span>
          {bundle.spotless && (
            <span className="text-[10px] text-amber-300 font-bold">SPOT-LESS DAY (full-chain baskets)</span>
          )}
          {bundle.forced_eod_close && (
            <span className="text-[10px] text-amber-300 font-bold">EOD FORCE-CLOSE</span>
          )}
          {(bundle.gaps?.length ?? 0) > 0 && (
            <span
              className="text-[10px] text-amber-300 font-bold"
              title={bundle.gaps!.map((g) => `${g[0]}–${g[1]} (${g[2]} min)`).join(", ")}
            >
              {bundle.gaps!.length} DATA GAP(S) — entries blocked while stale
            </span>
          )}
        </div>
        {decisionIdx && (
          <div className="text-[10.5px] text-muted">
            <span className="text-gray-200 font-bold">{hhmmOf(decisionIdx.ts)}</span>{" "}
            {describeDecision(decisionIdx)}
          </div>
        )}
        <ReplayController
          playing={playing}
          index={index}
          total={totalMinutes}
          speed={speed}
          currentTs={`${bundle.trade_date}T${hhmm}:00`}
          onPlayPause={() => {
            if (!playing && index >= totalMinutes - 1) setIndex(0);
            setPlaying((p) => !p);
          }}
          onRestart={() => {
            setIndex(0);
            setPlaying(false);
          }}
          onSeek={(i) => setIndex(i)}
          onSkip={(d) => setIndex((i) => Math.max(0, Math.min(totalMinutes - 1, i + d)))}
          onSpeed={setSpeed}
        />
        {/* zone strip */}
        <div className="relative h-5 rounded bg-panel border border-border overflow-hidden">
          {bundle.zones.map((z) => {
            const s = Math.max(0, (Number(z.start.slice(0, 2)) * 60 + Number(z.start.slice(3)) - 555) / totalMinutes);
            const e = Math.min(1, (Number(z.end.slice(0, 2)) * 60 + Number(z.end.slice(3)) - 555) / totalMinutes);
            return (
              <div
                key={z.zone_id}
                title={`${z.zone_id} ${z.start}–${z.end} · band ₹${z.premium_min}–₹${z.premium_max}`}
                className={`absolute top-0 bottom-0 border-x border-border/60 text-center text-[9px] leading-5 ${
                  activeZone?.zone_id === z.zone_id ? "bg-accent/25 text-gray-100" : "bg-accent/10 text-muted"
                }`}
                style={{ left: `${s * 100}%`, width: `${(e - s) * 100}%` }}
              >
                {z.zone_id}
              </div>
            );
          })}
          <div
            className="absolute top-0 bottom-0 w-[2px] bg-amber-400"
            style={{ left: `${(index / totalMinutes) * 100}%` }}
          />
        </div>
      </div>

      {/* live state at the playhead */}
      <div className="grid md:grid-cols-4 gap-4">
        <Card title="Engine State" hint={hhmm}>
          <div className={`text-lg font-bold ${STATE_STYLE[nowState?.state ?? "idle"] ?? "text-gray-100"}`}>
            {(nowState?.state ?? "idle").toUpperCase()}
          </div>
          <div className="text-[11px] text-muted mt-1">
            {nowState?.zone || "no active zone"}
            {nowState?.direction && nowState.direction !== "NO_TRADE"
              ? ` · direction ${nowState.direction}`
              : ""}
          </div>
        </Card>
        <Card title="Active Zone">
          {activeZone ? (
            <>
              <div className="text-lg font-bold text-gray-100">{activeZone.zone_id}</div>
              <div className="text-[11px] text-muted mt-1">
                {activeZone.start}–{activeZone.end} · band ₹{activeZone.premium_min}–₹{activeZone.premium_max}
                · max {activeZone.max_trades} trades
              </div>
            </>
          ) : (
            <div className="text-sm text-muted">between zones</div>
          )}
        </Card>
        <Card title="Signals (at playhead)">
          {activeZone && readings[activeZone.zone_id] ? (
            <div className="flex flex-col gap-1">
              {["oi_change", "multi_tf", "ratio", "combined"].map((ind) =>
                readings[activeZone.zone_id]?.[ind] ? (
                  <div key={ind} className="flex items-center justify-between text-[11px]">
                    <span className="text-muted">{ind}</span>
                    <span className={`font-bold ${READING_CLS[readings[activeZone.zone_id][ind]] ?? ""}`}>
                      {readings[activeZone.zone_id][ind]}
                    </span>
                  </div>
                ) : null
              )}
            </div>
          ) : (
            <div className="text-sm text-muted">no readings yet</div>
          )}
        </Card>
        <Card title="Equity">
          <div className="text-lg font-bold text-gray-100">
            {equityNow != null ? `₹${Math.round(equityNow).toLocaleString("en-IN")}` : "—"}
          </div>
          <div className="text-[11px] text-muted mt-1">
            day start {bundle.equity.day_start != null ? `₹${Math.round(bundle.equity.day_start).toLocaleString("en-IN")}` : "—"} →
            end {bundle.equity.day_end != null ? `₹${Math.round(bundle.equity.day_end).toLocaleString("en-IN")}` : "—"}
          </div>
        </Card>
      </div>

      {/* open in engine dashboards at this exact minute */}
      <div className="panel px-4 py-2.5 flex items-center gap-2 flex-wrap">
        <span className="text-[10.5px] text-muted">Open this minute in:</span>
        <button type="button" className="pill text-xs" onClick={() => deepLink("oi_structure")}>OI Change ↗</button>
        <button type="button" className="pill text-xs" onClick={() => deepLink("mtf_ratio")}>Multi-TF ↗</button>
        <button type="button" className="pill text-xs" onClick={() => deepLink("mqae")}>Ratio ↗</button>
        <button type="button" className="pill text-xs" onClick={() => deepLink("ump")}>Ultra Master Pro ↗</button>
        <span className="text-[10px] text-muted">
          (each dashboard opens on {bundle.trade_date} frozen at {hhmm}, under the run's config)
        </span>
      </div>

      {/* the OI-based visual narrative: signals as background bands, trades
          as vertical entry/exit markers, truncated at the playhead */}
      <div className="grid lg:grid-cols-2 gap-4">
        <Card
          title="OI Change — Call vs Put (Cr)"
          hint="green/red bands = unanimous CALL/PUT windows · ▲▼ = entries/exits"
        >
          {oiOption ? (
            <ReactECharts option={oiOption} style={{ height: 260 }} notMerge />
          ) : (
            <div className="text-sm text-muted py-8 text-center">
              no closed OI data at the playhead yet
            </div>
          )}
        </Card>
        <Card
          title="PCR / Ratio (MQAE inputs)"
          hint="the Green + Yellow lines the Ratio engine scores · same markers"
        >
          {ratioOption ? (
            <ReactECharts option={ratioOption} style={{ height: 260 }} notMerge />
          ) : (
            <div className="text-sm text-muted py-8 text-center">
              no closed ratio data at the playhead yet
            </div>
          )}
        </Card>
      </div>

      {/* trades + UMP chart */}
      <div className="grid lg:grid-cols-2 gap-4">
        {(bundle.decisions_index?.length ?? 0) > 0 && (
          <DecisionTraceCard
            title={`Decision Trace — up to ${hhmm}`}
            hint="every evaluated minute up to the playhead; click a row for each filter's verdict"
            rows={[
              ...(decisionFull && decisionIdx && decisionFull.id === decisionIdx.id ? [decisionFull] : []),
              ...(bundle.decisions_index ?? [])
                .filter((r) => hhmmOf(r.ts) <= hhmm && (!decisionFull || r.id !== decisionFull.id))
                .slice()
                .reverse(),
            ]}
            loadFull={(r) => algoApi.backtestDecisionAt(runId, bundle.trade_date, hhmmOf(r.ts)).catch(() => null)}
          />
        )}
        <Card
          title={`Trades (${bundle.trades.length})`}
          hint="click a row to chart it · click the time to jump the playhead"
        >
          <div className="overflow-x-auto">
            <table className="w-full text-[11px] min-w-[520px]">
              <thead>
                <tr className="text-muted text-left">
                  {["#", "Zone", "Side", "Strike", "Entry", "Exit", "Reason", "P&L"].map((h) => (
                    <th key={h} className="py-1.5 pr-2 font-medium">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {bundle.trades.map((t) => {
                  const entered = hhmmOf(t.entry_ts) <= hhmm;
                  return (
                    <tr
                      key={t.id}
                      onClick={() => setFocusTrade(t.id)}
                      className={`border-t border-border/30 cursor-pointer ${
                        focusTrade === t.id ? "bg-accent/10" : ""
                      } ${entered ? "" : "opacity-40"}`}
                    >
                      <td className="py-1.5 pr-2">{t.id}</td>
                      <td className="py-1.5 pr-2">{t.zone_id}</td>
                      <td className={`py-1.5 pr-2 font-bold ${t.side === "CALL" ? "text-pe" : "text-ce"}`}>
                        {t.side}
                      </td>
                      <td className="py-1.5 pr-2 font-mono">
                        {t.strike}
                        {t.strike != null && (
                          <button
                            type="button"
                            className="ml-1 underline decoration-dotted text-accent"
                            title="open Ultra Master Pro on this exact contract at the playhead"
                            onClick={(e) => {
                              e.stopPropagation();
                              deepLink("ump", t.zone_id as ZoneId, {
                                strike: t.strike as number,
                                optionType: (t.side === "CALL" ? "CE" : "PE") as "CE" | "PE",
                              });
                            }}
                          >
                            ↗
                          </button>
                        )}
                      </td>
                      <td className="py-1.5 pr-2 font-mono">
                        <button
                          type="button"
                          className="underline decoration-dotted"
                          onClick={(e) => {
                            e.stopPropagation();
                            const m = Number(t.entry_ts.slice(11, 13)) * 60 + Number(t.entry_ts.slice(14, 16)) - 555;
                            setIndex(Math.max(0, Math.min(totalMinutes - 1, m)));
                          }}
                        >
                          {hhmmOf(t.entry_ts)}
                        </button>{" "}
                        @ {t.entry_price.toFixed(2)}
                      </td>
                      <td className="py-1.5 pr-2 font-mono">
                        {t.exit_ts ? `${hhmmOf(t.exit_ts)} @ ${t.exit_price?.toFixed(2)}` : "open"}
                      </td>
                      <td className="py-1.5 pr-2 text-muted" title={t.exit_reason || undefined}>
                        {exitReasonLabel(t.exit_reason)}
                      </td>
                      <td className={`py-1.5 text-right font-bold ${
                        (t.pnl_rupees ?? 0) > 0 ? "text-pe" : (t.pnl_rupees ?? 0) < 0 ? "text-ce" : "text-muted"
                      }`}>
                        {t.pnl_rupees != null ? `${t.pnl_rupees > 0 ? "+" : ""}₹${Math.round(t.pnl_rupees)}` : "—"}
                      </td>
                    </tr>
                  );
                })}
                {bundle.trades.length === 0 && (
                  <tr>
                    <td colSpan={8} className="py-3 text-muted">
                      No trades this day — the unanimous rule / gates never let an entry through.
                      Scrub the playhead to watch the signals evolve.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </Card>

        <Card
          title={
            focused
              ? `UMP — ${focused.strike}${focused.side === "CALL" ? "CE" : "PE"} (trade #${focused.id})`
              : "UMP chart"
          }
          hint="5m premium candles · levels · entry/exit events — truncated at the playhead"
        >
          {chartOption ? (
            <ReactECharts option={chartOption} style={{ height: 320 }} notMerge />
          ) : (
            <div className="text-sm text-muted py-8 text-center">
              {focused
                ? "no candles at/before the playhead yet — move it forward"
                : "no trade to chart this day"}
            </div>
          )}
        </Card>
      </div>

      {/* alerts feed */}
      <Card title={`Alerts (${visibleAlerts.length} at playhead)`} hint="exactly what Telegram would have received">
        {visibleAlerts.length === 0 ? (
          <div className="text-sm text-muted">no alerts up to {hhmm}</div>
        ) : (
          <div className="flex flex-col gap-1 max-h-56 overflow-y-auto">
            {visibleAlerts.map((a, i) => (
              <div key={i} className="text-[11px] border-t border-border/30 pt-1 first:border-t-0 first:pt-0">
                <span className="font-mono text-muted mr-2">{hhmmOf(a.ts)}</span>
                {a.text}
              </div>
            ))}
          </div>
        )}
      </Card>

      <CalcNote>
        This replay is a faithful reconstruction of what the LIVE orchestrator would have done
        minute by minute: same engines, same unanimous rule, same premium-band strike pick,
        same UMP entries/exits, same paper fills and fees — driven over the archived 1-minute
        data under the run's frozen configuration.
      </CalcNote>
    </div>
  );
}
