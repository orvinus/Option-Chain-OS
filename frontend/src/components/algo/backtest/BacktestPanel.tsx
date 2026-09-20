/**
 * Sub-tab — Backtesting: run the ENTIRE live trading pipeline (all four
 * engines, unanimous rule, premium-band strike pick, UMP entries/exits,
 * paper fills + fees, risk kills, End-Exit) over the stored 6-month
 * 1-minute archive, day by day, under any saved configuration.
 *
 * Three views: the runs list + new-run form (with the data-integrity
 * preflight), a run's detail (progress / summary / equity curve / full P&L
 * reuse), and the per-day visual replay (BacktestDayReplay).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactECharts from "echarts-for-react";
import { AlgoApiError, algoApi } from "../../../api/algoRest";
import type {
  BacktestCoverage,
  AlgoConfigDoc,
  BacktestPreflight,
  BacktestRun,
  BacktestRunListItem,
  BacktestRunRequest,
  ConfigVersionMeta,
} from "../../../types/algo";
import { UMP_SCENARIOS } from "../../../types/algo";
import { CalcNote, Card, DateField, NumField, SelectField, ToggleLine } from "../controls";
import { PnlPanel } from "../PnlPanel";
import { ExportDialog } from "../ExportDialog";
import { BacktestDayReplay } from "./BacktestDayReplay";
import type { EngineDeepLink } from "./BacktestDayReplay";
import {
  EMPTY_OVERRIDES,
  PRESETS,
  applyOverrides,
  describeOverrides,
  overridesActive,
} from "./overrides";
import type { RunOverrides } from "./overrides";

type View = { kind: "list" } | { kind: "run"; id: number } | { kind: "day"; id: number; date: string };

const STATUS_CLS: Record<string, string> = {
  running: "text-amber-300",
  queued: "text-amber-300",
  done: "text-pe",
  error: "text-ce",
  cancelled: "text-muted",
};

function rupee(n: number | null | undefined): string {
  if (n == null) return "—";
  const sign = n > 0 ? "+" : "";
  return `${sign}₹${Math.round(n).toLocaleString("en-IN")}`;
}

function pnlCls(n: number | null | undefined): string {
  if (n == null || n === 0) return "text-muted";
  return n > 0 ? "text-pe" : "text-ce";
}

function ProgressBar({ done, total }: { done: number; total: number }) {
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  return (
    <div className="flex items-center gap-2 w-full">
      <div className="flex-1 h-2.5 rounded-full bg-panel border border-border overflow-hidden">
        <div
          className="h-full bg-accent transition-all duration-500"
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="text-[10.5px] text-muted whitespace-nowrap">
        {done}/{total} days · {pct}%
      </span>
    </div>
  );
}

export function BacktestPanel({
  onOpenEngine,
  sandboxDoc,
  sandboxDirty,
  onSelectRunForPnl,
}: {
  onOpenEngine: (l: EngineDeepLink) => void;
  /** The Backtesting workspace's SANDBOX draft — enables the "Sandbox config"
   *  run source (submitted inline, frozen into the run). */
  sandboxDoc?: AlgoConfigDoc | null;
  sandboxDirty?: boolean;
  /** Workspace hook: "use this run in the P&L Summary tab". */
  onSelectRunForPnl?: (id: number) => void;
}) {
  const [view, setView] = useState<View>({ kind: "list" });

  if (view.kind === "day") {
    return (
      <BacktestDayReplay
        runId={view.id}
        date={view.date}
        onBack={() => setView({ kind: "run", id: view.id })}
        onOpenEngine={onOpenEngine}
      />
    );
  }
  if (view.kind === "run") {
    return (
      <RunDetail
        runId={view.id}
        onBack={() => setView({ kind: "list" })}
        onOpenDay={(date) => setView({ kind: "day", id: view.id, date })}
        onSelectRunForPnl={onSelectRunForPnl}
      />
    );
  }
  return (
    <RunsList
      onOpenRun={(id) => setView({ kind: "run", id })}
      sandboxDoc={sandboxDoc}
      sandboxDirty={sandboxDirty}
    />
  );
}

// ─────────────────────────────────────────────────────────────── runs list

