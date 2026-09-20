/**
 * Backtesting — a full standalone workspace, mirror of Algo Config but bound
 * to the SANDBOX configuration document.
 *
 * Everything here is safe by construction: the sandbox lives in its own table
 * (never `algo_config_versions`), so nothing done on this page can change
 * what the live orchestrator trades. Runs launched from the sandbox freeze
 * the document (source=inline) and every dashboard/deep-link evaluates under
 * either the SANDBOX draft's saved state (`config_sandbox`) or a specific
 * run's frozen doc (`config_run`).
 *
 * Sub-tabs mirror Algo Config one-for-one: Runs & Results · Daily Trading
 * Config · the four engine dashboards · P&L Summary (run-scoped) · Paper
 * Settings (fill model) · Holiday Calendar · Integrations & Broker ·
 * Security & Audit · Validation.
 */
import { useCallback, useEffect, useState } from "react";
import { algoApi } from "../api/algoRest";
import type { MarketContextValue } from "../hooks/useMarketContext";
import type {
  AlgoConfigDoc,
  AlgoIdentity,
  AuditRow,
  BacktestRunListItem,
  ConfigVersionMeta,
  Weekday,
  ZoneId,
} from "../types/algo";
import { AlgoAdminGate } from "../components/algo/AlgoAdminGate";
import { ConfirmSaveModal } from "../components/algo/ConfirmSaveModal";
import { DailyConfigPanel } from "../components/algo/DailyConfigPanel";
import { ENGINE_TABS, EnginesPanel } from "../components/algo/engines/EnginesPanel";
import type { EngineKey } from "../components/algo/engines/EnginesPanel";
import type { IndicatorKey } from "../types/algo";
import { PnlPanel } from "../components/algo/PnlPanel";
import { ValidationPanel } from "../components/algo/ValidationPanel";
import { BacktestPanel } from "../components/algo/backtest/BacktestPanel";
import { Card, CalcNote, SelectField } from "../components/algo/controls";
import { useConfigDraft } from "../hooks/useConfigDraft";

// Paper Trading, Holiday Calendar and Integrations & Broker were removed from
// THIS workspace on 2026-09-09 (they edit live-trading concerns and the
// Integrations tab talked to the real broker gateway from a sandbox page).
// The underlying sandbox fields (paper balance/slippage, fees, holidays)
// stay load-bearing for every run: edit them in Algo Config and "Copy from
// live", or per run via the New Run form's Simulation settings.
type SubTab =
  | "runs"
  | "daily"
  | "engines"
  | "pnl"
  | "security"
  | "validation";

const SUB_TABS_BEFORE: { id: SubTab; label: string }[] = [
  { id: "runs", label: "Runs & Results" },
  { id: "daily", label: "Daily Trading Config" },
];
export const SUB_TABS_AFTER: { id: SubTab; label: string }[] = [
  { id: "pnl", label: "P&L Summary" },
  { id: "security", label: "Security & Audit" },
  { id: "validation", label: "Validation Summary" },
];

const IND_TO_ENGINE: Record<IndicatorKey, EngineKey> = {
  oi_change: "oi_structure",
  multi_tf: "mtf_ratio",
  ratio: "mqae",
};

async function loadSandbox() {
  const env = await algoApi.sandboxGet();
  return {
    config: env.config,
    meta: { updated_at: env.updated_at, updated_by: env.updated_by },
  };
}

async function saveSandbox(config: AlgoConfigDoc, _note: string): Promise<string> {
  const res = await algoApi.sandboxPut(config);
  return (
    `✔ Sandbox saved (${res.changed_fields} field${res.changed_fields === 1 ? "" : "s"})` +
    (res.warnings.length > 0 ? ` with ${res.warnings.length} warning(s)` : "")
  );
}

export function BacktestingPage({ mc }: { mc: MarketContextValue }) {
  return (
    <AlgoAdminGate>
      {(identity, signOut) => (
        <BacktestingInner mc={mc} identity={identity} signOut={signOut} />
      )}
    </AlgoAdminGate>
  );
}

