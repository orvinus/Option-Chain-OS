/**
 * MTF Ratio dashboard (the "Multi-TF" entry-filter indicator).
 *
 * Mirrors the Multi-Timeframe Ratio Analysis reference: the per-timeframe
 * positions table (Call/Put OI Δ, "1 : N" ratio, Lowest OI Side = lower SIGNED
 * Δ — the magnitude-based Ratio Side column was removed 2026-09-13), the
 * ordered rule builder
 * (Timeframe | Lowest-OI Side | Operator Any/Above/Below | Threshold | Call Sign |
 * Put Sign → THEN Call/Put/Neutral, first 100% match wins), the preview
 * direction and the "why this result?" checks.
 *
 * Rule edits go into the config draft — evaluation reflects the SAVED config
 * (§13), with the same unsaved-changes banner as the other engine views.
 */
import { useCallback, useEffect, useState } from "react";
import { algoApi } from "../../../api/algoRest";
import type {
  AlgoConfigDoc,
  MtfCondition,
  MtfRatioEvalResponse,
  MtfRatioParams,
  Weekday,
  ZoneId,
} from "../../../types/algo";
import { CalcNote, Card, NumField, SelectField, Switch } from "../controls";
import { signedCompact } from "../../../utils/num";
import { EngineActions } from "./EngineActions";
import { LoadingBlock } from "../../Loading";

const THRESHOLDS = ["Any", "1:1", "1:1.2", "1:1.5", "1:2", "1:3", "1:4"];
const ALL_TIMEFRAMES = ["1m", "3m", "5m", "10m", "15m", "30m", "1h", "2h", "3h", "full_day"];
const TF_LABEL: Record<string, string> = {
  "1m": "1 Min", "3m": "3 Min", "5m": "5 Min", "10m": "10 Min", "15m": "15 Min",
  "30m": "30 Min", "1h": "1 Hr", "2h": "2 Hrs", "3h": "3 Hrs", full_day: "Full Day",
};

function sideCls(side: string): string {
  return side === "Call" ? "text-pe" : side === "Put" ? "text-ce" : "text-gray-300";
}

/** Crores → the Multi-TF page's own format (signedCompact of raw OI), so the
 *  two pages show identical figures for identical data (2026-09-23). */
function fmtCr(v: number): string {
  return signedCompact(Math.round(v * 1e7));
}

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

