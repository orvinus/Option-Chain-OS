import { useState } from "react";
import { AtmWindowSelect } from "../components/AtmWindowSelect";
import { DatePicker } from "../components/DatePicker";
import { ExpirySelect } from "../components/ExpirySelect";
import { SymbolSelect } from "../components/SymbolSelect";
import type { MarketContextValue } from "../hooks/useMarketContext";
import { useAvailableDates } from "../hooks/useAvailableDates";
import { useMultiTimeframe } from "../hooks/useMultiTimeframe";
import type { MtfRow } from "../types";
import { signedCompact, compact } from "../utils/num";
import { effectiveAtmWindow } from "../utils/oiStrikeWindow";
import { callPutRatio, type DominantSide } from "../utils/ratio";
import { isToday, isoForSessionMinuteOnDate, maxMinForDate } from "../utils/sessionTime";

const ATM_MAX_WINDOW = 50;
const fmt2 = (v: number | null | undefined) => (v == null ? "—" : v.toFixed(2));
const changeColor = (v: number) => (v > 0 ? "text-emerald-400" : v < 0 ? "text-red-400" : "text-muted");
const sideColor = (s: DominantSide) =>
  s === "CALL" ? "text-emerald-400" : s === "PUT" ? "text-red-400" : "text-muted";
const sideLabel = (s: DominantSide) => (s === "CALL" ? "Call" : s === "PUT" ? "Put" : "Neutral");

const TF_LABEL: Record<string, string> = {
  "1m": "1 Min", "3m": "3 Min", "5m": "5 Min", "10m": "10 Min", "15m": "15 Min",
  "30m": "30 Min", "1h": "1 Hour", "2h": "2 Hour", "3h": "3 Hour", full_day: "Full Day",
};

export function MultiTimeframePage({ mc }: { mc: MarketContextValue }) {
  const {
    authenticated, health, symbol, symbolGroups, switching, symbolError, handleSymbolChange,
    expiry, setExpiry, expiries, expiryError, fnoEligible, symbolDisplay,
    atmWindow, setAtmWindow,
  } = mc;

  const [selectedDate, setSelectedDate] = useState<string | null>(null);

  const avail = useAvailableDates(
    fnoEligible ? symbol : null,
    expiry,
    authenticated && fnoEligible && !!expiry,
  );

  // A past date reads the grid "as of" that session's close (one fetch); today/null stays live.
  const historical = selectedDate != null && !isToday(selectedDate, health);
  const asOf = historical
    ? isoForSessionMinuteOnDate(selectedDate!, maxMinForDate(selectedDate!, health))
    : undefined;

  const { data, loading, error } = useMultiTimeframe({
    symbol: fnoEligible ? symbol : null,
    expiry,
    asOf,
    atmWindow,
    enabled: authenticated && fnoEligible && !!expiry,
  });

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
            <div className="flex items-center gap-2">
              <span className="text-xs text-muted">Strikes ATM ±</span>
              <AtmWindowSelect value={atmWindow} max={ATM_MAX_WINDOW} onChange={setAtmWindow} />
            </div>
          </>
        )}
        <div className="text-xs text-muted ml-auto">
          {historical && <span className="text-amber-300/90 mr-2">{selectedDate}</span>}
          {data ? `as of ${data.asof.slice(11, 19)} IST` : loading ? "loading…" : ""}
        </div>
      </div>

      {!fnoEligible ? (
        <div className="panel p-6 text-sm text-muted">No F&amp;O contracts for {symbolDisplay}.</div>
      ) : (
        <>
          {/* Shared point-in-time levels */}
          {data && (
            <div className="panel px-4 py-3 mb-4 flex flex-wrap gap-x-8 gap-y-2 text-sm">
              <Level label="Spot" value={data.spot != null ? data.spot.toFixed(2) : "—"} />
              <Level label="ATM" value={data.atm_strike != null ? String(data.atm_strike) : "—"} />
              <Level label="Ratio (C÷P)" value={fmt2(data.ratio)} />
              <Level label="PCR (P÷C)" value={fmt2(data.pcr)} />
              <Level label="Total Call OI" value={compact(data.total_call_oi)} />
              <Level label="Total Put OI" value={compact(data.total_put_oi)} />
            </div>
          )}

          <div className="panel p-3 overflow-x-auto">
            {error && <div className="px-1 pb-2 text-xs text-red-400">Failed to load: {error}</div>}
            <table className="w-full text-sm font-mono">
              <thead>
                <tr className="text-xs text-muted border-b border-border">
                  <th className="text-left py-2 px-3">Timeframe</th>
                  <th className="text-right py-2 px-3">Call OI Δ</th>
                  <th className="text-right py-2 px-3">Put OI Δ</th>
                  <th className="text-right py-2 px-3">Ratio</th>
                  <th className="text-right py-2 px-3">Side</th>
                  <th className="text-right py-2 px-3">Spot</th>
                  <th className="text-right py-2 px-3">ATM</th>
                </tr>
              </thead>
              <tbody>
                {data?.rows.map((r: MtfRow) => {
                  const rr = callPutRatio(r.call_oi_change, r.put_oi_change);
                  return (
                  <tr key={r.timeframe} className="border-b border-border/40 hover:bg-white/5">
                    <td className="text-left py-2 px-3 text-foreground">{TF_LABEL[r.timeframe] ?? r.timeframe}</td>
                    <td className={`text-right py-2 px-3 ${changeColor(r.call_oi_change)}`}>{signedCompact(r.call_oi_change)}</td>
                    <td className={`text-right py-2 px-3 ${changeColor(r.put_oi_change)}`}>{signedCompact(r.put_oi_change)}</td>
                    <td className="text-right py-2 px-3 text-foreground tabular-nums">{rr.text}</td>
                    <td className={`text-right py-2 px-3 font-semibold ${sideColor(rr.side)}`}>{sideLabel(rr.side)}</td>
                    <td className="text-right py-2 px-3 text-muted">{data.spot != null ? data.spot.toFixed(1) : "—"}</td>
                    <td className="text-right py-2 px-3 text-muted">{data.atm_strike ?? "—"}</td>
                  </tr>
                  );
                })}
                {!data && loading && (
                  <tr><td colSpan={7} className="py-6 text-center text-muted text-xs">Loading…</td></tr>
                )}
              </tbody>
            </table>
            <p className="px-3 pt-2 text-[10px] text-muted">
              Ratio (normalized Call : Put) and Side (dominant OI-Δ side) are computed per timeframe from
              that row's Call/Put OI change. Spot and ATM are point-in-time levels (identical across rows).
              All sums cover{" "}
              {atmWindow < 0 ? "the full stored chain" : `strikes ATM ± ${effectiveAtmWindow(atmWindow)}`}.
            </p>
          </div>
        </>
      )}
    </div>
  );
}

function Level({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col">
      <span className="text-[10px] uppercase tracking-wide text-muted">{label}</span>
      <span className="font-mono text-foreground">{value}</span>
    </div>
  );
}