function BacktestingInner({
  mc,
  identity,
  signOut,
}: {
  mc: MarketContextValue;
  identity: AlgoIdentity;
  signOut: () => void;
}) {
  const [tab, setTab] = useState<SubTab>("runs");
  const [engine, setEngine] = useState<EngineKey>("oi_structure");
  const [engDay, setEngDay] = useState<Weekday>("monday");
  const [engZone, setEngZone] = useState<ZoneId>("Z1");
  const [engExpiry, setEngExpiry] = useState("");
  const [engHistDate, setEngHistDate] = useState("");
  const [engAt, setEngAt] = useState("");
  const [engCfgRun, setEngCfgRun] = useState<number | null>(null);
  const [engStrike, setEngStrike] = useState<number | null>(null);
  const [engOt, setEngOt] = useState<"CE" | "PE" | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);

  const {
    meta, draft, loadError, mutate, dirty, changes, doSave, requestSave,
    discard, showConfirm, setShowConfirm, saveBusy, saveErrors, toast,
    refreshKey, defaults,
  } = useConfigDraft(loadSandbox, saveSandbox);

  const [copyBusy, setCopyBusy] = useState(false);
  const copyFrom = useCallback(
    async (source: "live" | "version", version?: number) => {
      setCopyBusy(true);
      try {
        await algoApi.sandboxCopy(source, version);
        window.location.reload();   // simplest correct refresh of draft+meta
      } finally {
        setCopyBusy(false);
      }
    },
    []
  );

  if (loadError) {
    return (
      <div className="min-h-screen w-full max-w-[1500px] mx-auto px-4 md:px-6 py-6">
        <div className="text-sm text-ce">{loadError}</div>
      </div>
    );
  }
  if (!draft) {
    return (
      <div className="min-h-screen w-full max-w-[1500px] mx-auto px-4 md:px-6 py-6">
        <div className="text-sm text-muted">Loading sandbox configuration…</div>
      </div>
    );
  }

  return (
    <div className="min-h-screen w-full max-w-[1500px] mx-auto px-4 md:px-6 py-3">
      {/* Sticky control bar — same opaque-wrapper rule as Algo Config. */}
      <div className="sticky top-0 z-30 -mx-4 md:-mx-6 -mt-3 px-4 md:px-6 pt-3 pb-3 bg-bg">
        <div className="panel px-4 py-3 flex flex-col gap-2">
          <div className="flex items-center gap-2 flex-wrap">
            {SUB_TABS_BEFORE.map((t) => (
              <button
                key={t.id}
                type="button"
                onClick={() => setTab(t.id)}
                className={`pill ${tab === t.id ? "pill-active" : ""}`}
              >
                {t.label}
              </button>
            ))}
            {ENGINE_TABS.map((e) => (
              <button
                key={e.id}
                type="button"
                onClick={() => {
                  setEngine(e.id);
                  setTab("engines");
                }}
                className={`pill ${tab === "engines" && engine === e.id ? "pill-active" : ""}`}
              >
                {e.label}
              </button>
            ))}
            {SUB_TABS_AFTER.map((t) => (
              <button
                key={t.id}
                type="button"
                onClick={() => setTab(t.id)}
                className={`pill ${tab === t.id ? "pill-active" : ""}`}
              >
                {t.label}
              </button>
            ))}
            <div className="flex-1" />
            <span className="text-[11px] text-muted">{identity.username}</span>
            <button type="button" className="pill text-xs" onClick={signOut}>
              Sign out
            </button>
          </div>
          <div className="flex items-center gap-3 flex-wrap">
            <span className="text-[11px] font-bold text-amber-400">■ SANDBOX CONFIG</span>
            <span className="text-[10.5px] text-muted">
              nothing here can affect live trading · last saved{" "}
              {meta?.updated_at ? meta.updated_at.slice(0, 16).replace("T", " ") : "—"} by{" "}
              {meta?.updated_by ?? "—"}
            </span>
            <button
              type="button"
              className="pill text-xs disabled:opacity-50"
              disabled={copyBusy}
              onClick={() => {
                if (window.confirm("Overwrite the sandbox with the CURRENT LIVE config?")) {
                  void copyFrom("live");
                }
              }}
            >
              {copyBusy ? "Copying…" : "⟳ Copy from live"}
            </button>
            <span className="text-[11px] text-muted">
              {mc.symbolDisplay ?? mc.symbol}
              {mc.liveSpot != null ? ` · ${mc.liveSpot.toFixed(2)}` : ""}
            </span>
            <div className="flex-1" />
            {dirty ? (
              <>
                <span className="text-[11px] text-amber-300">
                  {changes.length} unsaved change{changes.length === 1 ? "" : "s"}
                </span>
                <button type="button" className="pill text-xs" onClick={discard}>
                  Discard
                </button>
                <button
                  type="button"
                  className="pill pill-active text-xs"
                  onClick={requestSave}
                >
                  Save Sandbox…
                </button>
              </>
            ) : (
              <span className="text-[11px] text-muted">All changes saved</span>
            )}
          </div>
        </div>
      </div>

      <div className="pt-4">
        {tab === "runs" && (
          <BacktestPanel
            sandboxDoc={draft}
            sandboxDirty={dirty}
            onSelectRunForPnl={(id) => {
              setSelectedRunId(id);
              setTab("pnl");
            }}
            onOpenEngine={(link) => {
              setEngine(link.engine);
              setEngDay(link.day);
              setEngZone(link.zone);
              setEngExpiry(link.expiry);
              setEngHistDate(link.date);
              setEngAt(link.at);
              setEngCfgRun(link.runId ?? null);
              setEngStrike(link.strike ?? null);
              setEngOt(link.optionType ?? null);
              setTab("engines");
            }}
          />
        )}
        {tab === "daily" && (
          <DailyConfigPanel
            draft={draft}
            mutate={mutate}
            todayWeekday={null}
            defaults={defaults}
            showLiveSnapshot={false}
            onOpenEngine={(ind, day, zone) => {
              setEngine(ind === "ump" ? "ump" : IND_TO_ENGINE[ind]);
              setEngDay(day);
              setEngZone(zone);
              setTab("engines");
            }}
          />
        )}
        {tab === "engines" && (
          <>
            <div className="mb-3">
              <CalcNote>
                {engCfgRun != null
                  ? `Evaluating under backtest run #${engCfgRun}'s frozen config. The editors below edit the SANDBOX draft — Save Sandbox…, then clear the pin to evaluate your edits.`
                  : "Evaluating under the SAVED sandbox config. The editors edit the sandbox draft — Save Sandbox… to apply them to the evaluations, then launch a run from Runs & Results."}
              </CalcNote>
            </div>
            <EnginesPanel
              draft={draft}
              mutate={mutate}
              dirty={dirty}
              engine={engine}
              day={engDay}
              zone={engZone}
              expiry={engExpiry}
              histDate={engHistDate}
              at={engAt}
              configVersion={null}
              configRun={engCfgRun}
              configSandbox={true}
              livePoll={false}
              onDayChange={setEngDay}
              onZoneChange={setEngZone}
              onExpiryChange={setEngExpiry}
              onHistDateChange={setEngHistDate}
              onAtChange={setEngAt}
              onConfigVersionChange={() => setEngCfgRun(null)}
              defaults={defaults}
              onRequestSave={requestSave}
              initialStrike={engStrike}
              initialOptionType={engOt}
            />
          </>
        )}
        {tab === "pnl" &&
          (selectedRunId != null ? (
            <PnlPanel backtestRunId={selectedRunId} />
          ) : (
            <RunPicker onPick={(id) => setSelectedRunId(id)} />
          ))}
        {tab === "security" && (
          <WorkspaceSecurity
            meta={meta}
            onCopyVersion={(v) => {
              if (window.confirm(`Overwrite the sandbox with saved config v${v}?`)) {
                void copyFrom("version", v);
              }
            }}
          />
        )}
        {tab === "validation" && (
          <ValidationPanel refreshKey={refreshKey} document={draft} />
        )}
      </div>

      {showConfirm && (
        <ConfirmSaveModal
          changes={changes}
          busy={saveBusy}
          errors={saveErrors}
          onConfirm={(note) => void doSave(note)}
          onCancel={() => setShowConfirm(false)}
        />
      )}

      {toast && (
        <div className="fixed bottom-5 right-5 z-50 bg-pe/10 border border-pe/40 text-pe text-sm font-semibold px-4 py-2.5 rounded-lg shadow-lg">
          {toast}
        </div>
      )}
    </div>
  );
}