function RunsList({
  onOpenRun,
  sandboxDoc,
  sandboxDirty,
}: {
  onOpenRun: (id: number) => void;
  sandboxDoc?: AlgoConfigDoc | null;
  sandboxDirty?: boolean;
}) {
  const [runs, setRuns] = useState<BacktestRunListItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);

  const load = useCallback(async () => {
    try {
      setRuns(await algoApi.backtestRuns());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
    const t = window.setInterval(() => void load(), 5_000);
    return () => window.clearInterval(t);
  }, [load]);

  return (
    <div className="flex flex-col gap-4">
      <div className="panel px-4 py-3 flex items-center gap-3 flex-wrap">
        <span className="text-[12.5px] font-bold text-gray-100">
          Backtesting — replay the whole engine over stored history
        </span>
        <div className="flex-1" />
        <button
          type="button"
          className="pill pill-active text-xs"
          onClick={() => setShowForm((v) => !v)}
        >
          {showForm ? "Hide form" : "＋ New Backtest Run"}
        </button>
      </div>
      {error && <div className="text-sm text-ce">{error}</div>}

      {showForm && (
        <NewRunForm
          onStarted={(id) => onOpenRun(id)}
          sandboxDoc={sandboxDoc}
          sandboxDirty={sandboxDirty}
        />
      )}

      <Card title={`Runs (${runs.length})`} hint="click a run to open its results">
        <div className="overflow-x-auto">
          <table className="w-full text-[11px] min-w-[760px]">
            <thead>
              <tr className="text-muted text-left">
                {["#", "Label", "Range", "Config", "Status", "Progress", "Trades", "Net P&L", "Created"].map((h) => (
                  <th key={h} className="py-1.5 pr-2 font-medium">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr
                  key={r.id}
                  className="border-t border-border/30 cursor-pointer hover:bg-accent/5"
                  onClick={() => onOpenRun(r.id)}
                >
                  <td className="py-1.5 pr-2">{r.id}</td>
                  <td className="py-1.5 pr-2">{r.label || "—"}</td>
                  <td className="py-1.5 pr-2 font-mono">{r.from_date} → {r.to_date}</td>
                  <td className="py-1.5 pr-2">{r.config_version != null ? `v${r.config_version}` : "inline"}</td>
                  <td className={`py-1.5 pr-2 font-bold ${STATUS_CLS[r.status] ?? ""}`}>{r.status}</td>
                  <td className="py-1.5 pr-2 w-48">
                    <ProgressBar done={r.days_done} total={r.days_total} />
                  </td>
                  <td className="py-1.5 pr-2 text-right">{r.trades ?? "—"}</td>
                  <td className={`py-1.5 pr-2 text-right font-bold ${pnlCls(r.net_pnl)}`}>{rupee(r.net_pnl)}</td>
                  <td className="py-1.5 text-muted">{r.created_at.slice(0, 16).replace("T", " ")}</td>
                </tr>
              ))}
              {runs.length === 0 && (
                <tr>
                  <td colSpan={9} className="py-3 text-muted">
                    No backtest runs yet — create one above. Stored data and its
                    exact bounds are shown on the run form.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────── new-run form

function NewRunForm({
  onStarted,
  sandboxDoc,
  sandboxDirty,
}: {
  onStarted: (id: number) => void;
  sandboxDoc?: AlgoConfigDoc | null;
  sandboxDirty?: boolean;
}) {
  const [label, setLabel] = useState("");
  // Seeded from GET /coverage once it lands. Previously these were a
  // hardcoded "2026-02-05" and new Date().toISOString() — the latter is UTC,
  // so before 05:30 IST the To field silently showed YESTERDAY.
  const [coverage, setCoverage] = useState<BacktestCoverage | null>(null);
  const [fromDate, setFromDate] = useState("");
  const [toDate, setToDate] = useState("");
  const seededRef = useRef(false);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const c = await algoApi.backtestCoverage();
        if (!alive) return;
        setCoverage(c);
        // Seed ONCE — a later refetch must never stomp typed dates.
        if (!seededRef.current) {
          seededRef.current = true;
          if (c.default_from) setFromDate(c.default_from);
          if (c.default_to) setToDate(c.default_to);
        }
      } catch {
        /* endpoint unavailable — the fields stay editable, just unseeded */
      }
    })();
    return () => {
      alive = false;
    };
  }, []);
  const [configSource, setConfigSource] = useState<"sandbox" | "live" | "version">(
    sandboxDoc ? "sandbox" : "live"
  );
  const [version, setVersion] = useState<string>("");
  const [versions, setVersions] = useState<ConfigVersionMeta[]>([]);
  const [balanceMode, setBalanceMode] = useState<"compounding" | "fixed_per_day">("compounding");
  const [startingBalance, setStartingBalance] = useState(0);   // 0 = config's virtual balance
  const [minMinutes, setMinMinutes] = useState(300);
  const [useDefaults, setUseDefaults] = useState(true);
  const [carryPause, setCarryPause] = useState(false);
  const [symFilter, setSymFilter] = useState<"all" | "NIFTY" | "SENSEX">("all");
  const [excludedText, setExcludedText] = useState("");
  // Simulation settings (null = the base document's value). The base values
  // are read from the sandbox document when present, else the live config.
  const [simSlippage, setSimSlippage] = useState<number | null>(null);
  const [simBrokerage, setSimBrokerage] = useState<number | null>(null);
  const [recordDecisions, setRecordDecisions] = useState(true);
  const [baseSim, setBaseSim] = useState<{
    slippage_pct: number; fill_source: string; brokerage_per_order: number;
    fees: Record<string, number>; overnight_carry: boolean; holidays: number;
  } | null>(null);
  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const doc = sandboxDoc ?? (await algoApi.getConfig()).config;
        if (!alive) return;
        const fees = doc.global.fees as unknown as Record<string, number>;
        setBaseSim({
          slippage_pct: doc.global.paper.slippage_pct,
          fill_source: doc.global.paper.fill_source,
          brokerage_per_order: fees.brokerage_per_order,
          fees,
          overnight_carry: doc.global.overnight_carry,
          holidays: doc.global.holidays.length,
        });
      } catch {
        /* the block just shows "—" */
      }
    })();
    return () => {
      alive = false;
    };
  }, [sandboxDoc]);
  const [preflight, setPreflight] = useState<BacktestPreflight | null>(null);
  const [busy, setBusy] = useState<"" | "preflight" | "start" | "suite">("");
  const [error, setError] = useState<string | null>(null);
  const [presetKey, setPresetKey] = useState("baseline");
  const [ov, setOv] = useState<RunOverrides>({ ...EMPTY_OVERRIDES });

  const setOverride = useCallback(<K extends keyof RunOverrides>(k: K, v: RunOverrides[K]) => {
    setPresetKey("custom");
    setOv((prev) => ({ ...prev, [k]: v }));
  }, []);

  const pickPreset = useCallback((key: string) => {
    setPresetKey(key);
    const p = PRESETS.find((x) => x.key === key);
    if (p) setOv({ ...p.o });
  }, []);

  useEffect(() => {
    void (async () => {
      try {
        setVersions(await algoApi.versions(50));
      } catch {
        /* version picker just stays empty */
      }
    })();
  }, []);

  const buildRequest = useCallback(
    async (opts?: {
      overrides?: RunOverrides;
      labelSuffix?: string;
      queue?: boolean;
      presetName?: string;
    }): Promise<BacktestRunRequest> => {
      const o = opts?.overrides ?? ov;
      const excluded = excludedText
        .split(/[\s,]+/)
        .map((s) => s.trim())
        .filter((s) => /^\d{4}-\d{2}-\d{2}$/.test(s));

      // Overrides transform a copy of the BASE document → the run submits
      // source:"inline" and freezes exactly what was asked for.
      let config: BacktestRunRequest["config"];
      if (overridesActive(o)) {
        let base: AlgoConfigDoc;
        if (configSource === "sandbox" && sandboxDoc) base = sandboxDoc;
        else if (configSource === "version" && version)
          base = (await algoApi.version(Number(version))).config;
        else base = (await algoApi.getConfig()).config;
        config = { source: "inline", document: applyOverrides(base, o) };
      } else {
        config =
          configSource === "sandbox" && sandboxDoc
            ? { source: "inline", document: sandboxDoc }
            : configSource === "version" && version
              ? { source: "version", version: Number(version) }
              : { source: "live" };
      }

      return {
        label: label + (opts?.labelSuffix ?? ""),
        from_date: fromDate,
        to_date: toDate,
        config,
        settings: {
          balance_mode: balanceMode,
          starting_balance: startingBalance > 0 ? startingBalance : undefined,
          symbols: symFilter === "all" ? [] : [symFilter],
          excluded_days: excluded,
          use_default_exclusions: useDefaults,
          min_minutes_per_day: minMinutes,
          carry_pause: carryPause,
          sim:
            simSlippage != null || simBrokerage != null
              ? { slippage_pct: simSlippage, brokerage_per_order: simBrokerage }
              : null,
          record_decisions: recordDecisions,
        },
        queue: opts?.queue ?? false,
        overrides: overridesActive(o)
          ? { preset: opts?.presetName ?? presetKey, list: describeOverrides(o) }
          : null,
      };
    },
    [label, fromDate, toDate, configSource, version, balanceMode, startingBalance,
     excludedText, useDefaults, minMinutes, carryPause, symFilter, sandboxDoc, ov,
     presetKey, simSlippage, simBrokerage, recordDecisions]
  );

  const runPreflight = useCallback(async () => {
    setBusy("preflight");
    setError(null);
    try {
      setPreflight(await algoApi.backtestPreflight(await buildRequest()));
    } catch (e) {
      setError(e instanceof AlgoApiError ? e.message : String(e));
    } finally {
      setBusy("");
    }
  }, [buildRequest]);

  const start = useCallback(async () => {
    setBusy("start");
    setError(null);
    try {
      const res = await algoApi.backtestCreate(await buildRequest({ queue: true }));
      onStarted(res.id);
    } catch (e) {
      setError(e instanceof AlgoApiError ? e.message : String(e));
    } finally {
      setBusy("");
    }
  }, [buildRequest, onStarted]);

  const runSuite = useCallback(async () => {
    setBusy("suite");
    setError(null);
    try {
      const stamp = new Date().toTimeString().slice(0, 5);
      let firstId: number | null = null;
      for (const p of PRESETS) {
        const req = await buildRequest({
          overrides: p.o,
          labelSuffix: `${label ? " " : ""}[suite ${stamp} · ${p.label}]`,
          queue: true,
          presetName: p.key,
        });
        const res = await algoApi.backtestCreate(req);
        if (firstId == null) firstId = res.id;
      }
      if (firstId != null) onStarted(firstId);
    } catch (e) {
      setError(e instanceof AlgoApiError ? e.message : String(e));
    } finally {
      setBusy("");
    }
  }, [buildRequest, label, onStarted]);

  return (
    <Card title="New Backtest Run" hint="preflight first — it shows exactly which days will run and why any are skipped">
      <div className="flex flex-col gap-3">
        <div className="flex items-end gap-3 flex-wrap">
          <label className="block w-56">
            <span className="block text-[10px] text-muted mb-1">Label</span>
            <input
              className="bg-panel border border-border rounded-lg px-2 py-1.5 text-sm w-full focus:outline-none focus:border-accent"
              placeholder="e.g. Feb–Aug baseline"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
            />
          </label>
          <div className="w-44">
            <DateField label="From" value={fromDate} onChange={setFromDate}
              min={coverage?.bounds.min ?? undefined} max={coverage?.bounds.max ?? undefined} />
          </div>
          <div className="w-44">
            <DateField label="To" value={toDate} onChange={setToDate}
              min={coverage?.bounds.min ?? undefined} max={coverage?.bounds.max ?? undefined} />
          </div>
          <div className="w-44">
            <SelectField
              label="Configuration"
              value={configSource}
              options={[
                ...(sandboxDoc
                  ? [{ value: "sandbox" as const, label: "Sandbox config (this workspace)" }]
                  : []),
                { value: "live" as const, label: "Current live config" },
                { value: "version" as const, label: "A saved version…" },
              ]}
              onChange={(v) => setConfigSource(v)}
            />
          </div>
          {configSource === "version" && (
            <div className="w-56">
              <SelectField
                label="Version"
                value={version}
                options={[
                  { value: "", label: "pick a version" },
                  ...versions.map((v) => ({
                    value: String(v.version),
                    label: `v${v.version} · ${v.saved_at.slice(0, 10)}${v.note ? ` · ${v.note.slice(0, 24)}` : ""}`,
                  })),
                ]}
                onChange={setVersion}
              />
            </div>
          )}
        </div>

        <div className="flex items-end gap-3 flex-wrap">
          <div className="w-48">
            <SelectField
              label="Balance mode"
              value={balanceMode}
              options={[
                { value: "compounding", label: "Compounding (live-paper parity)" },
                { value: "fixed_per_day", label: "Fixed per day" },
              ]}
              onChange={(v) => setBalanceMode(v)}
            />
          </div>
          <NumField
            label="Starting balance (0 = config's virtual balance)"
            value={startingBalance}
            onChange={setStartingBalance}
            step={1000}
            suffix="₹"
          />
          <NumField
            label="Min minutes/day"
            value={minMinutes}
            onChange={(v) => setMinMinutes(Math.max(1, Math.min(385, Math.round(v))))}
            step={10}
          />
          <SelectField
            label="Index filter"
            value={symFilter}
            options={[
              { value: "all", label: "all configured indices" },
              { value: "NIFTY", label: "NIFTY days only" },
              { value: "SENSEX", label: "SENSEX days only" },
            ]}
            onChange={(v) => setSymFilter(v as "all" | "NIFTY" | "SENSEX")}
          />
        </div>

        {/* ── Simulation settings (the knobs the simulator actually consumes) ── */}
        <div className="border border-border rounded-xl px-3 py-2">
          <div className="flex items-center gap-3 flex-wrap mb-2">
            <span className="text-[11px] font-bold text-gray-200 uppercase">Simulation settings</span>
            <span className="text-[10px] text-muted">
              applied to the run's frozen config · blank = base document's value
            </span>
          </div>
          <div className="flex items-end gap-3 flex-wrap">
            <NumField
              label={`Slippage % per side (base ${baseSim ? baseSim.slippage_pct : "—"})`}
              value={simSlippage ?? baseSim?.slippage_pct ?? 0}
              onChange={(v) => setSimSlippage(Math.max(0, Math.min(10, v)))}
              step={0.1}
              suffix="%"
            />
            <NumField
              label={`Brokerage ₹/order (base ${baseSim ? baseSim.brokerage_per_order : "—"})`}
              value={simBrokerage ?? baseSim?.brokerage_per_order ?? 0}
              onChange={(v) => setSimBrokerage(Math.max(0, Math.min(1000, v)))}
              step={1}
              suffix="₹"
            />
            <button
              type="button"
              className="pill text-[10.5px]"
              disabled={simSlippage == null && simBrokerage == null}
              onClick={() => { setSimSlippage(null); setSimBrokerage(null); }}
            >
              use base values
            </button>
            <ToggleLine
              name="Record decision trace"
              desc="one row per simulated minute (filter-by-filter verdicts) — powers the replay's Decision Trace"
              on={recordDecisions}
              onChange={setRecordDecisions}
            />
          </div>
          <CalcNote>
            Fill model: {baseSim?.fill_source ?? "ltp"} at the closed-minute close ± slippage (the only
            modelled source; latency is not simulated). Statutory charges (STT{" "}
            {baseSim?.fees.stt_sell_premium_pct ?? "—"}%, exchange {baseSim?.fees.exchange_txn_pct ?? "—"}%,
            SEBI/IPFT/GST/stamp) and overnight carry ({baseSim ? String(baseSim.overnight_carry) : "—"}) come
            from the base document; holidays: {baseSim?.holidays ?? "—"} in config + the platform file (frozen
            into the run). Edit those in Algo Config, then "Copy from live" into the sandbox.
          </CalcNote>
        </div>

        {/* ── Scenario & Overrides ── */}
        <div className="border border-amber-500/30 rounded-xl px-3 py-2">
          <div className="flex items-center gap-3 flex-wrap mb-2">
            <span className="text-[11px] font-bold text-amber-300 uppercase">Scenario &amp; Overrides</span>
            <div className="w-64">
              <SelectField
                value={PRESETS.some((p) => p.key === presetKey) ? presetKey : "custom"}
                options={[
                  ...PRESETS.map((p) => ({ value: p.key, label: p.label })),
                  { value: "custom", label: "Custom…" },
                ]}
                onChange={pickPreset}
              />
            </div>
            <span className="text-[10.5px] text-muted">
              {PRESETS.find((p) => p.key === presetKey)?.desc ??
                "hand-picked overrides below"}
            </span>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <SelectField
              label="Indicators (all zones)"
              value={ov.indicators == null ? "keep" : ov.indicators.join("+") || "none"}
              options={[
                { value: "keep", label: "as configured" },
                { value: "oi_change", label: "OI Change only" },
                { value: "multi_tf", label: "Multi-TF only" },
                { value: "ratio", label: "Ratio only" },
                { value: "oi_change+multi_tf+ratio", label: "all three (unanimous)" },
              ]}
              onChange={(v) =>
                setOverride(
                  "indicators",
                  v === "keep" ? null : (v.split("+") as RunOverrides["indicators"])
                )
              }
            />
            <SelectField
              label="Re-eval cadence"
              value={ov.cadence ?? "keep"}
              options={[
                { value: "keep", label: "as configured" },
                { value: "zone_start", label: "once at zone start" },
                { value: "every_candle", label: "every candle" },
              ]}
              onChange={(v) => setOverride("cadence", v === "keep" ? null : (v as RunOverrides["cadence"]))}
            />
            <SelectField
              label="Direction-hold (experimental)"
              value={ov.directionHoldMin == null ? "keep" : String(ov.directionHoldMin)}
              options={[
                { value: "keep", label: "as configured" },
                { value: "0", label: "off (strict §5.3)" },
                { value: "5", label: "5 min" },
                { value: "10", label: "10 min" },
                { value: "15", label: "15 min" },
                { value: "30", label: "30 min" },
                { value: "60", label: "60 min" },
              ]}
              onChange={(v) => setOverride("directionHoldMin", v === "keep" ? null : Number(v))}
            />
            <SelectField
              label="Zone layout"
              value={ov.singleWideZone ? "wide" : "keep"}
              options={[
                { value: "keep", label: "as configured" },
                { value: "wide", label: "one wide zone 09:20–15:10" },
              ]}
              onChange={(v) => setOverride("singleWideZone", v === "wide")}
            />
            <NumField
              label="Band min ₹ (0 = keep)"
              value={ov.premiumMin ?? 0}
              onChange={(v) => setOverride("premiumMin", v > 0 ? v : null)}
            />
            <NumField
              label="Band max ₹ (0 = keep)"
              value={ov.premiumMax ?? 0}
              onChange={(v) => setOverride("premiumMax", v > 0 ? v : null)}
            />
            <NumField
              label="Max trades/zone (0 = keep)"
              value={ov.maxTrades ?? 0}
              onChange={(v) => setOverride("maxTrades", v > 0 ? Math.round(v) : null)}
            />
            <SelectField
              label="Strike scan (multi-strike)"
              value={ov.strikeScanCount == null ? "keep" : String(ov.strikeScanCount)}
              options={[
                { value: "keep", label: "as configured" },
                { value: "1", label: "1 (single-strike, original)" },
                { value: "2", label: "top 2" },
                { value: "3", label: "top 3" },
                { value: "5", label: "top 5" },
              ]}
              onChange={(v) => setOverride("strikeScanCount", v === "keep" ? null : Number(v))}
            />
            <SelectField
              label="Allocation"
              value={
                ov.allocationPct == null
                  ? "keep"
                  : ov.allocationPct > 100
                    ? "allin"
                    : String(ov.allocationPct)
              }
              options={[
                { value: "keep", label: "as configured" },
                { value: "25", label: "25%" },
                { value: "50", label: "50%" },
                { value: "90", label: "90%" },
                { value: "allin", label: "ALL-IN (100%)" },
              ]}
              onChange={(v) =>
                setOverride("allocationPct", v === "keep" ? null : v === "allin" ? 101 : Number(v))
              }
            />
            <SelectField
              label="UMP long entries"
              value={ov.umpEnableLong == null ? "keep" : ov.umpEnableLong ? "on" : "off"}
              options={[
                { value: "keep", label: "as configured" },
                { value: "on", label: "ON" },
                { value: "off", label: "OFF" },
              ]}
              onChange={(v) => setOverride("umpEnableLong", v === "keep" ? null : v === "on")}
            />
            <SelectField
              label="UMP retest entries"
              value={ov.umpEnableRetest == null ? "keep" : ov.umpEnableRetest ? "on" : "off"}
              options={[
                { value: "keep", label: "as configured" },
                { value: "on", label: "ON" },
                { value: "off", label: "OFF" },
              ]}
              onChange={(v) => setOverride("umpEnableRetest", v === "keep" ? null : v === "on")}
            />
            <SelectField
              label="UMP entry timeframe"
              value={ov.umpEntryTimeframeMin == null ? "keep" : String(ov.umpEntryTimeframeMin)}
              options={[
                { value: "keep", label: "as configured" },
                { value: "5", label: "5-Minute" },
                { value: "15", label: "15-Minute" },
              ]}
              onChange={(v) => setOverride("umpEntryTimeframeMin", v === "keep" ? null : (Number(v) as 5 | 15))}
            />
            <SelectField
              label="Day kills"
              value={ov.clearDayKills ? "clear" : "keep"}
              options={[
                { value: "keep", label: "as configured" },
                { value: "clear", label: "clear all (every day trades)" },
              ]}
              onChange={(v) => setOverride("clearDayKills", v === "clear")}
            />
          </div>
          <details className="mt-2 border border-border/60 rounded-lg px-3 py-1.5">
            <summary className="text-[10.5px] font-bold text-muted uppercase cursor-pointer select-none">
              UMP engine overrides — the 9 Pine numerics the engine consumes (0 = keep)
            </summary>
            <div className="grid grid-cols-2 md:grid-cols-3 gap-3 mt-2 pb-1">
              <NumField
                label="🏛️ Expansion law %"
                value={ov.umpExpansionLawPct ?? 0}
                step={0.5}
                onChange={(v) => setOverride("umpExpansionLawPct", v > 0 ? v : null)}
              />
              <NumField
                label="🏛️ Bridge law %"
                value={ov.umpBridgeLawPct ?? 0}
                step={0.5}
                onChange={(v) => setOverride("umpBridgeLawPct", v > 0 ? v : null)}
              />
              <NumField
                label="🏛️ Median law %"
                value={ov.umpMedianLawPct ?? 0}
                step={0.5}
                onChange={(v) => setOverride("umpMedianLawPct", v > 0 ? v : null)}
              />
              <NumField
                label="🏛️ Price floor ₹"
                value={ov.umpPriceFloorInr ?? 0}
                step={5}
                onChange={(v) => setOverride("umpPriceFloorInr", v > 0 ? v : null)}
              />
              <NumField
                label="🔍 Body match pts"
                value={ov.umpBodyMatchPts ?? 0}
                step={0.1}
                onChange={(v) => setOverride("umpBodyMatchPts", v > 0 ? v : null)}
              />
              <NumField
                label="🔍 Scan depth bars"
                value={ov.umpScanDepthBars ?? 0}
                step={10}
                onChange={(v) => setOverride("umpScanDepthBars", v > 0 ? Math.round(v) : null)}
              />
              <NumField
                label="🎨 Zone width %"
                value={ov.umpZoneWidthPct ?? 0}
                step={0.5}
                onChange={(v) => setOverride("umpZoneWidthPct", v > 0 ? v : null)}
              />
              <NumField
                label="📐 Max SL %"
                value={ov.umpMaxSlPct ?? 0}
                step={0.5}
                onChange={(v) => setOverride("umpMaxSlPct", v > 0 ? v : null)}
              />
              <NumField
                label="📐 Trigger timeout (bars of entry TF)"
                value={ov.umpTriggerTimeoutBars ?? 0}
                onChange={(v) => setOverride("umpTriggerTimeoutBars", v > 0 ? Math.round(v) : null)}
              />
            </div>
            <div className="mt-2">
              <div className="text-[10px] text-muted mb-1">
                UMP entry kinds switched OFF in every zone (click to toggle; none = as configured)
              </div>
              <div className="flex flex-wrap gap-1">
                {UMP_SCENARIOS.map((k) => {
                  const off = (ov.umpScenariosOff ?? []).includes(k);
                  return (
                    <button
                      key={k}
                      type="button"
                      className={`pill text-[10px] ${off ? "line-through opacity-60" : ""}`}
                      onClick={() => {
                        const cur = ov.umpScenariosOff ?? [];
                        const next = off ? cur.filter((x) => x !== k) : [...cur, k];
                        setOverride("umpScenariosOff", next.length ? next : null);
                      }}
                    >
                      {k}
                    </button>
                  );
                })}
              </div>
            </div>
          </details>
          {overridesActive(ov) && (
            <div className="text-[10.5px] text-amber-300 mt-2">
              Overrides → {describeOverrides(ov).join(" · ")}
              <button
                type="button"
                className="pill text-[10px] ml-2"
                onClick={() => pickPreset("baseline")}
              >
                clear
              </button>
            </div>
          )}
          {coverage && (
            <div className="mt-1">
              <CalcNote tone={coverage.stale ? "warn" : "info"}>
                Stored data:{" "}
                <b>
                  {coverage.bounds.min ?? "—"} → {coverage.bounds.max ?? "—"}
                </b>
                {coverage.stats_refreshed_at
                  ? ` · index refreshed ${coverage.stats_refreshed_at.slice(0, 16).replace("T", " ")}`
                  : ""}
                {coverage.stale && coverage.last_trading_day ? (
                  <>
                    {" "}
                    — <b>the {coverage.last_trading_day} session has not landed yet</b>, so
                    it cannot be backtested. The nightly TrueData top-up fills it; until
                    then the To date is clamped to what actually exists.
                  </>
                ) : (
                  " — the range extends automatically as new days land."
                )}
              </CalcNote>
            </div>
          )}
          <div className="mt-1">
            <CalcNote>
              Overrides apply to EVERY day and zone on top of the chosen base
              config, and the run freezes the result. Fine-grained editing of
              every parameter (each engine, each zone) lives in the tabs above
              — this form runs whatever you set there, plus these overrides.
            </CalcNote>
          </div>
        </div>

        <div className="grid md:grid-cols-2 gap-3">
          <div className="flex flex-col gap-1">
            <ToggleLine
              name="Skip catalogued bad-data days"
              desc="the operator-verified OI-collapse days (vendor data damage) stay out of the results"
              on={useDefaults}
              onChange={setUseDefaults}
            />
            <ToggleLine
              name="Carry the consecutive-loss pause across days"
              desc="OFF (default): each day auto-resumes, like a morning manual resume"
              on={carryPause}
              onChange={setCarryPause}
            />
          </div>
          <label className="block">
            <span className="block text-[10px] text-muted mb-1">
              Extra excluded days (YYYY-MM-DD, comma/space separated)
            </span>
            <textarea
              className="bg-panel border border-border rounded-lg px-2 py-1.5 text-sm w-full h-16 focus:outline-none focus:border-accent resize-y"
              placeholder="2026-03-12, 2026-04-01"
              value={excludedText}
              onChange={(e) => setExcludedText(e.target.value)}
            />
          </label>
        </div>

        {configSource === "sandbox" && sandboxDirty && (
          <div className="text-[11px] text-amber-300">
            ⚠ The sandbox has UNSAVED edits — the run will use the draft exactly
            as shown in the editor tabs (including those edits).
          </div>
        )}
        {error && <div className="text-xs text-ce whitespace-pre-wrap">{error}</div>}

        <div className="flex items-center gap-2">
          <button
            type="button"
            className="pill text-xs disabled:opacity-50"
            disabled={busy !== ""}
            onClick={() => void runPreflight()}
          >
            {busy === "preflight" ? "Checking…" : "Run Preflight"}
          </button>
          <button
            type="button"
            className="pill pill-active text-xs disabled:opacity-50"
            disabled={busy !== "" || (configSource === "version" && !version)}
            onClick={() => void start()}
          >
            {busy === "start" ? "Starting…" : "▶ Start Backtest"}
          </button>
          <button
            type="button"
            className="pill text-xs disabled:opacity-50"
            disabled={busy !== "" || (configSource === "version" && !version)}
            onClick={() => void runSuite()}
            title="Queue every scenario preset over this base + dates; they run one after another"
          >
            {busy === "suite" ? "Queueing…" : `🧪 Run scenario suite (${PRESETS.length})`}
          </button>
          <span className="text-[10px] text-muted">
            runs execute one at a time; extra runs QUEUE and start automatically
          </span>
        </div>

        {preflight && (
          <div className="border-t border-border/40 pt-3 flex flex-col gap-2">
            <div className="text-[11.5px]">
              <b className="text-pe">{preflight.planned}</b> day(s) will run,{" "}
              <b className="text-amber-300">{preflight.skipped}</b> skipped.
            </div>
            {preflight.warnings.length > 0 && (
              <ul className="text-[10.5px] text-amber-300 list-disc pl-4">
                {preflight.warnings.map((w, i) => (
                  <li key={i}>{w}</li>
                ))}
              </ul>
            )}
            <div className="overflow-x-auto max-h-64 overflow-y-auto">
              <table className="w-full text-[10.5px] min-w-[560px]">
                <thead>
                  <tr className="text-muted text-left sticky top-0 bg-bg">
                    {["Date", "Symbol", "Expiry", "Minutes", "Spot", "Verdict"].map((h) => (
                      <th key={h} className="py-1 pr-2 font-medium">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {preflight.days.map((d) => (
                    <tr key={d.trade_date} className="border-t border-border/20">
                      <td className="py-1 pr-2 font-mono">{d.trade_date}</td>
                      <td className="py-1 pr-2">{d.symbol}</td>
                      <td className="py-1 pr-2 font-mono">{d.expiry ?? "—"}</td>
                      <td className="py-1 pr-2 text-right">{d.minutes || "—"}</td>
                      <td className="py-1 pr-2">{d.spotless ? "⚠ none" : "✓"}</td>
                      <td className={`py-1 ${d.planned ? "text-pe" : "text-amber-300"}`}>
                        {d.planned ? "run" : d.skip_reason}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        <CalcNote>
          The backtest drives the SAME orchestrator code the live engine runs — engines,
          unanimous rule, premium-band strike selection, UMP execution, paper fills with
          slippage, the full fee model and every risk kill — over the archived 1-minute
          data. Fills are 1-minute paper simulations; real-world queue/liquidity effects
          are not modelled. Lot sizes are today's values (frozen into the run).
        </CalcNote>
      </div>
    </Card>
  );
}

// ─────────────────────────────────────────────────────────────── run detail

function RunDetail({
  runId, onBack, onOpenDay, onSelectRunForPnl,
}: {
  runId: number;
  onBack: () => void;
  onOpenDay: (date: string) => void;
  onSelectRunForPnl?: (id: number) => void;
}) {
  const [run, setRun] = useState<BacktestRun | null>(null);
  const [days, setDays] = useState<import("../../../types/algo").BacktestDayRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState("");
  const [exportOpen, setExportOpen] = useState(false);

  const load = useCallback(async () => {
    try {
      const [r, d] = await Promise.all([
        algoApi.backtestRun(runId),
        algoApi.backtestDays(runId),
      ]);
      setRun(r);
      setDays(d);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [runId]);

  useEffect(() => {
    void load();
  }, [load]);

  // Poll while the run is active.
  useEffect(() => {
    if (!run || (run.status !== "running" && run.status !== "queued")) return undefined;
    const t = window.setInterval(() => void load(), 2_500);
    return () => window.clearInterval(t);
  }, [run, load]);

  const act = useCallback(
    async (what: "cancel" | "resume" | "delete") => {
      setBusy(what);
      try {
        if (what === "cancel") await algoApi.backtestCancel(runId);
        if (what === "resume") await algoApi.backtestResume(runId);
        if (what === "delete") {
          await algoApi.backtestDelete(runId);
          onBack();
          return;
        }
        await load();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy("");
      }
    },
    [runId, load, onBack]
  );

  const equityOption = useMemo(() => {
    const s = run?.summary;
    // While a run is RUNNING its summary holds only the preflight — the
    // aggregate fields land at completion. Guard every access.
    if (!s || !Array.isArray(s.equity) || s.equity.length === 0) return null;
    return {
      animation: false,
      grid: { left: 64, right: 16, top: 18, bottom: 26 },
      tooltip: { trigger: "axis" },
      xAxis: {
        type: "category",
        data: s.equity.map((p) => p.date),
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
          type: "line",
          data: s.equity.map((p) => p.equity),
          showSymbol: false,
          lineStyle: { width: 2, color: "#38bdf8" },
          areaStyle: { color: "rgba(56,189,248,0.08)" },
          markLine: {
            symbol: "none",
            silent: true,
            data: [{ yAxis: s.starting_balance, lineStyle: { type: "dashed", color: "#94a3b8" },
                     label: { formatter: "start", fontSize: 9 } }],
          },
        },
      ],
    };
  }, [run]);

  if (error && !run) {
    return (
      <div className="flex flex-col gap-3">
        <button type="button" className="pill text-xs self-start" onClick={onBack}>← All runs</button>
        <div className="text-sm text-ce">{error}</div>
      </div>
    );
  }
  if (!run) return <div className="text-sm text-muted">Loading run…</div>;

  const s = run.summary;
  // "Complete" = the aggregates exist (a running run's summary is preflight-only).
  const complete = s != null && Array.isArray(s.equity) && s.trades != null;
  const active = run.status === "running" || run.status === "queued";

  return (
    <div className="flex flex-col gap-4">
      <div className="panel px-4 py-3 flex flex-col gap-2">
        <div className="flex items-center gap-3 flex-wrap">
          <button type="button" className="pill text-xs" onClick={onBack}>← All runs</button>
          <span className="text-[12.5px] font-bold text-gray-100">
            Run #{run.id} {run.label ? `— ${run.label}` : ""}
          </span>
          <span className="text-[10.5px] text-muted font-mono">
            {run.from_date} → {run.to_date}
            {run.config_version != null ? ` · config v${run.config_version}` : ""}
          </span>
          <span className={`text-[11px] font-bold ${STATUS_CLS[run.status] ?? ""}`}>
            {run.status.toUpperCase()}
          </span>
          <div className="flex-1" />
          {active && (
            <button type="button" className="pill text-xs" disabled={busy !== ""} onClick={() => void act("cancel")}>
              {busy === "cancel" ? "Cancelling…" : "■ Cancel"}
            </button>
          )}
          {(run.status === "error" || run.status === "cancelled") && (
            <button type="button" className="pill text-xs" disabled={busy !== ""} onClick={() => void act("resume")}>
              {busy === "resume" ? "Resuming…" : "↻ Resume"}
            </button>
          )}
          {onSelectRunForPnl && run.status === "done" && (
            <button
              type="button"
              className="pill text-xs"
              onClick={() => onSelectRunForPnl(runId)}
              title="Open the workspace's P&L Summary tab focused on this run"
            >
              Use in P&L tab ↗
            </button>
          )}
          {!active && (
            <button
              type="button"
              className="pill text-xs"
              onClick={() => setExportOpen(true)}
              title="Export this run's trades, decisions, signals, days, equity, summary or settings for its date range — CSV or Excel"
            >
              Export…
            </button>
          )}
          {!active && (
            <button type="button" className="pill text-xs text-ce" disabled={busy !== ""} onClick={() => void act("delete")}>
              {busy === "delete" ? "Deleting…" : "Delete run"}
            </button>
          )}
        </div>
        <ProgressBar done={run.days_done} total={run.days_total} />
        {run.status === "running" && run.cursor_date && (
          <div className="text-[10.5px] text-muted">simulating {run.cursor_date}…</div>
        )}
        {run.error && <div className="text-[11px] text-ce">{run.error}</div>}
        {error && <div className="text-[11px] text-ce">{error}</div>}
      </div>
      {exportOpen && (
        <ExportDialog
          mode={{ kind: "backtest", runId: run.id, from_date: run.from_date, to_date: run.to_date }}
          onClose={() => setExportOpen(false)}
        />
      )}

      <RunSettingsCard run={run} />

      {complete && s && (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <StatTile label="Net P&L" value={rupee(s.net_pnl)} cls={pnlCls(s.net_pnl)} />
            <StatTile
              label="Final Equity"
              value={`₹${Math.round(s.final_equity).toLocaleString("en-IN")}`}
              sub={`from ₹${Math.round(s.starting_balance).toLocaleString("en-IN")}`}
            />
            <StatTile
              label="Win Rate"
              value={s.win_rate != null ? `${s.win_rate}%` : "—"}
              sub={`${s.wins}W / ${s.losses}L of ${s.trades}`}
            />
            <StatTile
              label="Max Drawdown"
              value={`−₹${Math.round(s.max_drawdown).toLocaleString("en-IN")} (${s.max_drawdown_pct}%)`}
              cls="text-ce"
              sub={s.max_drawdown_from ? `${s.max_drawdown_from} → ${s.max_drawdown_to}` : undefined}
            />
            <StatTile label="Profit Factor" value={s.profit_factor != null ? String(s.profit_factor) : "—"} />
            <StatTile label="Total Fees" value={`₹${Math.round(s.fees_total).toLocaleString("en-IN")}`} />
            <StatTile
              label="Days"
              value={`${s.days_done} run · ${s.days_skipped} skipped`}
              sub={`${s.forced_eod_closes} EOD force-close · ${s.spotless_days} spot-less`}
            />
            <StatTile
              label="Avg Win / Loss"
              value={`${rupee(s.avg_win)} / ${rupee(s.avg_loss)}`}
            />
          </div>

          {s.funnel && (
            <Card
              title="Entry Funnel — why (not) more trades"
              hint="direction → hunt → trigger → entry, across every simulated minute"
            >
              <div className="grid grid-cols-2 md:grid-cols-5 gap-3 mb-2">
                <StatTile
                  label="Days with direction"
                  value={`${s.funnel.days_with_direction}/${s.funnel.days_counted}`}
                  sub={`${s.funnel.direction_minutes} unanimous minutes`}
                />
                <StatTile
                  label="Hunts started"
                  value={String(s.funnel.hunts_started)}
                  sub={`avg life ${s.funnel.avg_hunt_life_min ?? "—"} min`}
                />
                <StatTile
                  label="Hunts discarded"
                  value={String(s.funnel.hunts_discarded)}
                  sub={s.funnel.hold_minutes > 0 ? `${s.funnel.hold_minutes} hold-minutes saved` : "flicker / zone end"}
                />
                <StatTile
                  label="Trigger-armed minutes"
                  value={String(s.funnel.trigger_armed_minutes)}
                  sub={`${s.funnel.band_blocked_minutes} band-blocked min`}
                />
                <StatTile
                  label="Entries"
                  value={String(s.funnel.entries)}
                  cls={s.funnel.entries > 0 ? "text-pe" : "text-ce"}
                />
              </div>
              <CalcNote>
                {s.funnel.hunts_started > 10 && (s.funnel.avg_hunt_life_min ?? 99) < 5
                  ? "Hunts die fast — the unanimous signal flickers before the UMP's 5-minute machinery can complete a setup. Try zone-start cadence, a single indicator, or direction-hold in the New-Run overrides."
                  : s.funnel.hunts_started === 0
                    ? "No hunts — the enabled indicators never agreed inside a zone window. Loosen indicators/zones in the overrides."
                    : "Hunts live long enough — entry frequency is governed by the UMP engine's own setup requirements (trigger over level, scenarios, retest)."}
              </CalcNote>
            </Card>
          )}

          {equityOption && (
            <Card title="Equity Curve" hint={`${String(run.settings.balance_mode ?? "compounding")} · day-close equity`}>
              <ReactECharts option={equityOption} style={{ height: 260 }} notMerge />
            </Card>
          )}
        </>
      )}

      {/* per-day audit + entry into the visual replay */}
      <Card
        title={`Days (${days.length})`}
        hint="click any day to open its minute-by-minute visual replay"
      >
        <div className="overflow-x-auto max-h-80 overflow-y-auto">
          <table className="w-full text-[10.5px] min-w-[700px]">
            <thead>
              <tr className="text-muted text-left sticky top-0 bg-bg">
                {["Date", "Status", "Symbol", "Expiry", "Trades", "Net", "Fees", "Equity", "Flags / Skip reason"].map((h) => (
                  <th key={h} className="py-1 pr-2 font-medium">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {days.map((d) => {
                const det = d.detail ?? {};
                const clickable = d.status === "done";
                return (
                  <tr
                    key={d.trade_date}
                    onClick={() => clickable && onOpenDay(d.trade_date)}
                    className={`border-t border-border/20 ${clickable ? "cursor-pointer hover:bg-accent/5" : "opacity-60"}`}
                  >
                    <td className="py-1 pr-2 font-mono">{d.trade_date}</td>
                    <td className={`py-1 pr-2 font-bold ${
                      d.status === "done" ? "text-pe" : d.status === "error" ? "text-ce" : "text-amber-300"
                    }`}>{d.status}</td>
                    <td className="py-1 pr-2">{det.symbol ?? "—"}</td>
                    <td className="py-1 pr-2 font-mono">{det.expiry ?? "—"}</td>
                    <td className="py-1 pr-2 text-right">{det.trades ?? "—"}</td>
                    <td className={`py-1 pr-2 text-right font-bold ${pnlCls(det.net)}`}>
                      {det.net != null ? rupee(det.net) : "—"}
                    </td>
                    <td className="py-1 pr-2 text-right">{det.fees != null ? `₹${det.fees}` : "—"}</td>
                    <td className="py-1 pr-2 text-right">
                      {det.equity_after != null ? `₹${Math.round(det.equity_after).toLocaleString("en-IN")}` : "—"}
                    </td>
                    <td className="py-1 text-muted">
                      {[
                        d.skip_reason,
                        det.spotless ? "spot-less" : "",
                        det.forced_eod_close ? "EOD force-close" : "",
                        det.paused_reason ? `paused: ${det.paused_reason}` : "",
                        (det.gaps?.length ?? 0) > 0
                          ? `gaps: ${det.gaps!.map((g) => `${g[0]}–${g[1]}`).join(", ")}`
                          : "",
                      ].filter(Boolean).join(" · ") || "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>

      {/* the full P&L suite over this run — same components as the live tab */}
      {complete && s && s.trades > 0 && <PnlPanel backtestRunId={runId} />}
      {complete && s && s.trades === 0 && run.status === "done" && (
        <CalcNote tone="warn">
          The run completed with ZERO trades — the unanimous rule, premium band or risk
          gates never let an entry through under this configuration. Open any day's
          replay to watch exactly why (signals rarely aligned, band empty, etc.).
        </CalcNote>
      )}
    </div>
  );
}

/** Every setting the run was launched with — the frozen truth, not the form. */
function RunSettingsCard({ run }: { run: BacktestRun }) {
  const st = (run.settings ?? {}) as Record<string, unknown>;
  const ov = st.overrides as { preset?: string; list?: string[] } | null | undefined;
  const sim = st.sim_effective as Record<string, unknown> | undefined;
  const fees = (sim?.fees ?? {}) as Record<string, unknown>;
  const lots = (st.lot_sizes ?? {}) as Record<string, number>;
  const steps = (st.strike_steps ?? {}) as Record<string, number>;
  const excluded = (st.excluded_days ?? []) as string[];
  const items: [string, string][] = [
    ["Config", run.config_version != null ? `saved v${run.config_version}` : `inline (${ov ? "overrides" : "sandbox"})`],
    ["Balance mode", String(st.balance_mode ?? "compounding")],
    ["Starting balance", st.starting_balance != null ? `₹${Math.round(Number(st.starting_balance)).toLocaleString("en-IN")}` : "—"],
    ["Min minutes/day", String(st.min_minutes_per_day ?? 300)],
    ["Index filter", ((st.symbols as string[] | undefined) ?? []).join(", ") || "all"],
    ["Catalogued bad days", st.use_default_exclusions === false ? "included" : "skipped"],
    ["Extra excluded days", excluded.length ? excluded.join(", ") : "none"],
    ["Suspect-tz days", st.allow_suspect_tz ? "allowed" : "skipped"],
    ["Carry loss-pause", st.carry_pause ? "yes" : "no"],
    ["Lot sizes", Object.entries(lots).map(([k, v]) => `${k}=${v}`).join(", ") || "—"],
    ["Strike steps", Object.entries(steps).map(([k, v]) => `${k}=${v}`).join(", ") || "—"],
    ["Slippage / fill", sim ? `${String(sim.slippage_pct)}% · ${String(sim.fill_source)}` : "—"],
    ["Brokerage", fees.brokerage_per_order != null ? `₹${String(fees.brokerage_per_order)}/order` : "—"],
    ["Overnight carry", sim ? String(sim.overnight_carry) : "—"],
    ["Holidays", sim ? `${String(sim.config_holidays)} config + ${String(sim.platform_holidays)} platform` : "—"],
    ["Decision trace", st.record_decisions === false ? "off" : "on"],
    ["Overrides", ov ? `${ov.preset ?? "custom"}: ${(ov.list ?? []).join("; ") || "—"}` : "none"],
    ["Created", `${run.created_by} · ${run.created_at.slice(0, 16).replace("T", " ")}`],
  ];
  return (
    <Card title="Settings used" hint="frozen at launch — what this run actually simulated">
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-x-4 gap-y-1 text-[10.5px]">
        {items.map(([k, v]) => (
          <div key={k} className="flex flex-col">
            <span className="text-[9.5px] text-muted uppercase tracking-wide">{k}</span>
            <span className="text-gray-200 break-words">{v}</span>
          </div>
        ))}
      </div>
    </Card>
  );
}


function StatTile(props: { label: string; value: string; cls?: string; sub?: string }) {
  return (
    <div className="bg-panel/60 border border-border rounded-lg px-3 py-2">
      <div className="text-[9.5px] text-muted uppercase tracking-wide">{props.label}</div>
      <div className={`text-base font-bold ${props.cls ?? "text-gray-100"}`}>{props.value}</div>
      {props.sub && <div className="text-[9.5px] text-muted mt-0.5">{props.sub}</div>}
    </div>
  );
}
