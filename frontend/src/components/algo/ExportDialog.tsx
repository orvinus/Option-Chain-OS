/**
 * §6 Export dialog — full-day and custom-range exports for the paper/live
 * ledgers and for a backtest run. Rows are streamed server-side (no row cap):
 * CSV downloads straight from `/api/algo/export/*`; XLSX is built client-side
 * from the NDJSON stream with the existing `downloadXlsx`. Datasets are
 * fetched one after another so several selections never race the browser's
 * download handling.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { algoApi, type ExportQuery, type ExportScope } from "../../api/algoRest";
import type { ExportDataset } from "../../types/algo";
import { downloadXlsx } from "../../utils/exportData";
import { CalcNote, DateField, SelectField } from "./controls";

export type ExportDialogMode =
  | { kind: "ledger"; ledger: "paper" | "live" }
  | { kind: "backtest"; runId: number; from_date?: string; to_date?: string };

type Preset = "today" | "week" | "month" | "custom" | "run";
type Format = "csv" | "xlsx";

/** Above this many rows Excel gets slow — warn. */
const XLSX_WARN_ROWS = 50_000;
/** Above this many rows the sheet writer is not attempted — CSV only. */
const XLSX_MAX_ROWS = 200_000;
/** Server-side range cap (mirrors the backend validation). */
const MAX_RANGE_DAYS = 400;

const LEDGER_DATASETS: { id: ExportDataset; label: string; hint: string }[] = [
  { id: "trades", label: "Trades", hint: "one row per fill — entry/exit, lots, P&L, fee breakdown, held minutes" },
  { id: "decisions", label: "Decisions", hint: "one row per evaluated minute per zone — readings, gates, hunt state (≈375 rows/zone/day)" },
  { id: "signals", label: "Signals", hint: "indicator reading transitions (algo_signals)" },
  { id: "calendar", label: "Calendar", hint: "net ₹ + % per trading day" },
  { id: "summary", label: "Summary", hint: "period aggregates, by-day / by-zone tables" },
];

const BACKTEST_DATASETS: { id: ExportDataset; label: string; hint: string }[] = [
  { id: "trades", label: "Trades", hint: "every simulated fill in the run" },
  { id: "decisions", label: "Decisions", hint: "minute-by-minute decision trace (large)" },
  { id: "signals", label: "Signals", hint: "indicator transitions recorded by the run" },
  { id: "days", label: "Days", hint: "per-day status, net/fees/gross, equity after" },
  { id: "equity", label: "Equity", hint: "day-level equity curve" },
  { id: "summary", label: "Summary", hint: "run aggregates incl. max drawdown" },
  { id: "settings", label: "Settings", hint: "the frozen config the run used" },
];

function istToday(): Date {
  // IST = UTC+5:30 — the calendar day the exchange is on right now.
  return new Date(Date.now() + 5.5 * 3600 * 1000);
}
function isoUtc(d: Date): string {
  return d.toISOString().slice(0, 10);
}
function presetRange(p: Preset): { from: string; to: string } {
  const t = istToday();
  const to = isoUtc(t);
  if (p === "today") return { from: to, to };
  if (p === "week") {
    const dow = (t.getUTCDay() + 6) % 7; // Monday = 0
    const mon = new Date(t.getTime() - dow * 86_400_000);
    return { from: isoUtc(mon), to };
  }
  if (p === "month") return { from: `${to.slice(0, 7)}-01`, to };
  return { from: to, to };
}
function daysBetween(a: string, b: string): number {
  return Math.round((Date.parse(b) - Date.parse(a)) / 86_400_000) + 1;
}

interface Props {
  mode: ExportDialogMode;
  onClose: () => void;
}

