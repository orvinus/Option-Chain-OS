/**
 * OI Structure Engine dashboard (the "OI Change" entry-filter indicator).
 *
 * Mirrors oi_smc_engine.html: quad charts (Call/Put 5-minute candles on top,
 * Call/Put 1-minute lines with swing labels + the shaded range zone below),
 * the two engine master switches, the Global Aggregation Result with the
 * confidence gate, contributing events (the Algo Payload card is hidden —
 * HIDDEN-2026-09-13), the 12-slot
 * Interpretation Matrix and the detection/scoring parameter editors.
 *
 * Honesty rule: the evaluation always reflects the SAVED zone config — edits
 * here go into the same draft + confirm-save flow as everything else, and a
 * banner appears while draft ≠ saved so a tweaked matrix is never silently
 * "live" before it is committed (§13).
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { algoApi } from "../../../api/algoRest";
import type {
  AlgoConfigDoc,
  CandleRead,
  OiStructureEvalResponse,
  OIStructureParams,
  Side3,
  Weekday,
  ZoneId,
} from "../../../types/algo";
import type { OIChangeResponse } from "../../../types";
import { OIChangeChart } from "../../OIChangeChart";
import { fmtCr } from "../../../utils/chartBuckets";
import { CalcNote, Card, NumField, SelectField, Switch } from "../controls";
import { EngineActions } from "./EngineActions";
import { OiStructureTimeChart } from "./OiStructureTimeChart";
import { LoadingBlock } from "../../Loading";

const EVENTS: { id: string; label: string }[] = [
  { id: "HH", label: "Higher High (HH)" },
  { id: "HL", label: "Higher Low (HL)" },
  { id: "LH", label: "Lower High (LH)" },
  { id: "LL", label: "Lower Low (LL)" },
  { id: "BREAKOUT_UP", label: "Range Breakout Up" },
  { id: "BREAKOUT_DOWN", label: "Range Breakout Down" },
];

interface Props {
  draft: AlgoConfigDoc;
  mutate: (fn: (d: AlgoConfigDoc) => void) => void;
  day: Weekday;
  zone: ZoneId;
  dirty: boolean;
  histDate: string; // "" = live
  expiry: string;   // "" = current weekly (auto)
  at: string;       // "" = whole session; "HH:MM" freezes the eval
  configVersion: number | null;   // pin the eval to a saved config version
  configRun?: number | null;      // pin to a backtest run's frozen config
  configSandbox?: boolean;        // evaluate under the Backtesting sandbox doc
  livePoll?: boolean;             // false in the workspace: no auto-refresh
  defaults: AlgoConfigDoc | null;
  onRequestSave: () => void;
  onCopyTo?: () => void;          // §9 copy-settings dialog (EnginesPanel owns it)
}

export function OiStructurePanel({
  draft, mutate, day, zone, dirty, histDate, expiry, at, configVersion,
  configRun, configSandbox, livePoll = true, defaults, onRequestSave, onCopyTo,
}: Props) {
  const [data, setData] = useState<OiStructureEvalResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // Structure overlay per 1-Minute chart. Pure VIEW state, so it lives in the
  // browser and never marks the trading config unsaved — matching the
  // reference, where it is plain in-page state (docs/reference/oi_smc_engine.html).
  const [structCall, setStructCall] = useState(() => loadStruct("call"));
  const [structPut, setStructPut] = useState(() => loadStruct("put"));
  const params = draft.days[day]?.zones[zone]?.oi_structure;

  // Per-strike replica of the dashboard OI Change tab: the backend already
  // restricted `by_strike` to the engine's basket (Δ since open at `as_of`),
  // so it is rendered through the unchanged `OIChangeChart` with no ATM
  // re-windowing (`atmWindow={-1}`) and its bars sum to the engine series.
  const strikeData = useMemo<OIChangeResponse | null>(() => {
    if (!data?.by_strike || data.by_strike.length === 0) return null;
    const rows = data.by_strike.map((r) => ({
      ...r,
      call_ltp: null,
      put_ltp: null,
      call_ltp_change: null,
      put_ltp_change: null,
    }));
    return {
      timeframe: "range",
      expiry: data.expiry,
      spot: data.spot,
      asof: data.as_of ?? data.timestamps[data.timestamps.length - 1] ?? "",
      total_call_oi_change: rows.reduce((a, r) => a + r.call_oi_change, 0),
      total_put_oi_change: rows.reduce((a, r) => a + r.put_oi_change, 0),
      rows,
    };
  }, [data]);

  // `background: true` = the 30s poll. It must NOT raise the loading flag, or
  // the panel blinks every 30 seconds over data that is already correct.
  const load = useCallback(async (opts?: { background?: boolean }) => {
    if (!opts?.background) setLoading(true);
    try {
      const res = await algoApi.oiStructureEval({
        day,
        zone,
        date: histDate || undefined,
        expiry: expiry || undefined,
        at: at || undefined,
        configVersion: configVersion ?? undefined,
        configRun: configRun ?? undefined,
        configSandbox: configSandbox || undefined,
      });
      setData(res);
      setError(null);
    } catch (e) {
      setData(null);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [day, zone, histDate, expiry, at, configVersion, configRun, configSandbox]);

  useEffect(() => {
    void load();
    if (histDate || at || !livePoll) return undefined; // historical / frozen / workspace → one fetch
    const t = window.setInterval(() => void load({ background: true }), 30_000);
    return () => window.clearInterval(t);
  }, [load, histDate]);

  const set = useCallback(
    (fn: (p: OIStructureParams) => void) => {
      mutate((doc) => {
        const p = doc.days[day]?.zones[zone]?.oi_structure;
        if (p) fn(p);
      });
    },
    [mutate, day, zone]
  );

  if (!params) return <div className="text-sm text-muted">No zone configuration.</div>;

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
          const src = defaults?.days[day]?.zones[zone]?.oi_structure;
          if (src) {
            mutate((d) => void (d.days[day].zones[zone].oi_structure = JSON.parse(JSON.stringify(src))));
          }
        }}
      />
      {dirty && (
        <CalcNote tone="warn">
          Unsaved engine parameters — the evaluation below reflects the <b>saved</b> config.
          Save to apply your edits.
        </CalcNote>
      )}
      {error && <div className="text-xs text-ce">{error}</div>}

      {/* Master switches */}
      <Card title="Engine Master Switches" hint="run either timeframe alone, or both">
        <div className="grid md:grid-cols-2 gap-4">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-[12.5px] font-semibold">1-Minute Structure Engine</div>
              <div className="text-[10.5px] text-muted">
                {params.engine_1m_on
                  ? "ON — HH/HL/LH/LL and Range Breakout contribute to the signal"
                  : "OFF — 1-minute structure fully excluded from scoring"}
              </div>
            </div>
            <Switch on={params.engine_1m_on} onChange={(v) => set((p) => void (p.engine_1m_on = v))} />
          </div>
          <div className="flex items-center justify-between">
            <div>
              <div className="text-[12.5px] font-semibold">5-Minute Red Candle Rule</div>
              <div className="text-[10.5px] text-muted">
                {params.engine_5m_on
                  ? "ON — the fixed red-candle rule contributes to the signal"
                  : "OFF — red candles add nothing regardless of colour"}
              </div>
            </div>
            <Switch on={params.engine_5m_on} onChange={(v) => set((p) => void (p.engine_5m_on = v))} />
          </div>
        </div>
      </Card>

      {/* Result */}
      {data && (
        <Card title="Global Aggregation Result" hint={`${data.symbol} · exp ${data.expiry} · strikes ${data.strike_min}–${data.strike_max}`}>
          <div className="flex items-center gap-6 flex-wrap">
            <div>
              <div className="text-[10px] text-muted uppercase mb-1">Final Bias</div>
              <span
                className={`text-xl font-extrabold px-4 py-1.5 rounded-lg ${
                  data.signal === "CALL"
                    ? "text-pe bg-pe/10 border border-pe/40"
                    : data.signal === "PUT"
                      ? "text-ce bg-ce/10 border border-ce/40"
                      : "text-amber-400 bg-amber-950/40 border border-amber-800/40"
                }`}
              >
                {data.signal}
              </span>
            </div>
            <div className="flex-1 min-w-[220px]">
              <div className="flex justify-between text-[10.5px] text-muted mb-1">
                <span>
                  CALL <b className="text-pe">{data.call_pts}</b> pts
                </span>
                <span>
                  PUT <b className="text-ce">{data.put_pts}</b> pts
                </span>
              </div>
              <div className="h-2 rounded bg-panel flex overflow-hidden">
                <div
                  className="bg-pe"
                  style={{
                    width: `${(data.call_pts / Math.max(data.call_pts + data.put_pts, 1)) * 100}%`,
                  }}
                />
                <div
                  className="bg-ce"
                  style={{
                    width: `${(data.put_pts / Math.max(data.call_pts + data.put_pts, 1)) * 100}%`,
                  }}
                />
              </div>
              <div className="text-[10.5px] text-muted mt-1.5">
                Confidence gate:{" "}
                {data.signal === "NO_TRADE"
                  ? `leading side (${Math.max(data.call_pts, data.put_pts)} pts) is below the ${params.confidence_threshold} threshold → NO_TRADE`
                  : `${data.signal} leads with ${Math.max(data.call_pts, data.put_pts)} pts, clearing ${params.confidence_threshold}`}
              </div>
            </div>
          </div>
        </Card>
      )}

      {/* Quad charts, per docs/reference/oi_smc_engine.html: Call/Put 5-Minute
          candles on top, Call/Put 1-Minute lines with swing labels and the
          shaded range zone below. The interval pill bar is gone — each chart
          has a fixed timeframe now. */}
      {data && (
        <div className="flex flex-col gap-4">
          <div className="text-[10.5px] text-muted">
            engine runs on 1m closed buckets · the 5-Minute charts re-bucket the same series
            (session-anchored) · the fixed 5m rule reads the last FULLY CLOSED candle
          </div>
          <div className="grid xl:grid-cols-2 gap-3">
            <OiStructureTimeChart
              data={data}
              side="call"
              interval={5}
              height={QUAD_H}
              // Structure belongs to the 1-Minute charts only, as in the
              // reference: the swings and range zone are computed on the
              // 1-minute series, so drawing them over 5-minute candles maps
              // 1-minute indices onto the wrong buckets.
              showStructure={false}
              persistKey={`${day}-${zone}-call5`}
              engine5mOn={params.engine_5m_on}
            />
            <OiStructureTimeChart
              data={data}
              side="put"
              interval={5}
              height={QUAD_H}
              // Structure belongs to the 1-Minute charts only, as in the
              // reference: the swings and range zone are computed on the
              // 1-minute series, so drawing them over 5-minute candles maps
              // 1-minute indices onto the wrong buckets.
              showStructure={false}
              persistKey={`${day}-${zone}-put5`}
              engine5mOn={params.engine_5m_on}
            />
            <OiStructureTimeChart
              data={data}
              side="call"
              interval={1}
              height={QUAD_H}
              showStructure={structCall}
              toggle={
                <StructureToggle on={structCall} onChange={(v) => setStructCall(saveStruct("call", v))} />
              }
              persistKey={`${day}-${zone}-call1`}
              engine5mOn={params.engine_5m_on}
            />
            <OiStructureTimeChart
              data={data}
              side="put"
              interval={1}
              height={QUAD_H}
              showStructure={structPut}
              toggle={
                <StructureToggle on={structPut} onChange={(v) => setStructPut(saveStruct("put", v))} />
              }
              persistKey={`${day}-${zone}-put1`}
              engine5mOn={params.engine_5m_on}
            />
          </div>
          {strikeData ? (
            <div className="flex flex-col gap-2">
              <div className="text-[10.5px] text-muted px-1">
                Per strike · strikes {data.strike_min}–{data.strike_max} · Δ since open at{" "}
                {(data.as_of ?? "").slice(11, 16) || "—"} · Σ CE ={" "}
                <b className="text-ce">{fmtCr(data.call_series_cr[data.call_series_cr.length - 1])}</b> · Σ PE ={" "}
                <b className="text-pe">{fmtCr(data.put_series_cr[data.put_series_cr.length - 1])}</b>{" "}
                (the engine series' last point — bars sum to it)
              </div>
              <OIChangeChart
                data={strikeData}
                mode="change"
                atmWindow={-1}
                underlyingLabel={data.symbol}
                liveSpot={data.spot}
                strikeStep={data.strike_step ?? null}
              />
            </div>
          ) : (
            loading ? (
              <LoadingBlock />
            ) : (
              <div className="text-[10.5px] text-muted px-1">
                Per-strike deltas unavailable for this evaluation (older backend or the strike query failed).
              </div>
            )
          )}
        </div>
      )}

      <div className="grid gap-4">
        {/* Contributing events. The Algo Payload card that used to sit beside
            this one is HIDDEN-2026-09-13 (see below) — restore it and change
            this back to `grid md:grid-cols-2 gap-4` to get the two-up row. */}
        <Card title="Contributing Events" hint="every event that scored, in order detected">
          <div className="max-h-72 overflow-y-auto">
            {data?.contributions.map((c, i) => (
              <div
                key={`${c.label}-${i}`}
                className="flex justify-between text-[11px] py-1 border-b border-border/30 last:border-b-0"
              >
                <span>
                  {c.label}{" "}
                  <span
                    className={`ml-1 px-1.5 rounded text-[10px] font-bold ${
                      c.side === "Call" ? "text-pe" : c.side === "Put" ? "text-ce" : "text-muted"
                    }`}
                  >
                    {c.side}
                  </span>
                </span>
                <span className="font-bold">{c.points} pts</span>
              </div>
            )) ?? (loading ? <LoadingBlock /> : <div className="text-xs text-muted">No evaluation yet.</div>)}
          </div>
        </Card>
        {/* HIDDEN-2026-09-13 — Algo Payload card, removed from the UI as
            unnecessary. Display only: the payload is still computed and still
            returned by /oi-structure, so nothing about the engine changed and
            un-commenting this block is the whole restore.
        <Card title="Algo Payload" hint="what the engine hands to the execution layer">
          <pre className="text-[10.5px] text-pe/80 bg-black/40 border border-border rounded-lg p-3 overflow-auto max-h-72">
            {data ? JSON.stringify(data.payload, null, 2) : "—"}
          </pre>
        </Card>
        */}
      </div>

      {/* Interpretation matrix */}
      <Card
        title="Interpretation Matrix"
        hint="the ONLY place an event acquires a side — 1 Min events only; the 5m red-candle rule is fixed"
      >
        <table className="w-full text-xs">
          <thead>
            <tr className="text-muted text-left">
              <th className="py-1 font-medium">1m Event</th>
              <th className="py-1 font-medium">Call Chart →</th>
              <th className="py-1 font-medium">Put Chart →</th>
            </tr>
          </thead>
          <tbody>
            {EVENTS.map((ev) => (
              <tr key={ev.id} className="border-t border-border/40">
                <td className="py-1.5">{ev.label}</td>
                {(["call", "put"] as const).map((chart) => (
                  <td key={chart} className="py-1.5 pr-4">
                    <select
                      className="bg-panel border border-border rounded px-2 py-1 text-xs w-24"
                      value={params.matrix[chart][ev.id]}
                      onChange={(e) =>
                        set((p) => void (p.matrix[chart][ev.id] = e.target.value as Side3))
                      }
                    >
                      <option>Call</option>
                      <option>Put</option>
                      <option>Ignore</option>
                    </select>
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      {/* Detection + scoring params */}
      <div className="grid md:grid-cols-2 gap-4">
        <Card title="Swing Detection & Range Zone" hint="1 Min only">
          <div className="grid grid-cols-3 gap-3">
            <NumField label="Left Bars" value={params.left_bars} onChange={(v) => set((p) => void (p.left_bars = Math.round(v)))} />
            <NumField label="Right Bars" value={params.right_bars} onChange={(v) => set((p) => void (p.right_bars = Math.round(v)))} />
            <NumField label="Min Swing Move (Cr)" value={params.min_swing_move_cr} step={0.01} onChange={(v) => set((p) => void (p.min_swing_move_cr = v))} />
            <NumField label="EQH/EQL Tolerance (Cr)" value={params.eq_tolerance_cr} step={0.005} onChange={(v) => set((p) => void (p.eq_tolerance_cr = v))} />
            <NumField label="Breakout Buffer (Cr)" value={params.breakout_buffer_cr} step={0.005} onChange={(v) => set((p) => void (p.breakout_buffer_cr = v))} />
            <NumField label="Strikes ATM ± (−1 = All)" value={params.strikes_atm_window} onChange={(v) => set((p) => void (p.strikes_atm_window = Math.round(v)))} />
          </div>
        </Card>
        <Card title="Signal Intelligence — Scoring Weights">
          <div className="grid grid-cols-3 gap-3">
            <NumField label="Swing Event Weight" value={params.swing_weight} onChange={(v) => set((p) => void (p.swing_weight = v))} />
            <NumField label="Range Breakout Weight" value={params.breakout_weight} onChange={(v) => set((p) => void (p.breakout_weight = v))} />
            <NumField label="Liquidity Sweep Weight" value={params.sweep_weight} onChange={(v) => set((p) => void (p.sweep_weight = v))} />
            <NumField label="Red Candle Weight (5m)" value={params.red_candle_weight} onChange={(v) => set((p) => void (p.red_candle_weight = v))} />
            <NumField label="Call+Put Agreement Bonus" value={params.agreement_bonus} onChange={(v) => set((p) => void (p.agreement_bonus = v))} />
            <NumField label="Min Confidence Threshold" value={params.confidence_threshold} onChange={(v) => set((p) => void (p.confidence_threshold = v))} />
          </div>
        </Card>
        <Card
          title="Swing Scoring Lookback"
          hint="filters points only — never the zone or the breakout"
        >
          <div className="grid grid-cols-3 gap-3">
            <NumField
              label="Lookback (bars, 0 = whole session)"
              value={params.swing_scoring_lookback ?? 0}
              onChange={(v) =>
                set((p) => void (p.swing_scoring_lookback = Math.max(0, Math.min(375, Math.round(v)))))
              }
            />
          </div>
          <div className="text-[11px] text-muted mt-2 leading-relaxed">
            {(params.swing_scoring_lookback ?? 0) === 0 ? (
              <>
                <b className="text-gray-200">Whole session.</b> Every swing confirmed since the open
                keeps scoring, so the point totals climb all day and the{" "}
                {params.confidence_threshold} threshold is cleared easily by the afternoon. This is
                the original behaviour.
              </>
            ) : (
              <>
                Only swings confirmed in the last{" "}
                <b className="text-gray-200">{params.swing_scoring_lookback} bars</b> score, which
                keeps the {params.confidence_threshold} threshold meaningful late in the session. The
                range zone and the breakout test still read the complete structure.
              </>
            )}
          </div>
        </Card>
        <Card title="Candle Rule" hint="the red-candle rule on the folded candles">
          <div className="grid grid-cols-2 gap-3">
            <NumField
              label="Candle Size (1-min bars)"
              value={params.candle_size_bars ?? 5}
              onChange={(v) =>
                set((p) => void (p.candle_size_bars = Math.max(2, Math.min(60, Math.round(v)))))
              }
            />
            <SelectField
              label="Candle Read"
              value={params.candle_read ?? "closed"}
              options={[
                { value: "closed", label: "Last fully closed candle" },
                { value: "forming", label: "Current forming candle" },
              ]}
              onChange={(v) => set((p) => void (p.candle_read = v as CandleRead))}
            />
          </div>
          <div className="text-[11px] text-muted mt-2 leading-relaxed">
            Reading{" "}
            <b className="text-gray-200">
              {params.candle_size_bars ?? 5}-bar candle,{" "}
              {(params.candle_read ?? "closed") === "forming" ? "forming" : "last closed"}
            </b>
            .{" "}
            {(params.candle_read ?? "closed") === "forming"
              ? "The forming candle's close is the live value, so its colour can flip while the candle is still being built."
              : "A completed candle never changes, so this reading is stable until the next candle closes."}
          </div>
        </Card>
      </div>
    </div>
  );
}


/** Height of each quad panel. The reference draws these at 220px; four 500px
 *  panels would be over 1000px of grid. */
const QUAD_H = 260;

const STRUCT_KEY = (side: "call" | "put") => `oistruct.structure.${side}`;

function loadStruct(side: "call" | "put"): boolean {
  try {
    return window.localStorage.getItem(STRUCT_KEY(side)) !== "off";
  } catch {
    return true;
  }
}

/** Persist and return the value, so callers can set state in one expression. */
function saveStruct(side: "call" | "put", v: boolean): boolean {
  try {
    window.localStorage.setItem(STRUCT_KEY(side), v ? "on" : "off");
  } catch {
    /* private window — the toggle still works for this session */
  }
  return v;
}

/** The reference's per-chart "Structure" switch. Rendered INTO the chart
 *  header's right-hand group, beside the drawing toolbar — absolutely
 *  positioning it at the top-right overlapped the toolbar buttons. */
function StructureToggle(props: { on: boolean; onChange: (v: boolean) => void }) {
  return (
    <span className="flex items-center gap-1.5 text-[10.5px] text-muted whitespace-nowrap">
      Structure
      <Switch on={props.on} onChange={props.onChange} />
    </span>
  );
}
