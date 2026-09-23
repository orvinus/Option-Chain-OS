/**
 * Decision trace — the per-minute, filter-by-filter record the orchestrator
 * writes for every evaluated minute (live: `algo_decisions`; backtest:
 * `algo_backtest_decisions`). One row = one minute: the stage reached, every
 * gate, each indicator's reading WITH its engine trace (MTF rule checks, OI
 * Structure contributions, MQAE model logs), the candidate strikes with their
 * UMP state, data freshness, and the outcome (accept / reject + reason).
 *
 * Nothing here is recomputed: every value is the backend's own record.
 */
import { useEffect, useState } from "react";
import { algoApi } from "../../api/algoRest";
import type { DecisionIndexRow, DecisionRow } from "../../types/algo";
import { INDICATOR_LABEL } from "../../types/algo";
import { Card } from "./controls";
import { signedCompact } from "../../utils/num";

const DECISION_CLS: Record<string, string> = {
  accept: "text-pe",
  reject: "text-ce",
  manage: "text-amber-300",
  skip: "text-muted",
};

function hhmm(ts: string): string {
  return ts.slice(11, 16);
}

/** Multi-TF crores shown as OI, like the Multi-TF page (2026-09-23). */
function oiUnits(v: unknown): string {
  return typeof v === "number" && Number.isFinite(v) ? signedCompact(Math.round(v * 1e7)) : "—";
}

function num(v: unknown, d = 2): string {
  return typeof v === "number" && Number.isFinite(v) ? v.toFixed(d) : "—";
}

/** One-line human summary of a decision row (shared by the live card and the replay strip). */
export function describeDecision(r: DecisionIndexRow | DecisionRow): string {
  const dir = r.direction && r.direction !== "NO_TRADE" ? ` ${r.direction}` : "";
  return `${r.decision.toUpperCase()}${dir} · ${r.stage.replace(/_/g, " ")}${r.reason ? ` — ${r.reason}` : ""}`;
}