// ── P&L run picker (when no run is selected yet) ────────────────────────────

function RunPicker({ onPick }: { onPick: (id: number) => void }) {
  const [runs, setRuns] = useState<BacktestRunListItem[]>([]);
  useEffect(() => {
    void (async () => {
      try {
        setRuns((await algoApi.backtestRuns()).filter((r) => r.status === "done"));
      } catch {
        /* empty list rendered */
      }
    })();
  }, []);
  return (
    <Card title="Pick a run" hint="the P&L suite renders one backtest run at a time">
      {runs.length === 0 ? (
        <div className="text-sm text-muted">
          No completed runs yet — launch one from Runs & Results.
        </div>
      ) : (
        <div className="flex flex-col gap-1">
          {runs.map((r) => (
            <button
              key={r.id}
              type="button"
              className="text-left border-t border-border/30 first:border-t-0 py-1.5 hover:bg-accent/5 px-2 rounded"
              onClick={() => onPick(r.id)}
            >
              <span className="font-bold text-[12px]">#{r.id}</span>{" "}
              <span className="text-[12px]">{r.label || "—"}</span>{" "}
              <span className="text-[10.5px] text-muted font-mono">
                {r.from_date} → {r.to_date} · {r.trades ?? 0} trades
              </span>
            </button>
          ))}
        </div>
      )}
    </Card>
  );
}

