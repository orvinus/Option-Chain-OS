import { useMemo, useState } from "react";
import type { IvScannerResponse, IvScannerRow, SymbolSectorGroup } from "../types";
import {
  fmtFloat,
  fmtInt,
  fmtIvPct,
  fmtPctChange,
  fmtRatio,
} from "../utils/formatMarket";
import { WatchlistManager, WATCHLIST_MAX } from "./WatchlistManager";

type ExpirySlot = "near" | "next" | "far";
type ScanMode = "latest" | "historical";
type SortKey = keyof IvScannerRow | "iv_range";

interface Props {
  groups: SymbolSectorGroup[];
  symbols: string[];
  onSymbolsChange: (symbols: string[]) => void;
  data: IvScannerResponse | null;
  loading: boolean;
  error: string | null;
  updatedAt: string | null;
  expiry: ExpirySlot;
  mode: ScanMode;
  onExpiryChange: (e: ExpirySlot) => void;
  onModeChange: (m: ScanMode) => void;
  onSubmit: () => void;
  onClearFilters: () => void;
}

function chgClass(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v) || v === 0) return "text-muted";
  return v > 0 ? "text-pe" : "text-ce";
}

function Arrow({ v }: { v: number | null | undefined }) {
  if (v == null || !Number.isFinite(v) || v === 0) return null;
  return <span className="ml-0.5">{v > 0 ? "▲" : "▼"}</span>;
}

function ivrClass(v: number | null | undefined): string {
  if (v == null) return "text-muted";
  if (v >= 80) return "text-ce font-semibold";
  if (v <= 20) return "text-pe font-semibold";
  return "text-gray-200";
}

function pcrClass(v: number | null | undefined): string {
  if (v == null) return "text-muted";
  if (v >= 1.5) return "text-pe";
  if (v <= 0.7) return "text-ce";
  return "text-gray-200";
}

const COLUMNS: { key: SortKey; label: string; advanced?: boolean }[] = [
  { key: "symbol", label: "Symbol" },
  { key: "price", label: "Price" },
  { key: "price_chg", label: "Price Chg" },
  { key: "price_chg_pct", label: "Price Chg (%)" },
  { key: "total_oi", label: "Total OI" },
  { key: "total_oi_chg", label: "Total OI Chg" },
  { key: "total_oi_chg_pct", label: "Total OI Chg (%)" },
  { key: "pcr", label: "PCR" },
  { key: "iv", label: "IV" },
  { key: "iv_chg_pct", label: "IV Chg (%)" },
  { key: "iv_range", label: "IV Range (1Yr)" },
  { key: "hv_10", label: "HV 10", advanced: true },
  { key: "hv_20", label: "HV 20", advanced: true },
  { key: "hv_30", label: "HV 30", advanced: true },
  { key: "ivr", label: "IVR", advanced: true },
  { key: "ivp", label: "IVP", advanced: true },
  { key: "iv_hv10", label: "IV/HV10 %", advanced: true },
  { key: "iv_hv20", label: "IV/HV20 %", advanced: true },
  { key: "iv_hv30", label: "IV/HV30 %", advanced: true },
];

function cellValue(row: IvScannerRow, key: SortKey): string | number | null {
  if (key === "iv_range") {
    if (row.iv_range_1y_low == null || row.iv_range_1y_high == null) return null;
    return `${(row.iv_range_1y_low * 100).toFixed(1)}–${(row.iv_range_1y_high * 100).toFixed(1)}%`;
  }
  const v = row[key as keyof IvScannerRow];
  if (typeof v === "boolean") return v ? 1 : 0;
  return v as string | number | null;
}

function sortValue(row: IvScannerRow, key: SortKey): number | string {
  if (key === "iv_range") {
    return row.iv_range_1y_high ?? -Infinity;
  }
  if (key === "symbol" || key === "display") {
    return String(row[key] ?? "");
  }
  const v = row[key as keyof IvScannerRow];
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v === "boolean") return v ? 1 : 0;
  return -Infinity;
}