export function ExportDialog({ mode, onClose }: Props) {
  const isBacktest = mode.kind === "backtest";
  const datasets = isBacktest ? BACKTEST_DATASETS : LEDGER_DATASETS;

  // A backtest opened from the P&L tab may only know the run id — fetch the
  // range so "Run range" and the custom clamp are correct.
  const [runRange, setRunRange] = useState<{ from: string; to: string } | null>(
    mode.kind === "backtest" && mode.from_date && mode.to_date
      ? { from: mode.from_date, to: mode.to_date }
      : null
  );
  useEffect(() => {
    if (mode.kind !== "backtest" || runRange) return;
    let cancelled = false;
    void (async () => {
      try {
        const r = await algoApi.backtestRun(mode.runId);
        if (!cancelled) setRunRange({ from: r.from_date, to: r.to_date });
      } catch {
        /* keep the custom picker usable without a clamp */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [mode, runRange]);

  const [preset, setPreset] = useState<Preset>(isBacktest ? "run" : "today");
  const [custom, setCustom] = useState(() => presetRange("today"));
  const [selected, setSelected] = useState<Set<ExportDataset>>(() => new Set(["trades"]));
  const [format, setFormat] = useState<Format>("csv");
  const [zone, setZone] = useState("");
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<string | null>(null);
  const [log, setLog] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  const range = useMemo(() => {
    if (preset === "run") return runRange ?? null;
    if (preset === "custom") return custom;
    return presetRange(preset);
  }, [preset, runRange, custom]);

  const rangeProblem = useMemo(() => {
    if (!range) return isBacktest ? "Loading the run's date range…" : "Pick a range.";
    if (!range.from || !range.to) return "Both dates are required.";
    if (range.from > range.to) return "From must be on or before To.";
    if (daysBetween(range.from, range.to) > MAX_RANGE_DAYS) {
      return `Ranges are capped at ${MAX_RANGE_DAYS} days.`;
    }
    if (isBacktest && runRange && (range.from < runRange.from || range.to > runRange.to)) {
      return `Outside the run (${runRange.from} → ${runRange.to}) — the server clamps to the run.`;
    }
    return null;
  }, [range, isBacktest, runRange]);

  const toggle = (id: ExportDataset) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const wantsZone = selected.has("decisions") || selected.has("signals");
  const longRange = range ? daysBetween(range.from, range.to) : 0;
  const decisionsEstimate = selected.has("decisions") ? longRange * 3 * 375 : 0;

  const run = useCallback(async () => {
    if (!range || busy) return;
    const scope: ExportScope =
      mode.kind === "backtest"
        ? { kind: "backtest", runId: mode.runId }
        : { kind: "ledger", ledger: mode.ledger };
    const order = datasets.map((d) => d.id).filter((id) => selected.has(id));
    setBusy(true);
    setError(null);
    setLog([]);
    try {
      for (let i = 0; i < order.length; i++) {
        const ds = order[i];
        const label = `${i + 1}/${order.length} — ${ds}`;
        const q: ExportQuery = {
          scope, dataset: ds, from_date: range.from, to_date: range.to,
          format: "csv", zone: zone || undefined,
        };
        if (format === "csv") {
          setProgress(`Downloading ${label}…`);
          const name = await algoApi.downloadExportCsv(q);
          setLog((l) => [...l, `${ds}: ${name}`]);
          continue;
        }
        setProgress(`Fetching ${label}…`);
        const table = await algoApi.fetchExportRows({ ...q, format: "ndjson" }, (n) =>
          setProgress(`Fetching ${label} — ${n.toLocaleString("en-IN")} rows…`)
        );
        const n = table.rows.length;
        if (n > XLSX_MAX_ROWS) {
          setProgress(`${ds}: ${n.toLocaleString("en-IN")} rows — too large for Excel, downloading CSV…`);
          const name = await algoApi.downloadExportCsv(q);
          setLog((l) => [...l, `${ds}: ${n.toLocaleString("en-IN")} rows > ${XLSX_MAX_ROWS.toLocaleString("en-IN")} — delivered as CSV (${name})`]);
          continue;
        }
        setProgress(`Building ${label} (${n.toLocaleString("en-IN")} rows)…`);
        await downloadXlsx(
          { headers: table.headers, rows: table.rows },
          `${table.filenameBase}.xlsx`
        );
        setLog((l) => [
          ...l,
          `${ds}: ${table.filenameBase}.xlsx (${n.toLocaleString("en-IN")} rows)${
            n > XLSX_WARN_ROWS ? " — large sheet, Excel may be slow to open" : ""
          }`,
        ]);
      }
      setProgress("Done.");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setProgress(null);
    } finally {
      setBusy(false);
    }
  }, [range, busy, mode, datasets, selected, zone, format]);

  const title =
    mode.kind === "backtest"
      ? `Export — backtest run #${mode.runId}`
      : `Export — ${mode.ledger.toUpperCase()} ledger`;

  return (
    <div
      className="fixed inset-0 z-50 bg-black/60 flex items-center justify-center px-4"
      onClick={busy ? undefined : onClose}
    >
      <div
        className="panel w-full max-w-xl px-5 py-4 max-h-[85vh] flex flex-col overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-base font-bold text-gray-100 mb-1">{title}</h3>
        <p className="text-xs text-muted mb-3">
          Complete data for the range — no row cap. CSV streams from the server; Excel is
          assembled in the browser from the same stream.
        </p>

        {/* Range presets */}
        <div className="text-[10px] text-muted uppercase tracking-wide mb-1">Range</div>
        <div className="flex items-center gap-1.5 flex-wrap mb-2">
          {(isBacktest
            ? ([["run", "Run range"], ["custom", "Custom"]] as const)
            : ([["today", "Today"], ["week", "This week"], ["month", "This month"], ["custom", "Custom"]] as const)
          ).map(([p, label]) => (
            <button
              key={p}
              type="button"
              className={`pill text-xs ${preset === p ? "pill-active" : ""}`}
              onClick={() => {
                setPreset(p);
                if (p === "custom" && range) setCustom({ from: range.from, to: range.to });
              }}
            >
              {label}
            </button>
          ))}
          {range && (
            <span className="text-[11px] text-muted font-mono ml-1">
              {range.from} → {range.to} ({longRange} day{longRange === 1 ? "" : "s"})
            </span>
          )}
        </div>
        {preset === "custom" && (
          <div className="grid grid-cols-2 gap-3 mb-2">
            <DateField
              label="From"
              value={custom.from}
              min={runRange?.from}
              max={runRange?.to}
              onChange={(v) => setCustom((c) => ({ ...c, from: v }))}
            />
            <DateField
              label="To"
              value={custom.to}
              min={runRange?.from}
              max={runRange?.to}
              onChange={(v) => setCustom((c) => ({ ...c, to: v }))}
            />
          </div>
        )}
        {rangeProblem && <div className="text-[11px] text-amber-300 mb-2">{rangeProblem}</div>}

        {/* Datasets */}
        <div className="text-[10px] text-muted uppercase tracking-wide mb-1">Datasets</div>
        <div className="grid sm:grid-cols-2 gap-x-4 gap-y-1 mb-3">
          {datasets.map((d) => (
            <label key={d.id} className="flex items-start gap-2 text-xs cursor-pointer py-0.5">
              <input
                type="checkbox"
                className="mt-0.5 accent-accent"
                checked={selected.has(d.id)}
                onChange={() => toggle(d.id)}
              />
              <span>
                <span className="text-gray-100 font-semibold">{d.label}</span>
                <span className="block text-[10.5px] text-muted">{d.hint}</span>
              </span>
            </label>
          ))}
        </div>
        <div className="flex items-center gap-2 mb-3">
          <button
            type="button"
            className="pill text-xs"
            onClick={() => setSelected(new Set(datasets.map((d) => d.id)))}
          >
            All
          </button>
          <button type="button" className="pill text-xs" onClick={() => setSelected(new Set())}>
            Clear
          </button>
          {wantsZone && (
            <div className="w-32 ml-auto">
              <SelectField
                label="Zone (decisions / signals)"
                value={zone}
                options={[
                  { value: "", label: "All zones" },
                  { value: "Z1", label: "Z1" },
                  { value: "Z2", label: "Z2" },
                  { value: "Z3", label: "Z3" },
                ]}
                onChange={setZone}
              />
            </div>
          )}
        </div>

        {/* Format */}
        <div className="text-[10px] text-muted uppercase tracking-wide mb-1">Format</div>
        <div className="flex items-center gap-1.5 mb-3">
          {(["csv", "xlsx"] as const).map((f) => (
            <button
              key={f}
              type="button"
              className={`pill text-xs ${format === f ? "pill-active" : ""}`}
              onClick={() => setFormat(f)}
            >
              {f === "csv" ? "CSV" : "Excel (.xlsx)"}
            </button>
          ))}
          <span className="text-[10.5px] text-muted ml-2">
            One file per dataset, downloaded in sequence.
          </span>
        </div>

        {format === "xlsx" && decisionsEstimate > XLSX_WARN_ROWS && (
          <div className="mb-3">
            <CalcNote tone="warn">
              Decisions over {longRange} days can reach ~{decisionsEstimate.toLocaleString("en-IN")}{" "}
              rows. Sheets above {XLSX_WARN_ROWS.toLocaleString("en-IN")} rows open slowly in Excel;
              above {XLSX_MAX_ROWS.toLocaleString("en-IN")} rows the export is delivered as CSV
              instead.
            </CalcNote>
          </div>
        )}

        {(progress || log.length > 0 || error) && (
          <div className="text-[11px] border border-border rounded-lg px-3 py-2 mb-3 bg-panel/60">
            {progress && <div className="text-gray-100">{progress}</div>}
            {log.map((l, i) => (
              <div key={i} className="text-muted font-mono">{l}</div>
            ))}
            {error && <div className="text-ce">{error}</div>}
          </div>
        )}

        <div className="flex justify-end gap-2 mt-auto">
          <button type="button" className="pill" onClick={onClose} disabled={busy}>
            {log.length > 0 && !busy ? "Close" : "Cancel"}
          </button>
          <button
            type="button"
            className="pill pill-active disabled:opacity-50"
            disabled={busy || selected.size === 0 || !range || (rangeProblem != null && !rangeProblem.startsWith("Outside"))}
            onClick={() => void run()}
          >
            {busy
              ? "Exporting…"
              : `Export ${selected.size} dataset${selected.size === 1 ? "" : "s"}`}
          </button>
        </div>
      </div>
    </div>
  );
}
