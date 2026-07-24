import { useMemo, useState } from "react";
import type { OptionChainFullResponse, OptionChainFullRow } from "../types";
import { fmtFloat, fmtGreek, fmtOiLakh } from "../utils/formatMarket";
import { atmStrike, filterOiRowsByAtmWindow } from "../utils/oiStrikeWindow";

interface Props {
  data: OptionChainFullResponse | null;
  liveSpot: number | null;
  atmWindow: number;
  symbolDisplay?: string;
  isLoading?: boolean;
}

function OiBar({ value, max, color }: { value: number; max: number; color: "ce" | "pe" }) {
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  const bg = color === "ce" ? "bg-ce/70" : "bg-pe/70";
  return (
    <div className="flex items-center gap-1 min-w-[72px]">
      <div className="flex-1 h-1.5 rounded-full bg-black/40 overflow-hidden">
        <div className={`h-full ${bg}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

export function GreeksChainTable({
  data,
  liveSpot,
  atmWindow,
  symbolDisplay,
  isLoading,
}: Props) {
  const [perLot, setPerLot] = useState(false);
  const spot = liveSpot ?? data?.spot ?? null;
  const lot = data?.lot_size ?? 1;
  const allRows = data?.rows ?? [];
  const rows = filterOiRowsByAtmWindow(allRows, spot, atmWindow);
  const strikes = allRows.map((r) => r.strike);
  const atm = spot != null && strikes.length ? atmStrike(strikes, spot) : null;

  const maxCallOi = useMemo(() => Math.max(1, ...rows.map((r) => r.call_oi)), [rows]);
  const maxPutOi = useMemo(() => Math.max(1, ...rows.map((r) => r.put_oi)), [rows]);

  const scaleLtp = (ltp: number | null) => {
    if (ltp == null) return null;
    return perLot ? ltp * lot : ltp;
  };

  if (isLoading && !data) {
    return <div className="panel p-8 text-center text-muted text-sm">Loading Greeks chain…</div>;
  }
  if (!data || rows.length === 0) {
    return (
      <div className="panel p-6 text-center text-muted text-sm">
        No option-chain data yet for this symbol / expiry.
      </div>
    );
  }

  const grid = "border border-border/80";
  const thc = `py-1 px-1 text-[11px] leading-tight font-mono uppercase tracking-tight align-middle ${grid}`;
  const tc = `py-0.5 px-1 text-sm leading-snug font-mono tabular-nums align-middle ${grid}`;
  const callBg = "bg-green-950/25";
  const putBg = "bg-red-950/25";
  const strikeColBg = "bg-violet-950/55";
  const strikeColHeaderBg = "bg-violet-900/60";
  const atmRowBg = "bg-yellow-400/45";
  const sectionDividerR = "border-r-[5px] border-r-white";

  const renderRow = (r: OptionChainFullRow, idx: number) => {
    const isAtm = atm != null && r.strike === atm;
    const callItm = spot != null && r.strike < spot;
    const putItm = spot != null && r.strike > spot;
    const zebra = idx % 2 === 1 ? "bg-black/25" : "";
    const rowBg = isAtm ? atmRowBg : zebra;

    return (
      <tr
        key={r.strike}
        className={`${rowBg} ${isAtm ? "ring-2 ring-inset ring-yellow-500/80" : ""}`}
      >
        {/* CALLS */}
        <td className={`${tc} ${callBg} ${callItm && !isAtm ? "bg-amber-900/20" : ""} text-right`}>
          {fmtGreek(r.call_gamma, "gamma")}
        </td>
        <td className={`${tc} ${callBg} text-right`}>{fmtGreek(r.call_vega, "vega")}</td>
        <td className={`${tc} ${callBg} text-right`}>{fmtGreek(r.call_theta, "theta")}</td>
        <td className={`${tc} ${callBg} text-right`}>{fmtGreek(r.call_delta, "delta")}</td>
        <td className={`${tc} ${callBg} text-right`}>{fmtOiLakh(r.call_oi)}</td>
        <td className={`${tc} ${callBg}`}>
          <OiBar value={r.call_oi} max={maxCallOi} color="ce" />
        </td>
        <td
          className={`${tc} ${callBg} ${sectionDividerR} text-right font-semibold text-gray-100`}
        >
          {fmtFloat(scaleLtp(r.call_ltp))}
        </td>

        {/* Strike */}
        <td
          className={`${tc} ${strikeColBg} ${sectionDividerR} text-center font-semibold text-white`}
        >
          {r.strike.toLocaleString()}
        </td>

        {/* PUTS */}
        <td className={`${tc} ${putBg} text-right font-semibold text-gray-100`}>
          {fmtFloat(scaleLtp(r.put_ltp))}
        </td>
        <td className={`${tc} ${putBg}`}>
          <OiBar value={r.put_oi} max={maxPutOi} color="pe" />
        </td>
        <td className={`${tc} ${putBg} ${putItm && !isAtm ? "bg-amber-900/20" : ""} text-right`}>
          {fmtOiLakh(r.put_oi)}
        </td>
        <td className={`${tc} ${putBg} text-right`}>{fmtGreek(r.put_delta, "delta")}</td>
        <td className={`${tc} ${putBg} text-right`}>{fmtGreek(r.put_theta, "theta")}</td>
        <td className={`${tc} ${putBg} text-right`}>{fmtGreek(r.put_vega, "vega")}</td>
        <td className={`${tc} ${putBg} text-right`}>{fmtGreek(r.put_gamma, "gamma")}</td>
      </tr>
    );
  };

  return (
    <div className="flex flex-col gap-2">
      <div className="panel px-4 py-3 flex flex-wrap items-center gap-4 justify-between">
        <div className="flex flex-wrap items-center gap-3 text-sm">
          <span className="font-semibold text-gray-100">
            {symbolDisplay ?? "Symbol"}{" "}
            <span className="font-mono text-accent">
              {spot != null ? spot.toFixed(2) : "—"}
            </span>
          </span>
          <span className="text-xs text-muted">
            Expiry <span className="text-gray-200 font-mono">{data.expiry}</span>
          </span>
          <span className="text-xs text-muted">
            Synth Fut{" "}
            <span className="text-gray-200 font-mono">
              {fmtFloat(data.synthetic_future ?? null)}
            </span>
          </span>
          <span className="text-xs text-muted">
            IVP{" "}
            <span className="text-violet-300 font-mono">
              {data.ivp != null && Number.isFinite(data.ivp) ? data.ivp.toFixed(0) : "—"}
            </span>
          </span>
          {data.atm_iv != null && (
            <span className="text-xs text-muted">
              ATM IV{" "}
              <span className="text-violet-300 font-mono">
                {(data.atm_iv * 100).toFixed(2)}%
              </span>
            </span>
          )}
        </div>
        <label className="flex items-center gap-2 text-xs text-muted cursor-pointer select-none">
          <span>Per Lot</span>
          <button
            type="button"
            role="switch"
            aria-checked={perLot}
            onClick={() => setPerLot((v) => !v)}
            className={`relative w-10 h-5 rounded-full transition border ${
              perLot ? "bg-accent/80 border-accent" : "bg-panel border-border"
            }`}
          >
            <span
              className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white transition-transform ${
                perLot ? "translate-x-5" : ""
              }`}
            />
          </button>
        </label>
      </div>

      <div className="panel overflow-hidden">
        <div className="overflow-x-auto">
          <table className="min-w-[1100px] w-full border-collapse border border-border bg-[#0a0f18]">
            <thead>
              <tr className="bg-[#0c1220]">
                <th colSpan={7} className={`${thc} ${callBg} ${sectionDividerR} text-ce`}>
                  CALLS
                </th>
                <th className={`${thc} ${strikeColHeaderBg} ${sectionDividerR} text-white`}>
                  Strike
                </th>
                <th colSpan={7} className={`${thc} ${putBg} text-pe`}>
                  PUTS
                </th>
              </tr>
              <tr className="bg-[#0c1220]/95 text-muted">
                <th className={`${thc} ${callBg} text-right`}>Gamma</th>
                <th className={`${thc} ${callBg} text-right`}>Vega</th>
                <th className={`${thc} ${callBg} text-right`}>Theta</th>
                <th className={`${thc} ${callBg} text-right`}>Delta</th>
                <th className={`${thc} ${callBg} text-right`}>OI-lakh</th>
                <th className={`${thc} ${callBg}`}>Call OI</th>
                <th className={`${thc} ${callBg} ${sectionDividerR} text-right`}>LTP</th>
                <th className={`${thc} ${strikeColHeaderBg} ${sectionDividerR}`}>Strike</th>
                <th className={`${thc} ${putBg} text-right`}>LTP</th>
                <th className={`${thc} ${putBg}`}>Put OI</th>
                <th className={`${thc} ${putBg} text-right`}>OI-lakh</th>
                <th className={`${thc} ${putBg} text-right`}>Delta</th>
                <th className={`${thc} ${putBg} text-right`}>Theta</th>
                <th className={`${thc} ${putBg} text-right`}>Vega</th>
                <th className={`${thc} ${putBg} text-right`}>Gamma</th>
              </tr>
            </thead>
            <tbody>{rows.map(renderRow)}</tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