export function IvScanner({
  groups,
  symbols,
  onSymbolsChange,
  data,
  loading,
  error,
  updatedAt,
  expiry,
  mode,
  onExpiryChange,
  onModeChange,
  onSubmit,
  onClearFilters,
}: Props) {
  const [sortKey, setSortKey] = useState<SortKey>("symbol");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");
  const [showAdvanced, setShowAdvanced] = useState(true);
  const [filters, setFilters] = useState<Record<string, string>>({});

  const visibleCols = COLUMNS.filter((c) => showAdvanced || !c.advanced);

  const rows = useMemo(() => {
    let list = data?.rows ?? [];
    for (const [k, q] of Object.entries(filters)) {
      const needle = q.trim().toUpperCase();
      if (!needle) continue;
      list = list.filter((r) => {
        const val = cellValue(r, k as SortKey);
        return String(val ?? "").toUpperCase().includes(needle);
      });
    }
    const sorted = [...list].sort((a, b) => {
      const av = sortValue(a, sortKey);
      const bv = sortValue(b, sortKey);
      if (typeof av === "string" && typeof bv === "string") {
        return sortDir === "asc" ? av.localeCompare(bv) : bv.localeCompare(av);
      }
      const an = Number(av);
      const bn = Number(bv);
      return sortDir === "asc" ? an - bn : bn - an;
    });
    return sorted;
  }, [data, filters, sortKey, sortDir]);

  const toggleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir(key === "symbol" ? "asc" : "desc");
    }
  };

  const renderCell = (row: IvScannerRow, key: SortKey) => {
    switch (key) {
      case "symbol":
        return <span className="font-semibold text-gray-100">{row.symbol}</span>;
      case "price":
        return fmtFloat(row.price);
      case "price_chg":
        return (
          <span className={chgClass(row.price_chg)}>
            {fmtFloat(row.price_chg)}
            <Arrow v={row.price_chg} />
          </span>
        );
      case "price_chg_pct":
        return (
          <span className={chgClass(row.price_chg_pct)}>
            {fmtPctChange(row.price_chg_pct)}
            <Arrow v={row.price_chg_pct} />
          </span>
        );
      case "total_oi":
        return fmtInt(row.total_oi);
      case "total_oi_chg":
        return (
          <span className={chgClass(row.total_oi_chg)}>
            {fmtInt(row.total_oi_chg)}
            <Arrow v={row.total_oi_chg} />
          </span>
        );
      case "total_oi_chg_pct":
        return (
          <span className={chgClass(row.total_oi_chg_pct)}>
            {fmtPctChange(row.total_oi_chg_pct)}
          </span>
        );
      case "pcr":
        return <span className={pcrClass(row.pcr)}>{fmtRatio(row.pcr, 2)}</span>;
      case "iv":
        return <span className="text-violet-300">{fmtIvPct(row.iv)}</span>;
      case "iv_chg_pct":
        return (
          <span className={chgClass(row.iv_chg_pct)}>
            {row.history_ready || row.iv_chg_pct != null
              ? fmtPctChange(row.iv_chg_pct)
              : "warming…"}
          </span>
        );
      case "iv_range":
        return (
          <span className="text-xs text-muted">
            {row.iv_range_1y_low != null && row.iv_range_1y_high != null
              ? `${(row.iv_range_1y_low * 100).toFixed(1)}–${(row.iv_range_1y_high * 100).toFixed(1)}%`
              : "warming…"}
          </span>
        );
      case "hv_10":
        return fmtIvPct(row.hv_10);
      case "hv_20":
        return fmtIvPct(row.hv_20);
      case "hv_30":
        return fmtIvPct(row.hv_30);
      case "ivr":
        return (
          <span className={ivrClass(row.ivr)}>
            {row.ivr != null ? row.ivr.toFixed(0) : "warming…"}
          </span>
        );
      case "ivp":
        return (
          <span className={ivrClass(row.ivp)}>
            {row.ivp != null ? row.ivp.toFixed(0) : "warming…"}
          </span>
        );
      case "iv_hv10":
        return fmtFloat(row.iv_hv10, 1);
      case "iv_hv20":
        return fmtFloat(row.iv_hv20, 1);
      case "iv_hv30":
        return fmtFloat(row.iv_hv30, 1);
      default:
        return "—";
    }
  };

  const th = "py-1.5 px-1.5 text-[10px] uppercase tracking-tight text-muted font-mono border border-border/70 bg-[#0c1220] sticky top-0";
  const td = "py-1 px-1.5 text-xs font-mono tabular-nums border border-border/50 align-middle";

  return (
    <div className="flex flex-col gap-3">
      <div className="panel px-4 py-3 flex flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-base font-semibold text-gray-100 tracking-wide">IVR-IVP Scan</h2>
          <div className="flex items-center gap-3 text-xs text-muted">
            {updatedAt && (
              <span>
                Last updated:{" "}
                <span className="text-gray-300 font-mono">
                  {new Date(updatedAt).toLocaleString("en-IN", { hour12: false })}
                </span>
              </span>
            )}
            {loading && <span className="text-accent">Refreshing…</span>}
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-4">
          <div className="flex items-center gap-2 text-xs">
            <span className="text-muted uppercase tracking-wider">Data View</span>
            {(["latest", "historical"] as ScanMode[]).map((m) => (
              <label key={m} className="flex items-center gap-1 cursor-pointer">
                <input
                  type="radio"
                  name="iv-mode"
                  checked={mode === m}
                  onChange={() => onModeChange(m)}
                />
                <span className="capitalize text-gray-200">{m}</span>
              </label>
            ))}
          </div>

          <div className="flex items-center gap-2 text-xs">
            <span className="text-muted uppercase tracking-wider">Expiry</span>
            {(["near", "next", "far"] as ExpirySlot[]).map((e) => (
              <label key={e} className="flex items-center gap-1 cursor-pointer">
                <input
                  type="radio"
                  name="iv-expiry"
                  checked={expiry === e}
                  onChange={() => onExpiryChange(e)}
                />
                <span className="capitalize text-gray-200">
                  {e}
                  {data?.expiry && expiry === e ? ` (${data.expiry})` : ""}
                </span>
              </label>
            ))}
          </div>

          <button type="button" className="pill-active pill text-xs px-4" onClick={onSubmit}>
            Submit
          </button>
          <button
            type="button"
            className="pill text-xs"
            onClick={() => {
              setFilters({});
              onClearFilters();
            }}
          >
            Clear filters
          </button>
          <label className="flex items-center gap-1.5 text-xs text-muted cursor-pointer ml-auto">
            <input
              type="checkbox"
              checked={showAdvanced}
              onChange={(e) => setShowAdvanced(e.target.checked)}
            />
            Advanced (HV / IVR / IVP)
          </label>
        </div>

        <WatchlistManager groups={groups} onActiveSymbolsChange={onSymbolsChange} />

        {symbols.length === 0 && (
          <div className="text-xs text-amber-300">
            Watchlist is empty — open Edit symbols and add up to {WATCHLIST_MAX} symbols.
          </div>
        )}
        {data?.note && <div className="text-xs text-amber-300">{data.note}</div>}
      </div>

      {error && (
        <div className="panel p-3 text-sm text-red-400 border border-red-500/20">{error}</div>
      )}

      <div className="panel overflow-hidden">
        <div className="overflow-x-auto max-h-[70vh]">
          <table className="min-w-[1400px] w-full border-collapse bg-[#0a0f18]">
            <thead>
              <tr>
                {visibleCols.map((c) => (
                  <th
                    key={c.key}
                    className={`${th} cursor-pointer hover:text-white select-none whitespace-nowrap`}
                    onClick={() => toggleSort(c.key)}
                  >
                    {c.label}
                    {sortKey === c.key ? (sortDir === "asc" ? " ↑" : " ↓") : ""}
                  </th>
                ))}
              </tr>
              <tr>
                {visibleCols.map((c) => (
                  <th key={`f-${c.key}`} className="p-0.5 border border-border/50 bg-[#0a0f18]">
                    <input
                      className="w-full bg-panel border border-border/60 rounded px-1 py-0.5 text-[10px] text-gray-200"
                      placeholder="…"
                      value={filters[c.key] ?? ""}
                      onChange={(e) =>
                        setFilters((prev) => ({ ...prev, [c.key]: e.target.value }))
                      }
                    />
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.length === 0 && !loading && (
                <tr>
                  <td
                    colSpan={visibleCols.length}
                    className="py-8 text-center text-muted text-sm"
                  >
                    {symbols.length === 0
                      ? "Add symbols to your watchlist to begin."
                      : "No rows — submit to load, or wait for data."}
                  </td>
                </tr>
              )}
              {rows.map((row, i) => (
                <tr key={row.symbol} className={i % 2 ? "bg-black/25" : ""}>
                  {visibleCols.map((c) => (
                    <td key={c.key} className={`${td} text-right first:text-left`}>
                      {renderCell(row, c.key)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
