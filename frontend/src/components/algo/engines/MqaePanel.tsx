/**
 * Master Quantitative Action Engine dashboard (the "Ratio" entry-filter
 * indicator).
 *
 * Mirrors the reference: Execution Risk Profile (mutually exclusive
 * Aggressive/Conservative switches that rewire the model weights and
 * auto-adjust the master threshold), the five model cards with kill switches
 * and parameters, the main dual-line chart (Green = PCR, Yellow = Ratio,
 * with active order-block zones, the rider trail and SMC swing labels), the
 * velocity oscillator, the three score pills, the final signal box, the
 * chronological execution history and the live algorithmic weight matrix.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { algoApi } from "../../../api/algoRest";
import type {
  AlgoConfigDoc,
  MqaeEvalResponse,
  MqaeModelLog,
  MqaeParams,
  Weekday,
  ZoneId,
} from "../../../types/algo";
import { CalcNote, Card, NumField, Switch } from "../controls";
import { EngineActions } from "./EngineActions";
import { BUCKET_MIN, type Bucket, MqaeRatioChart, MqaeVelocityChart } from "./MqaeRatioChart";
import { LoadingBlock } from "../../Loading";

const GREEN = "#22c55e";
const YELLOW = "#fbbf24";

interface Props {
  draft: AlgoConfigDoc;
  mutate: (fn: (d: AlgoConfigDoc) => void) => void;
  day: Weekday;
  zone: ZoneId;
  dirty: boolean;
  histDate: string;
  expiry: string;
  at: string;       // "" = whole session; "HH:MM" freezes the eval
  configVersion: number | null;   // pin the eval to a saved config version
  configRun?: number | null;      // pin to a backtest run's frozen config
  configSandbox?: boolean;        // evaluate under the Backtesting sandbox doc
  livePoll?: boolean;             // false in the workspace: no auto-refresh
  defaults: AlgoConfigDoc | null;
  onRequestSave: () => void;
  onCopyTo?: () => void;          // §9 copy-settings dialog (EnginesPanel owns it)
}

export function MqaePanel({
  draft, mutate, day, zone, dirty, histDate, expiry, at, configVersion,
  configRun, configSandbox, livePoll = true, defaults, onRequestSave, onCopyTo,
}: Props) {
  const [data, setData] = useState<MqaeEvalResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [replayAt, setReplayAt] = useState("");   // "" = end of series / live
  // The PCR-over-time bucket lives HERE, not in the chart, because the
  // TIME-REPLAY slider below steps at whatever interval is selected
  // (2026-09-13). 1m selected = 1-minute replay, 30m = 30-minute replay.
  // The timeframe pills ARE the zone's `mqae.timeframe` setting (2026-09-15):
  // the five models run on the selected candle, in this panel, in live trading
  // and in backtests. Local state mirrors the draft so a click re-evaluates at
  // once (the endpoint previews it); Save makes live trading use it.
  const savedTf = (draft.days[day]?.zones[zone]?.mqae?.timeframe ?? "1m") as Bucket;
  const [bucket, setBucket] = useState<Bucket>(savedTf);
  useEffect(() => {
    setBucket(savedTf);
  }, [savedTf, day, zone]);
  // The slider writes ``replayAt`` on every move so the thumb and the HH:MM
  // label track the drag, while ``scrubAt`` settles ~180 ms later and is what
  // actually re-fetches. At the 1-minute step a drag crosses ~385 positions;
  // without this that is ~385 evaluations of the whole session.
  const [scrubAt, setScrubAt] = useState("");

  // A deep link (backtest day replay / the context bar's At field) seeds the
  // slider's scrub position.
  useEffect(() => {
    setReplayAt(at);
  }, [at]);

  useEffect(() => {
    const t = window.setTimeout(() => setScrubAt(replayAt), 180);
    return () => window.clearTimeout(t);
  }, [replayAt]);

  const stepMin = BUCKET_MIN[bucket];
  const maxStep = Math.max(1, Math.floor((SESSION_LAST_MIN - SESSION_OPEN_MIN) / stepMin));

  // Changing the interval changes the grid: re-snap the scrub position onto it,
  // or the slider thumb and the HH:MM label disagree.
  useEffect(() => {
    setReplayAt((cur) =>
      cur ? replayHhmm(Math.max(1, replayStep(cur, stepMin, maxStep)), stepMin) : cur,
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stepMin]);
  const params = draft.days[day]?.zones[zone]?.mqae;

  // Per-model net contribution (all three lines combined) — feeds the
  // BULLISH/BEARISH chip on each model card, always from the real engine.
  const modelNet = useMemo(() => {
    const sums: Record<string, number> = {};
    for (const l of [
      ...(data?.logs_green ?? []),
      ...(data?.logs_yellow ?? []),
      ...(data?.logs_cross ?? []),
    ]) {
      sums[l.model] = (sums[l.model] ?? 0) + l.points;
    }
    return sums;
  }, [data]);

  // `background: true` = the 30s poll. It must NOT raise the loading flag, or
  // the panel blinks every 30 seconds over data that is already correct.
  // PDS §11 / §16 Step 6: every switch and parameter change recalculates the
  // whole state at once, before Save (the reference re-runs on every onchange).
  // The unsaved parameters are sent as a preview — but only in the editable
  // view: a panel pinned to a saved version or a backtest run's frozen config
  // must keep showing exactly that config.
  const pinned = configVersion != null || configRun != null;
  const draftParams = draft.days[day]?.zones[zone]?.mqae;
  const draftJson = useMemo(
    () => (pinned || !draftParams ? "" : JSON.stringify({ ...draftParams, timeframe: bucket })),
    [pinned, draftParams, bucket],
  );
  // Typing a number fires one evaluation after a short pause, not one per key.
  const [previewJson, setPreviewJson] = useState(draftJson);
  useEffect(() => {
    const t = window.setTimeout(() => setPreviewJson(draftJson), 450);
    return () => window.clearTimeout(t);
  }, [draftJson]);
  // Only the newest request may paint: an evaluation takes seconds, and an older
  // slower response must never overwrite the result of a later edit.
  const reqSeq = useRef(0);

  const load = useCallback(async (opts?: { background?: boolean }) => {
    const seq = ++reqSeq.current;
    if (!opts?.background) setLoading(true);
    try {
      const res = await algoApi.mqaeEval({
        day, zone, date: histDate || undefined, expiry: expiry || undefined,
        at: scrubAt || undefined, configVersion: configVersion ?? undefined,
        configRun: configRun ?? undefined, configSandbox: configSandbox || undefined,
        timeframe: bucket,
        params: previewJson || undefined,
      });
      if (seq !== reqSeq.current) return;
      setData(res);
      setError(null);
    } catch (e) {
      if (seq !== reqSeq.current) return;
      const msg = e instanceof Error ? e.message : String(e);
      setData(null);
      // A cursor earlier than the first two closed buckets is not an error, it
      // is simply too early to evaluate. Say what to do about it.
      setError(
        /no data at or before/i.test(msg)
          ? `${msg} — the ratio needs two closed buckets, so drag the replay later into the session.`
          : msg,
      );
    } finally {
      if (seq === reqSeq.current) setLoading(false);
    }
  }, [day, zone, histDate, expiry, scrubAt, configVersion, configRun, configSandbox, bucket, previewJson]);

  useEffect(() => {
    void load();
    // Auto-refresh only in live end-of-series mode — a replay scrub is a
    // frozen point in time.
    if (histDate || scrubAt || !livePoll) return undefined;
    const t = window.setInterval(() => void load({ background: true }), 30_000);
    return () => window.clearInterval(t);
  }, [load, histDate, scrubAt]);

  const set = useCallback(
    (fn: (p: MqaeParams) => void) => {
      mutate((doc) => {
        const p = doc.days[day]?.zones[zone]?.mqae;
        if (p) fn(p);
      });
    },
    [mutate, day, zone]
  );

  if (!params) return <div className="text-sm text-muted">No zone configuration.</div>;
  const aggressive = params.risk_mode === "aggressive";

  return (
    <div className="flex flex-col gap-4">
      <EngineActions
        dirty={dirty}
        day={day}
        zone={zone}
        canReset={defaults !== null}
        onRequestSave={onRequestSave}
        onCopyTo={onCopyTo}
        onRerun={() => void load()}
        loading={loading}
        onResetDefaults={() => {
          const src = defaults?.days[day]?.zones[zone]?.mqae;
          if (src) {
            mutate((d) => void (d.days[day].zones[zone].mqae = JSON.parse(JSON.stringify(src))));
          }
        }}
      />
      {dirty && (
        <CalcNote tone="warn">
          {pinned ? (
            <>
              Unsaved engine parameters — this view is pinned to a saved config, so the
              evaluation below shows <b>that</b> config, not your edits.
            </>
          ) : (
            <>
              Unsaved engine parameters — the scores, history and chart below already use your
              edits (preview). <b>Live trading still uses the saved config</b> until you Save.
            </>
          )}
        </CalcNote>
      )}
      {error && <div className="text-xs text-ce">{error}</div>}

      {/* ── TIME-REPLAY: scrub the session; the whole page re-evaluates as if
             it had ended at the chosen minute ── */}
      <div className="panel px-4 py-2.5 flex items-center gap-3 flex-wrap">
        <b className="text-[11px] text-accent tracking-wide">
          TIME-REPLAY ({SESSION_OPEN_HHMM} – {SESSION_CLOSE_HHMM}) · {stepMin}m steps
        </b>
        <input
          type="range"
          min={1}
          max={maxStep}
          step={1}
          className="flex-1 min-w-[220px] accent-amber-400"
          value={replayAt ? replayStep(replayAt, stepMin, maxStep) : maxStep}
          onChange={(e) => {
            const s = Number(e.target.value);
            setReplayAt(s >= maxStep ? "" : replayHhmm(s, stepMin));
          }}
        />
        <span className="font-mono text-sm text-gray-100 w-16 text-center">
          {replayAt || SESSION_CLOSE_HHMM}
        </span>
        {replayAt ? (
          <button type="button" className="pill text-xs" onClick={() => setReplayAt("")}>
            {histDate ? "▶ END" : "▶ LIVE"}
          </button>
        ) : (
          <span className="text-[10px] text-muted">{histDate ? "session end" : "live"}</span>
        )}
      </div>

      <div className="grid lg:grid-cols-[380px_1fr] gap-4">
        {/* ── Sidebar: risk profile + models ── */}
        <div className="flex flex-col gap-4">
          <Card title="Execution Risk Profile" hint="rewires the model weights">
            <div className="flex items-center justify-between py-1.5">
              <div>
                <div className="text-[11.5px] font-extrabold text-ce">AGGRESSIVE MODE</div>
                <div className="text-[9.5px] text-muted">
                  Early entry · Velocity & Order Blocks ×3 · threshold 3
                </div>
              </div>
              <Switch
                on={aggressive}
                danger
                onChange={(v) => {
                  // Mutual exclusion: turning one on turns the other off;
                  // turning the active one off is refused (never both off).
                  if (!v) return;
                  set((p) => {
                    p.risk_mode = "aggressive";
                    p.master_threshold = 3;
                  });
                }}
              />
            </div>
            <div className="flex items-center justify-between py-1.5 border-b border-border/40">
              <div>
                <div className="text-[11.5px] font-extrabold text-pe">CONSERVATIVE MODE</div>
                <div className="text-[9.5px] text-muted">
                  Safe entry · SMC & Crossover ×3 · threshold 6
                </div>
              </div>
              <Switch
                on={!aggressive}
                onChange={(v) => {
                  if (!v) return;
                  set((p) => {
                    p.risk_mode = "conservative";
                    p.master_threshold = 6;
                  });
                }}
              />
            </div>
            <div className="mt-2 grid grid-cols-2 gap-3">
              <NumField
                label="Master Execution Threshold"
                value={params.master_threshold}
                onChange={(v) => set((p) => void (p.master_threshold = Math.round(v)))}
              />
              <NumField
                label="Strikes ATM ± (−1 = All)"
                value={params.strikes_atm_window}
                onChange={(v) => set((p) => void (p.strikes_atm_window = Math.round(v)))}
              />
            </div>
          </Card>

          <ModelCard
            title="1. Kinetic Velocity"
            on={params.velocity_on}
            net={modelNet["velocity"]}
            onToggle={(v) => set((p) => void (p.velocity_on = v))}
          >
            <NumField label="RoC Period" value={params.velocity_period} onChange={(v) => set((p) => void (p.velocity_period = Math.round(v)))} />
            <NumField label="Surge Threshold" value={params.velocity_surge_threshold} step={0.005} onChange={(v) => set((p) => void (p.velocity_surge_threshold = v))} />
          </ModelCard>
          <ModelCard
            title="2. Order Blocks"
            on={params.order_blocks_on}
            net={modelNet["order_blocks"]}
            onToggle={(v) => set((p) => void (p.order_blocks_on = v))}
          >
            <NumField label="Impulse Trigger" value={params.ob_impulse_trigger} step={0.005} onChange={(v) => set((p) => void (p.ob_impulse_trigger = v))} />
            <NumField label="Zone Width" value={params.ob_zone_width} step={0.005} onChange={(v) => set((p) => void (p.ob_zone_width = v))} />
          </ModelCard>
          <ModelCard
            title="3. SMC Structure"
            on={params.smc_on}
            net={modelNet["smc"]}
            onToggle={(v) => set((p) => void (p.smc_on = v))}
          >
            <NumField label="Swing Bars" value={params.smc_swing_bars} onChange={(v) => set((p) => void (p.smc_swing_bars = Math.round(v)))} />
            <NumField label="Min Move" value={params.smc_min_move} step={0.01} onChange={(v) => set((p) => void (p.smc_min_move = v))} />
          </ModelCard>
          <ModelCard
            title="4. Macro Trend Rider"
            on={params.trend_rider_on}
            net={modelNet["trend_rider"]}
            onToggle={(v) => set((p) => void (p.trend_rider_on = v))}
          >
            <NumField label="Trail Period" value={params.trail_period} onChange={(v) => set((p) => void (p.trail_period = Math.round(v)))} />
            <NumField label="Buffer" value={params.trail_buffer} step={0.01} onChange={(v) => set((p) => void (p.trail_buffer = v))} />
          </ModelCard>
          <ModelCard
            title="5. Crossover Momentum"
            on={params.crossover_on}
            net={modelNet["crossover"]}
            onToggle={(v) => set((p) => void (p.crossover_on = v))}
          >
            <div className="text-[10.5px] text-muted col-span-2">
              Green vs Yellow direct comparison — ×3 in Conservative mode.
            </div>
          </ModelCard>
        </div>

        {/* ── Main: result bar + charts + logs ── */}
        <div className="flex flex-col gap-4">
          {data && (
            <Card
              title="Signal Synthesis"
              hint={`${data.symbol} · exp ${data.expiry} · strikes ${data.strike_min}–${data.strike_max}`}
            >
              <div className="flex items-stretch gap-3 flex-wrap">
                <ScorePill label="Green (PCR) Net" score={data.score_green} accent={GREEN} />
                <ScorePill label="Yellow (Ratio) Net" score={data.score_yellow} accent={YELLOW} />
                <ScorePill label="Crossover" score={data.score_cross} accent="#58a6ff" />
                <div className="flex-1 min-w-[180px] flex flex-col items-center justify-center">
                  <span
                    className={`text-2xl font-black tracking-widest px-6 py-2 rounded-lg border-2 ${
                      data.signal === "CALL"
                        ? "text-pe border-pe/60 bg-pe/10"
                        : data.signal === "PUT"
                          ? "text-ce border-ce/60 bg-ce/10"
                          : "text-gray-400 border-border bg-panel"
                    }`}
                  >
                    {data.signal === "NO_TRADE" ? "NO TRADE" : `${data.signal} EXECUTION`}
                  </span>
                  <span className="text-[10.5px] text-muted mt-1">
                    Total {data.total >= 0 ? "+" : ""}
                    {data.total} vs ±{params.master_threshold} threshold
                  </span>
                </div>
              </div>
            </Card>
          )}

          {data && (
            <MqaeRatioChart
              data={data}
              params={params}
              persistKey={`${day}-${zone}`}
              bucket={bucket}
              onBucketChange={(b) => {
                setBucket(b);
                mutate((doc) => {
                  const p = doc.days[day]?.zones[zone]?.mqae;
                  if (p) p.timeframe = b;
                });
              }}
            />
          )}
          {/* Pass the bucket: this chart already accepted one but nothing ever
              supplied it, so the oscillator was stuck on 1m while the chart
              above it could be on 30m. */}
          {data && <MqaeVelocityChart data={data} bucket={bucket} />}

          <div className="grid md:grid-cols-2 gap-4">
            <Card title="Chronological Execution History" hint="every signal transition this session">
              <div className="max-h-64 overflow-y-auto flex flex-col gap-1.5">
                {data?.history.slice().reverse().map((h) => (
                  <div
                    key={h.bar}
                    className={`flex items-center justify-between px-2.5 py-1.5 rounded border text-xs ${
                      h.signal === "CALL"
                        ? "border-pe/40 bg-pe/5 text-pe"
                        : h.signal === "PUT"
                          ? "border-ce/40 bg-ce/5 text-ce"
                          : "border-border bg-panel text-gray-400"
                    }`}
                  >
                    <span className="font-bold font-mono">{h.ts ? h.ts.slice(11, 16) : `#${h.bar}`}</span>
                    <span className="font-black tracking-wide">
                      {h.signal === "NO_TRADE" ? "NO TRADE" : h.signal}
                    </span>
                    <span className="font-mono text-[10px]">Net {h.total >= 0 ? "+" : ""}{h.total}</span>
                  </div>
                )) ?? (loading ? <LoadingBlock /> : <div className="text-xs text-muted">No evaluation yet.</div>)}
              </div>
            </Card>
            <Card title="Live Algorithmic Weight Matrix" hint="every active model's contribution now">
              <div className="max-h-64 overflow-y-auto text-[11px] flex flex-col gap-2">
                <LogSection title="1. GREEN LINE (PCR)" color={GREEN} net={data?.score_green ?? 0} logs={data?.logs_green ?? []} loading={loading && !data} />
                <LogSection title="2. YELLOW LINE (INVERSE)" color={YELLOW} net={data?.score_yellow ?? 0} logs={data?.logs_yellow ?? []} loading={loading && !data} />
                <LogSection title="3. CROSSOVER MOMENTUM" color="#58a6ff" net={data?.score_cross ?? 0} logs={data?.logs_cross ?? []} loading={loading && !data} />
                <div className="border-t border-border/60 pt-1.5 text-gray-100">
                  <b>4. SYNTHESIS [{params.risk_mode.toUpperCase()}]</b>
                  <div className="flex justify-between text-muted">
                    <span>Total Computed Matrix Score</span>
                    <span className="text-white font-black">
                      {data ? `${data.total >= 0 ? "+" : ""}${data.total}` : "—"}
                    </span>
                  </div>
                  <div className="flex justify-between text-muted">
                    <span>Execution Threshold Required</span>
                    <span>±{params.master_threshold}</span>
                  </div>
                </div>
              </div>
            </Card>
          </div>
        </div>
      </div>
    </div>
  );
}