export function MtfRatioPanel({
  draft, mutate, day, zone, dirty, histDate, expiry, at, configVersion,
  configRun, configSandbox, livePoll = true, defaults, onRequestSave, onCopyTo,
}: Props) {
  const [data, setData] = useState<MtfRatioEvalResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const params = draft.days[day]?.zones[zone]?.mtf_ratio;
  // The table is computed for the window the box SHOWS — saved or not — so
  // it can never quietly disagree with the Multi-TF page (24 Sep report).
  const shownWindow = params?.strikes_atm_window;

  // `background: true` = the 30s poll. It must NOT raise the loading flag, or
  // the panel blinks every 30 seconds over data that is already correct.
  const load = useCallback(async (opts?: { background?: boolean }) => {
    if (!opts?.background) setLoading(true);
    try {
      const res = await algoApi.mtfRatioEval({
        day, zone, date: histDate || undefined, expiry: expiry || undefined,
        at: at || undefined, configVersion: configVersion ?? undefined,
        configRun: configRun ?? undefined, configSandbox: configSandbox || undefined,
        atmWindow: shownWindow,
      });
      setData(res);
      setError(null);
    } catch (e) {
      setData(null);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [day, zone, histDate, expiry, at, configVersion, configRun, configSandbox, shownWindow]);

  useEffect(() => {
    void load();
    if (histDate || at || !livePoll) return undefined;
    const t = window.setInterval(() => void load({ background: true }), 30_000);
    return () => window.clearInterval(t);
  }, [load, histDate]);

  const set = useCallback(
    (fn: (p: MtfRatioParams) => void) => {
      mutate((doc) => {
        const p = doc.days[day]?.zones[zone]?.mtf_ratio;
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
          const src = defaults?.days[day]?.zones[zone]?.mtf_ratio;
          if (src) {
            mutate((d) => void (d.days[day].zones[zone].mtf_ratio = JSON.parse(JSON.stringify(src))));
          }
        }}
      />
      {dirty && (
        <CalcNote tone="warn">
          Unsaved rule changes — the evaluation below reflects the <b>saved</b> config. Save to
          apply your edits.
        </CalcNote>
      )}
      {error && <div className="text-xs text-ce">{error}</div>}

      {/* Result strip */}
      {data && (
        <Card
          title="Preview Direction"
          hint={`${data.symbol} · exp ${data.expiry} · strikes ${data.strike_min}–${data.strike_max} · as of ${data.as_of.slice(11, 16)}`}
        >
          <div className="flex items-center gap-5 flex-wrap">
            <span
              className={`text-2xl font-extrabold px-5 py-1.5 rounded-lg border ${
                data.direction === "Call"
                  ? "text-pe bg-pe/10 border-pe/40"
                  : data.direction === "Put"
                    ? "text-ce bg-ce/10 border-ce/40"
                    : "text-gray-300 bg-panel border-border"
              }`}
            >
              {data.direction}
            </span>
            <div className="text-xs text-muted">
              {data.matched_rule ? (
                <>
                  Matched rule: <b className="text-gray-100">{data.matched_rule}</b>
                </>
              ) : (
                "No interpretation has matched — engine reports No Trade."
              )}
              <div className="mt-0.5">
                Filter-layer reading: <b className="text-accent">{data.reading}</b>
              </div>
            </div>
          </div>
        </Card>
      )}

      <div className="grid lg:grid-cols-[1.2fr_1fr] gap-4">
        {/* TF table */}
        <Card
          title="Multi-Timeframe Positions & Ratio"
          hint="Lowest OI Side = lower signed Δ — the side the rules filter on"
        >
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-muted text-left">
                  <th className="py-1.5 pr-2 font-medium">Timeframe</th>
                  <th className="py-1.5 pr-2 font-medium text-right">Call OI Δ</th>
                  <th className="py-1.5 pr-2 font-medium text-right">Put OI Δ</th>
                  <th className="py-1.5 pr-2 font-medium">Call : Put</th>
                  <th className="py-1.5 font-medium">Lowest OI Side</th>
                </tr>
              </thead>
              <tbody>
                {data?.rows.map((r) => (
                  <tr key={r.timeframe} className="border-t border-border/40">
                    <td className="py-1.5 pr-2 font-semibold text-gray-200">
                      {TF_LABEL[r.timeframe] ?? r.timeframe}
                    </td>
                    <td
                      className={`py-1.5 pr-2 text-right font-mono ${
                        r.call_delta_cr > 0 ? "text-pe" : r.call_delta_cr < 0 ? "text-ce" : "text-muted"
                      }`}
                    >
                      {fmtCr(r.call_delta_cr)}
                    </td>
                    <td
                      className={`py-1.5 pr-2 text-right font-mono ${
                        r.put_delta_cr > 0 ? "text-pe" : r.put_delta_cr < 0 ? "text-ce" : "text-muted"
                      }`}
                    >
                      {fmtCr(r.put_delta_cr)}
                    </td>
                    <td className="py-1.5 pr-2 font-mono font-bold">{r.text}</td>
                    <td className={`py-1.5 font-bold ${sideCls(r.lowest_side)}`}>{r.lowest_side}</td>
                  </tr>
                )) ?? (
                  <tr>
                    <td colSpan={5} className="py-3 text-muted">
                      {loading ? <LoadingBlock /> : "No evaluation yet."}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <div className="mt-3 flex items-end gap-3">
            <NumField
              label="Strikes ATM ± (−1 = All)"
              value={params.strikes_atm_window}
              width="w-40"
              onChange={(v) => set((p) => void (p.strikes_atm_window = Math.round(v)))}
            />
            <div className="flex-1">
              <span className="block text-[10px] text-muted mb-1">Timeframe rows computed</span>
              <div className="flex gap-1.5 flex-wrap">
                {ALL_TIMEFRAMES.map((tf) => {
                  const on = params.timeframes.includes(tf);
                  return (
                    <button
                      key={tf}
                      type="button"
                      className={`pill text-[10.5px] ${on ? "pill-active" : ""}`}
                      onClick={() =>
                        set((p) => {
                          p.timeframes = on
                            ? p.timeframes.filter((x) => x !== tf)
                            : [...ALL_TIMEFRAMES.filter((x) => p.timeframes.includes(x) || x === tf)];
                        })
                      }
                    >
                      {TF_LABEL[tf]}
                    </button>
                  );
                })}
              </div>
            </div>
          </div>
        </Card>

        {/* Why this result */}
        <Card title="Why this result?" hint="per-rule evaluation, top to bottom — first full match wins">
          <div className="max-h-96 overflow-y-auto flex flex-col gap-2">
            {data?.traces.map((t) => (
              <div
                key={t.name}
                className={`border rounded-lg px-3 py-2 ${
                  t.matched
                    ? "border-pe/50 bg-pe/5"
                    : t.on
                      ? "border-border"
                      : "border-border opacity-50"
                }`}
              >
                <div className="flex items-center justify-between text-xs">
                  <span className="font-semibold text-gray-100">{t.name}</span>
                  <span className={t.matched ? "text-pe font-bold" : "text-muted"}>
                    {!t.on ? "OFF" : t.matched ? `MATCH → ${t.out}` : "failed"}
                  </span>
                </div>
                {t.on &&
                  t.checks.map((c, i) => (
                    <div
                      key={`${c.timeframe}-${i}`}
                      className={`text-[10.5px] mt-1 ${c.passed ? "text-muted" : "text-ce/90"}`}
                    >
                      {c.passed ? "✓" : "✗"} {c.reason}
                    </div>
                  ))}
              </div>
            )) ?? <div className="text-xs text-muted">No evaluation yet.</div>}
          </div>
        </Card>
      </div>

      {/* Rule builder */}
      <Card
        title="Ratio Direction Conditions"
        hint='"Above" catches capitulation (wider than target), "Below" tight ranges, "Any" pure direction'
      >
        <CalcNote>
          A condition line means: Timeframe | Lowest-OI Side | Operator | Threshold | Call Sign |
          Put Sign — all conditions in a rule must pass (AND). Rules evaluate top to bottom; the
          first 100% match triggers and evaluation stops. The side is matched against the
          LOWEST-OI side (the lower signed Δ); the Operator/Threshold still compare the Call : Put
          ratio.
        </CalcNote>
        <div className="mt-3 flex flex-col gap-3">
          {params.rules.map((rule, ri) => (
            <div key={ri} className="border border-border rounded-xl overflow-hidden">
              <div className="flex items-center gap-2 px-3 py-2 border-b border-border/60 bg-panel/60">
                <Switch
                  on={rule.on}
                  onChange={(v) => set((p) => void (p.rules[ri].on = v))}
                  title="Rule enabled"
                />
                <input
                  className="bg-transparent text-sm font-semibold text-gray-100 focus:outline-none flex-1"
                  value={rule.name}
                  onChange={(e) => set((p) => void (p.rules[ri].name = e.target.value))}
                />
                <span className="text-[10px] text-muted">THEN</span>
                <div className="w-24">
                  <SelectField
                    value={rule.out}
                    options={[
                      { value: "Call", label: "Call" },
                      { value: "Put", label: "Put" },
                      { value: "Neutral", label: "Neutral" },
                    ]}
                    onChange={(v) => set((p) => void (p.rules[ri].out = v))}
                  />
                </div>
                <button
                  type="button"
                  className="text-ce text-xs hover:text-white ml-1"
                  onClick={() => set((p) => void p.rules.splice(ri, 1))}
                >
                  Delete
                </button>
              </div>
              <div className="px-3 py-2">
                <div className="grid grid-cols-[34px_1fr_1fr_1fr_1fr_1fr_1fr_50px] gap-2 text-[9px] text-muted uppercase mb-1">
                  <div />
                  <div>Timeframe</div>
                  <div>Lowest-OI Side</div>
                  <div>Operator</div>
                  <div>Threshold</div>
                  <div>Call Sign</div>
                  <div>Put Sign</div>
                  <div />
                </div>
                {rule.conditions.map((c, ci) => (
                  <ConditionRow
                    key={ci}
                    cond={c}
                    timeframes={params.timeframes}
                    onChange={(patch) =>
                      set((p) => void Object.assign(p.rules[ri].conditions[ci], patch))
                    }
                    onDelete={() => set((p) => void p.rules[ri].conditions.splice(ci, 1))}
                  />
                ))}
                {rule.conditions.length === 0 && (
                  <div className="text-[10.5px] text-amber-300/80 py-1">
                    No conditions — this rule can never match.
                  </div>
                )}
                <button
                  type="button"
                  className="pill text-xs mt-2"
                  onClick={() =>
                    set((p) =>
                      void p.rules[ri].conditions.push({
                        timeframe: p.timeframes[0] ?? "1m",
                        side: "Any",
                        operator: "Any",
                        threshold: "Any",
                        call_sign: "Any",
                        put_sign: "Any",
                      })
                    )
                  }
                >
                  + Add condition
                </button>
              </div>
            </div>
          ))}
        </div>
        <button
          type="button"
          className="pill pill-active text-xs mt-3"
          onClick={() =>
            set((p) =>
              void p.rules.push({
                name: "New Rule",
                on: true,
                out: "Call",
                conditions: [
                  {
                    timeframe: p.timeframes[0] ?? "1m",
                    side: "Any",
                    operator: "Any",
                    threshold: "Any",
                    call_sign: "Any",
                    put_sign: "Any",
                  },
                ],
              })
            )
          }
        >
          + New Interpretation
        </button>
      </Card>
    </div>
  );
}

function ConditionRow(props: {
  cond: MtfCondition;
  timeframes: string[];
  onChange: (patch: Partial<MtfCondition>) => void;
  onDelete: () => void;
}) {
  // (ThresholdField below keeps "Any" + presets AND allows typing any 1:N)
  const { cond } = props;
  const tfs = props.timeframes.length > 0 ? props.timeframes : ["1m"];
  return (
    <div className="grid grid-cols-[34px_1fr_1fr_1fr_1fr_1fr_1fr_50px] gap-2 items-center py-1">
      <div className="text-accent text-[10px] font-bold text-center">AND</div>
      <SelectField
        value={cond.timeframe}
        options={[...new Set([...ALL_TIMEFRAMES, ...tfs, cond.timeframe])].map((t) => ({
          value: t,
          label:
            (TF_LABEL[t] ?? t) + (props.timeframes.includes(t) ? "" : " (not computed)"),
        }))}
        onChange={(v) => props.onChange({ timeframe: v })}
      />
      <SelectField
        value={cond.side}
        options={(["Any", "Call", "Put"] as const).map((v) => ({ value: v, label: v }))}
        onChange={(v) => props.onChange({ side: v })}
      />
      <SelectField
        value={cond.operator}
        options={(["Any", "Above", "Below"] as const).map((v) => ({ value: v, label: v }))}
        onChange={(v) => props.onChange({ operator: v })}
      />
      <ThresholdField
        value={cond.threshold}
        onChange={(v) => props.onChange({ threshold: v })}
      />
      <SelectField
        value={cond.call_sign}
        options={(["Any", "Positive", "Negative"] as const).map((v) => ({
          value: v,
          label: v === "Positive" ? "Positive (+)" : v === "Negative" ? "Negative (−)" : v,
        }))}
        onChange={(v) => props.onChange({ call_sign: v })}
      />
      <SelectField
        value={cond.put_sign}
        options={(["Any", "Positive", "Negative"] as const).map((v) => ({
          value: v,
          label: v === "Positive" ? "Positive (+)" : v === "Negative" ? "Negative (−)" : v,
        }))}
        onChange={(v) => props.onChange({ put_sign: v })}
      />
      <button type="button" className="text-ce text-xs hover:text-white" onClick={props.onDelete}>
        ✕
      </button>
    </div>
  );
}

/** Threshold picker: the preset dropdown (Any, 1:1 … 1:4) PLUS free typing of
 *  any custom ratio as "1:N" (e.g. 1:2.5) — the evaluator parses 1:N
 *  generically, so a typed value behaves exactly like a preset. */
function ThresholdField(props: { value: string; onChange: (v: string) => void }) {
  const isPreset = THRESHOLDS.includes(props.value);
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(props.value);

  const commit = () => {
    const t = text.trim();
    if (/^1:\d+(\.\d+)?$/.test(t)) {
      props.onChange(t);
      setEditing(false);
    }
  };

  if (editing || !isPreset) {
    const valid = /^1:\d+(\.\d+)?$/.test((editing ? text : props.value).trim());
    return (
      <div className="flex items-center gap-1">
        <input
          className={`bg-panel border rounded-lg px-2 py-1.5 text-sm w-full focus:outline-none ${
            valid ? "border-border focus:border-accent" : "border-ce/60"
          }`}
          value={editing ? text : props.value}
          placeholder="1:2.5"
          title='Type any ratio as "1:N" (e.g. 1:2.5), then Enter'
          onChange={(e) => {
            if (!editing) setEditing(true);
            setText(e.target.value);
          }}
          onFocus={() => {
            if (!editing) {
              setEditing(true);
              setText(props.value);
            }
          }}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Enter") commit();
          }}
        />
        <button
          type="button"
          className="text-muted hover:text-gray-200 text-xs"
          title="Back to the preset list"
          onClick={() => {
            props.onChange("Any");
            setEditing(false);
          }}
        >
          ▾
        </button>
      </div>
    );
  }
  return (
    <SelectField
      value={props.value}
      options={[
        ...THRESHOLDS.map((t) => ({ value: t, label: t })),
        { value: "__custom__", label: "custom… (type 1:N)" },
      ]}
      onChange={(v) => {
        if (v === "__custom__") {
          setEditing(true);
          setText("1:");
        } else {
          props.onChange(v);
        }
      }}
    />
  );
}