function ReadingDetail({ name, detail }: { name: string; detail: Record<string, unknown> }) {
  const label = INDICATOR_LABEL[name as keyof typeof INDICATOR_LABEL] ?? name;
  const basket = detail.basket as Record<string, unknown> | undefined;
  const rows = detail.rows as Record<string, unknown>[] | undefined;
  const traces = detail.traces as
    | { name: string; on: boolean; matched: boolean; out: string; checks: { timeframe: string; passed: boolean; reason: string }[] }[]
    | undefined;
  const contributions = detail.contributions as { label: string; side: string; points: number }[] | undefined;
  const logs = (["logs_green", "logs_yellow", "logs_cross"] as const)
    .flatMap((k) => ((detail[k] as { model: string; text: string; points: number }[] | undefined) ?? []).map((l) => ({ ...l, lane: k.slice(5) })));
  return (
    <div className="border border-border/60 rounded-lg px-2.5 py-2 text-[10.5px]">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="font-bold text-gray-100">{label}</span>
        <span className={`font-bold ${String(detail.signal) === "CALL" ? "text-pe" : String(detail.signal) === "PUT" ? "text-ce" : "text-muted"}`}>
          {String(detail.signal ?? "—")}
        </span>
        {basket && (
          <span className="text-muted">
            basket {String(basket.strike_min ?? "—")}–{String(basket.strike_max ?? "—")} · spot {num(basket.spot, 1)} ·{" "}
            {String(basket.closed_minutes ?? 0)} closed min · as of {typeof basket.as_of === "string" ? hhmm(basket.as_of) : "—"}
          </span>
        )}
        {typeof detail.why === "string" && <span className="text-amber-300">{detail.why}</span>}
      </div>
      {/* OI Structure */}
      {contributions && (
        <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-muted">
          <span>Call {num(detail.call_pts, 1)} pts · Put {num(detail.put_pts, 1)} pts · conf {String(detail.confidence ?? "—")}</span>
          <span>ΔCE {num(detail.last_call_cr)} Cr · ΔPE {num(detail.last_put_cr)} Cr</span>
          <span>breakout C:{String(detail.call_breakout ?? "—")} P:{String(detail.put_breakout ?? "—")} · red5m C:{String(detail.call_red_5m)} P:{String(detail.put_red_5m)}</span>
          {contributions.map((c, i) => (
            <span key={i} className={c.side === "Call" ? "text-pe" : c.side === "Put" ? "text-ce" : ""}>
              {c.label} {c.side} +{c.points}
            </span>
          ))}
        </div>
      )}
      {/* MTF Ratio */}
      {rows && (
        <div className="mt-1 overflow-x-auto">
          <table className="text-[10px] min-w-[420px]">
            <thead>
              <tr className="text-muted text-left">
                {["TF", "ΔCE OI", "ΔPE OI", "Ratio", "Ratio Side", "Lowest-OI Side (filter)", "signs"].map((h) => (
                  <th key={h} className="pr-2 font-medium">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i}>
                  <td className="pr-2 font-mono">{String(r.timeframe)}</td>
                  <td className="pr-2 font-mono">{oiUnits(r.call_delta_cr)}</td>
                  <td className="pr-2 font-mono">{oiUnits(r.put_delta_cr)}</td>
                  <td className="pr-2 font-mono">{String(r.text)}</td>
                  <td className={`pr-2 font-bold ${r.side === "Call" ? "text-pe" : r.side === "Put" ? "text-ce" : "text-muted"}`}>{String(r.side)}</td>
                  <td className={`pr-2 ${r.lowest_side === "Call" ? "text-pe" : r.lowest_side === "Put" ? "text-ce" : "text-muted"}`}>{String(r.lowest_side)}</td>
                  <td className="pr-2 text-muted">{String(r.call_sign)}/{String(r.put_sign)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {traces && (
            <div className="mt-1 flex flex-col gap-0.5">
              {traces.map((t) => (
                <div key={t.name} className={t.matched ? "text-pe" : t.on ? "text-muted" : "text-muted/50"}>
                  {t.matched ? "✔" : t.on ? "✗" : "○"} {t.name} → {t.out}
                  {t.checks.length > 0 && (
                    <span className="ml-2">
                      {t.checks.map((c, i) => (
                        <span key={i} className={c.passed ? "text-pe/80" : "text-ce/80"}>
                          [{c.timeframe} {c.passed ? "pass" : "fail"}: {c.reason}]{" "}
                        </span>
                      ))}
                    </span>
                  )}
                </div>
              ))}
              {typeof detail.matched_rule === "string" && (
                <div className="text-gray-200">matched rule: {detail.matched_rule}</div>
              )}
            </div>
          )}
        </div>
      )}
      {/* MQAE */}
      {logs.length > 0 && (
        <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-muted">
          <span>
            total {num(detail.total, 1)} · green {num(detail.score_green, 1)} · yellow {num(detail.score_yellow, 1)} · cross {num(detail.score_cross, 1)}
          </span>
          <span>PCR {num(detail.last_green_pcr, 3)} · Ratio {num(detail.last_yellow_ratio, 3)}</span>
          {logs.map((l, i) => (
            <span key={i} className={l.points > 0 ? "text-pe" : l.points < 0 ? "text-ce" : ""}>
              {l.lane}/{l.model}: {l.text} ({l.points > 0 ? "+" : ""}{l.points})
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function RowDetail({ row }: { row: DecisionRow }) {
  const readings = (row.readings ?? {}) as Record<string, Record<string, unknown>>;
  const cands = (row.candidates ?? []) as Record<string, unknown>[];
  const zone = (row.zone_snapshot ?? null) as Record<string, unknown> | null;
  const sizing = (row.sizing ?? null) as Record<string, unknown> | null;
  const pos = (row.position ?? null) as Record<string, unknown> | null;
  return (
    <div className="flex flex-col gap-2 py-2">
      {(row.gate_blocks?.length ?? 0) > 0 && (
        <div className="text-[10.5px] text-amber-300">gates: {row.gate_blocks!.join(" · ")}</div>
      )}
      {Object.entries(readings).map(([k, d]) => (
        <ReadingDetail key={k} name={k} detail={d} />
      ))}
      {cands.length > 0 && (
        <div className="border border-border/60 rounded-lg px-2.5 py-2 text-[10.5px]">
          <div className="font-bold text-gray-100 mb-1">Candidates ({cands.length})</div>
          <table className="text-[10px]">
            <thead>
              <tr className="text-muted text-left">
                {["Strike", "Side", "bar", "close", "in band", "UMP", "trig", "levels", "trail SL", "fired"].map((h) => (
                  <th key={h} className="pr-2 font-medium">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {cands.map((c, i) => {
                const u = (c.ump ?? {}) as Record<string, unknown>;
                return (
                  <tr key={i} className={c.fired ? "text-pe font-bold" : ""}>
                    <td className="pr-2 font-mono">{String(c.strike)}{String(c.option_type)}</td>
                    <td className="pr-2">{String(c.side)}</td>
                    <td className="pr-2">{c.bar_present ? "✔" : "—"}</td>
                    <td className="pr-2 font-mono">{num(c.last_close)}</td>
                    <td className={`pr-2 ${c.in_band ? "text-pe" : "text-ce"}`}>{c.in_band ? "yes" : "no"}</td>
                    <td className="pr-2">{String(u.state ?? "—")}{u.sub ? ` ${String(u.sub)}` : ""}</td>
                    <td className="pr-2">{u.trig ? "armed" : "—"}</td>
                    <td className="pr-2">{u.levels_ready ? "ready" : "warming"}</td>
                    <td className="pr-2 font-mono">{num(u.trail_sl)}</td>
                    <td className="pr-2">{c.fired ? "FIRED" : ""}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-[10.5px] text-muted">
        {row.data_age_s != null && <span>data age {Math.round(row.data_age_s)} s</span>}
        {row.unanimous != null && <span>unanimous: {row.unanimous ? "yes" : "no"}</span>}
        {row.config_version != null && <span>config v{row.config_version}</span>}
        {row.ledger && <span>ledger {row.ledger}</span>}
        {sizing && (
          <span>
            sizing: alloc ₹{num(sizing.allocated, 0)} · lot {String(sizing.lot_size)} · entry {num(sizing.raw_entry)} → {String(sizing.lots)} lot(s)
            {sizing.fill != null ? ` · fill ${num(sizing.fill)}` : ""}
          </span>
        )}
        {pos && (
          <span>
            position: {String(pos.side)} {String(pos.contract)} × {String(pos.lots)} @ {num(pos.entry)} · trail {num(pos.trail_sl)} · maxSL {num(pos.max_sl)}
          </span>
        )}
        {zone && (
          <span>
            zone {String(zone.start)}–{String(zone.end)} · band ₹{String(zone.premium_min)}–₹{String(zone.premium_max)} ·{" "}
            {((zone.enabled_indicators as string[] | undefined) ?? []).join("+") || "no indicators"}
            {/* Which rule counted those votes. Stored per decision, so a row
                stays readable after the setting is changed. */}
            {zone.combine_rule != null && (
              <> ({String(zone.combine_rule)}, neutral {String(zone.neutral_mode ?? "block")})</>
            )}{" "}
            · scan {String(zone.strike_scan_count)} · hold {String(zone.direction_hold_min)}m
          </span>
        )}
        {row.trade_id != null && <span>trade #{row.trade_id}</span>}
      </div>
    </div>
  );
}

export function DecisionTraceCard({
  rows, title = "Decision Trace", hint, emptyText, loadFull,
}: {
  rows: (DecisionIndexRow | DecisionRow)[];
  title?: string;
  hint?: string;
  emptyText?: string;
  /** Compact rows expand by fetching the full record on demand. */
  loadFull?: (row: DecisionIndexRow) => Promise<DecisionRow | null>;
}) {
  const [filter, setFilter] = useState<"all" | "reject" | "accept" | "candidates">("all");
  const [open, setOpen] = useState<number | null>(null);
  const [full, setFull] = useState<Record<number, DecisionRow>>({});

  const shown = rows.filter((r) =>
    filter === "all" ? true :
    filter === "candidates" ? ("n_candidates" in r ? (r.n_candidates ?? 0) > 0 : ((r as DecisionRow).candidates?.length ?? 0) > 0) :
    r.decision === filter,
  );

  const toggle = async (r: DecisionIndexRow | DecisionRow) => {
    if (open === r.id) {
      setOpen(null);
      return;
    }
    setOpen(r.id);
    if (!("readings" in r) && loadFull && !full[r.id]) {
      const f = await loadFull(r as DecisionIndexRow);
      if (f) setFull((m) => ({ ...m, [r.id]: f }));
    }
  };

  return (
    <Card title={title} hint={hint}>
      <div className="flex items-center gap-2 mb-2 text-[10.5px]">
        {(["all", "reject", "accept", "candidates"] as const).map((f) => (
          <button
            key={f}
            type="button"
            className={`pill text-[10.5px] ${filter === f ? "pill-active" : ""}`}
            onClick={() => setFilter(f)}
          >
            {f === "candidates" ? "with candidates" : f}
          </button>
        ))}
        <span className="text-muted ml-auto">{shown.length} minute(s)</span>
      </div>
      {shown.length === 0 && (
        <div className="text-[11px] text-muted">{emptyText ?? "No decision rows yet."}</div>
      )}
      {shown.length > 0 && (
        <div className="overflow-x-auto max-h-96 overflow-y-auto">
          <table className="w-full text-[10.5px] min-w-[640px]">
            <thead>
              <tr className="text-muted text-left sticky top-0 bg-bg">
                {["Time", "Zone", "Stage", "Dir", "Readings", "Cands", "Age", "Decision", "Reason"].map((h) => (
                  <th key={h} className="py-1 pr-2 font-medium">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {shown.map((r) => {
                const fullRow = "readings" in r ? (r as DecisionRow) : full[r.id];
                const nC = "n_candidates" in r ? r.n_candidates ?? 0 : ((r as DecisionRow).candidates?.length ?? 0);
                const readings = fullRow?.readings ? Object.entries(fullRow.readings) : [];
                return (
                  <>
                    <tr
                      key={r.id}
                      onClick={() => void toggle(r)}
                      className={`border-t border-border/20 cursor-pointer hover:bg-accent/5 ${open === r.id ? "bg-accent/10" : ""}`}
                    >
                      <td className="py-1 pr-2 font-mono">{hhmm(r.ts)}</td>
                      <td className="py-1 pr-2">{r.zone_id || "—"}</td>
                      <td className="py-1 pr-2">{r.stage.replace(/_/g, " ")}</td>
                      <td className={`py-1 pr-2 font-bold ${r.direction === "CALL" ? "text-pe" : r.direction === "PUT" ? "text-ce" : "text-muted"}`}>
                        {r.direction || "—"}
                      </td>
                      <td className="py-1 pr-2">
                        {readings.length > 0
                          ? readings.map(([k, v]) => (
                              <span key={k} className={`mr-1 ${String((v as Record<string, unknown>).signal) === "CALL" ? "text-pe" : String((v as Record<string, unknown>).signal) === "PUT" ? "text-ce" : "text-muted"}`}>
                                {(INDICATOR_LABEL[k as keyof typeof INDICATOR_LABEL] ?? k).slice(0, 8)}:{String((v as Record<string, unknown>).signal)}
                              </span>
                            ))
                          : "—"}
                      </td>
                      <td className="py-1 pr-2 text-right">{nC || "—"}</td>
                      <td className="py-1 pr-2 text-right">{r.data_age_s != null ? `${Math.round(r.data_age_s)}s` : "—"}</td>
                      <td className={`py-1 pr-2 font-bold ${DECISION_CLS[r.decision] ?? ""}`}>{r.decision}</td>
                      <td className="py-1 text-muted">{r.reason || "—"}</td>
                    </tr>
                    {open === r.id && (
                      <tr key={`${r.id}-d`} className="bg-panel/40">
                        <td colSpan={9} className="px-2">
                          {fullRow ? <RowDetail row={fullRow} /> : <div className="text-[10.5px] text-muted py-2">loading…</div>}
                        </td>
                      </tr>
                    )}
                  </>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

/** Live card: today's rows for the active zone, polled with the snapshot. */
export function LiveDecisionTrace({ zone }: { zone: string }) {
  const [rows, setRows] = useState<DecisionRow[]>([]);
  const zoneId = zone.includes("/") ? zone.split("/")[1] : zone;
  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const res = await algoApi.decisions({ zone: zoneId || undefined, limit: 120 });
        if (alive) setRows(res.rows);
      } catch {
        if (alive) setRows([]);
      }
    };
    void load();
    const t = window.setInterval(() => void load(), 5000);
    return () => {
      alive = false;
      window.clearInterval(t);
    };
  }, [zoneId]);
  return (
    <DecisionTraceCard
      rows={rows}
      title="Decision Trace (today)"
      hint="one row per evaluated minute — click a row for every filter's verdict"
      emptyText="No decisions recorded today yet (the orchestrator writes one row per evaluated minute while the session is open)."
    />
  );
}