// ── Security & Audit (workspace-flavored, read-only) ────────────────────────

function WorkspaceSecurity({
  meta,
  onCopyVersion,
}: {
  meta: { updated_at: string; updated_by: string } | null;
  onCopyVersion: (v: number) => void;
}) {
  const [versions, setVersions] = useState<ConfigVersionMeta[]>([]);
  const [audit, setAudit] = useState<AuditRow[]>([]);
  const [pick, setPick] = useState("");

  useEffect(() => {
    void (async () => {
      try {
        setVersions(await algoApi.versions(25));
      } catch { /* list stays empty */ }
      try {
        const rows = await algoApi.audit({ limit: 200 });
        setAudit(
          rows.filter(
            (r) =>
              r.event_type.startsWith("backtest") ||
              r.event_type === "config_change"
          )
        );
      } catch { /* table stays empty */ }
    })();
  }, []);

  return (
    <div className="flex flex-col gap-4">
      <Card title="Sandbox Document" hint="the workspace's own config — never a live version">
        <div className="text-sm">
          Last saved{" "}
          <span className="font-mono">
            {meta?.updated_at ? meta.updated_at.slice(0, 19).replace("T", " ") : "—"}
          </span>{" "}
          by <b>{meta?.updated_by ?? "—"}</b>
        </div>
        <div className="mt-3 flex items-end gap-2">
          <div className="w-64">
            <SelectField
              label="Seed the sandbox from a saved LIVE version"
              value={pick}
              options={[
                { value: "", label: "pick a version…" },
                ...versions.map((v) => ({
                  value: String(v.version),
                  label: `v${v.version} · ${v.saved_at.slice(0, 10)}${v.note ? ` · ${v.note.slice(0, 24)}` : ""}`,
                })),
              ]}
              onChange={setPick}
            />
          </div>
          <button
            type="button"
            className="pill text-xs disabled:opacity-50"
            disabled={!pick}
            onClick={() => onCopyVersion(Number(pick))}
          >
            Copy into sandbox
          </button>
        </div>
        <div className="mt-2">
          <CalcNote>
            Version restore for the LIVE config (and the Config Lock) stays in Algo
            Config → Security &amp; Backups — deliberately not reachable from the
            sandbox workspace.
          </CalcNote>
        </div>
      </Card>

      <Card title={`Backtest & Config Audit (${audit.length})`} hint="append-only; sandbox saves, runs, deletions, live config changes">
        <div className="overflow-x-auto max-h-96 overflow-y-auto">
          <table className="w-full text-[10.5px] min-w-[640px]">
            <thead>
              <tr className="text-muted text-left sticky top-0 bg-bg">
                {["Time", "User", "Event", "Scope", "Detail"].map((h) => (
                  <th key={h} className="py-1 pr-2 font-medium">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {audit.map((r, i) => (
                <tr key={i} className="border-t border-border/20">
                  <td className="py-1 pr-2 font-mono">{r.ts.slice(0, 19).replace("T", " ")}</td>
                  <td className="py-1 pr-2">{r.username}</td>
                  <td className="py-1 pr-2">{r.event_type}</td>
                  <td className="py-1 pr-2">{r.scope}</td>
                  <td className="py-1 text-muted">
                    {r.field ? `${r.field}: ${String(r.old_value)} → ${String(r.new_value)}` : ""}
                  </td>
                </tr>
              ))}
              {audit.length === 0 && (
                <tr><td colSpan={5} className="py-2 text-muted">No rows.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}