/** Slider step ↔ IST HH:MM helpers for the TIME-REPLAY bar.
 *
 * The step size follows the PCR-over-time bucket rather than being pinned at 5
 * minutes, and the end of the range derives from the SESSION CLOSE rather than
 * a literal. The old hard-coded max of 75 steps put the end at 15:30, so on
 * every date since 2026-08-03 the last ten minutes of the session were simply
 * unreachable on this slider. */
export const SESSION_OPEN_MIN = 9 * 60 + 15;   // 09:15
export const SESSION_LAST_MIN = 15 * 60 + 40;  // 15:40 close
const hhmm = (total: number) =>
  `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
export const SESSION_OPEN_HHMM = hhmm(SESSION_OPEN_MIN);
export const SESSION_CLOSE_HHMM = hhmm(SESSION_LAST_MIN);

function replayHhmm(step: number, stepMin: number): string {
  return hhmm(SESSION_OPEN_MIN + step * stepMin);
}

function replayStep(v: string, stepMin: number, maxStep: number): number {
  const [h, m] = v.split(":").map(Number);
  const mins = (h ?? 0) * 60 + (m ?? 0) - SESSION_OPEN_MIN;
  return Math.max(0, Math.min(maxStep, Math.round(mins / stepMin)));
}

function ModelCard(props: {
  title: string;
  on: boolean;
  onToggle: (v: boolean) => void;
  net?: number;               // real engine contribution → BULLISH/BEARISH chip
  children: React.ReactNode;
}) {
  const net = props.net ?? 0;
  const state = !props.on ? "OFF" : net > 0 ? "BULLISH" : net < 0 ? "BEARISH" : "NEUTRAL";
  const color =
    !props.on ? "#8b96a5" : net > 0 ? "#22c55e" : net < 0 ? "#ef4444" : "#8b96a5";
  return (
    <div className={`panel px-4 py-3 ${props.on ? "" : "opacity-60"}`}>
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs font-bold uppercase tracking-wide text-gray-100">{props.title}</span>
        <div className="flex items-center gap-2">
          <span
            className="text-[9.5px] font-black px-1.5 py-0.5 rounded border"
            style={{ color, borderColor: `${color}66` }}
            title="This model's live net contribution across Green/Yellow/Crossover"
          >
            {state}
            {props.on && net !== 0 ? ` ${net > 0 ? "+" : ""}${Math.round(net * 100) / 100}` : ""}
          </span>
          <Switch on={props.on} onChange={props.onToggle} />
        </div>
      </div>
      <div className="grid grid-cols-2 gap-3">{props.children}</div>
    </div>
  );
}

function ScorePill(props: { label: string; score: number; accent: string }) {
  const state = props.score > 0 ? "BULLISH" : props.score < 0 ? "BEARISH" : "NEUTRAL";
  const color = props.score > 0 ? "#22c55e" : props.score < 0 ? "#ef4444" : "#8b96a5";
  return (
    <div
      className="flex flex-col justify-center px-3.5 py-2 rounded-lg border border-border bg-panel min-w-[130px]"
      style={{ borderLeft: `3px solid ${props.accent}` }}
    >
      <span className="text-[9.5px] text-muted uppercase font-bold">{props.label}</span>
      <span className="text-base font-black" style={{ color }}>
        {state}{" "}
        <span className="text-xs font-mono">
          {props.score >= 0 ? "+" : ""}
          {props.score}
        </span>
      </span>
    </div>
  );
}

function LogSection(props: {
  title: string; color: string; net: number; logs: MqaeModelLog[];
  /** No evaluation has arrived yet — "no conditions met" would be a guess. */
  loading?: boolean;
}) {
  return (
    <div>
      <div className="flex justify-between font-bold" style={{ color: props.color }}>
        <span>{props.title}</span>
        <span>Net: {props.net >= 0 ? "+" : ""}{props.net}</span>
      </div>
      {props.logs.length === 0 ? (
        props.loading ? (
          <LoadingBlock />
        ) : (
          <div className="text-muted">No active conditions met.</div>
        )
      ) : (
        props.logs.map((l, i) => (
          <div key={`${l.text}-${i}`} className="flex justify-between text-muted">
            <span>{l.text}</span>
            <span className={l.points >= 0 ? "text-pe" : "text-ce"}>
              {l.points >= 0 ? "+" : ""}
              {l.points} {l.points >= 0 ? "CALL" : "PUT"}
            </span>
          </div>
        ))
      )}
    </div>
  );
}
