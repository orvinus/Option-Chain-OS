import type { OIChangeResponse } from "../types";
import { atmRound, filterOiRowsByAtmWindow } from "../utils/oiStrikeWindow";
import { callPutRatio } from "../utils/ratio";
import { compact, signedCompact } from "../utils/num";
import { changeColor, sideColor, sideLabel } from "../utils/ui";
import type { OIMode } from "./OIChangeChart";

interface Props {
  data: OIChangeResponse | null;
  mode: OIMode;
  atmWindow: number;
  /** Live spot from /api/health when the feed is connected — aligns ATM with the index. */
  liveSpot?: number | null;
}

export function KPIBar({ data, mode, atmWindow, liveSpot }: Props) {
  const rows = data?.rows ?? [];
  const spot = liveSpot ?? data?.spot ?? null;
  const step = rows.length > 1 ? Math.abs(rows[1].strike - rows[0].strike) || 50 : 50;
  const windowRows = filterOiRowsByAtmWindow(rows, spot, atmWindow);

  // Absolute OI totals (in contracts).
  const totalCeOI = windowRows.reduce((s, r) => s + r.call_oi, 0);
  const totalPeOI = windowRows.reduce((s, r) => s + r.put_oi, 0);
  // Single canonical PCR = Put OI ÷ Call OI on LEVELS (matches the backend / main dashboard).
  const pcr = totalCeOI > 0 ? totalPeOI / totalCeOI : 0;

  // OI Change totals (match the chart window).
  const ceChg = windowRows.reduce((s, r) => s + r.call_oi_change, 0);
  const peChg = windowRows.reduce((s, r) => s + r.put_oi_change, 0);

  const isChange = mode === "change";
  const ceVal = isChange ? ceChg : totalCeOI;
  const peVal = isChange ? peChg : totalPeOI;
  const pcrVal = pcr;

  // Standardized normalized Call : Put ratio + dominant Side (shared with the main
  // dashboard). Ratio text is magnitude-normalized; Side = the smaller signed OI Δ.
  const cpr = callPutRatio(ceChg, peChg);
  const absRatio = !isChange && totalPeOI > 0 ? totalCeOI / totalPeOI : null;

  const hasRatio = isChange
    ? cpr.side !== "NEUTRAL"
    : absRatio !== null && Number.isFinite(absRatio);
  const ratioMain = isChange
    ? cpr.text
    : absRatio !== null && Number.isFinite(absRatio)
      ? absRatio.toFixed(3)
      : "—";

  return (
    <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
      {/* Call OI */}
      <div className="kpi">
        <span className="text-xs text-muted uppercase tracking-wider">
          {isChange ? "Call OI Change" : "Total Call OI"}
        </span>
        <span className={`text-xl font-mono font-bold ${
          isChange ? changeColor(ceChg) : "text-red-400"
        }`}>
          {isChange ? signedCompact(ceVal) : compact(ceVal)}
        </span>
        <span className="text-[10px] text-muted/60">contracts</span>
      </div>

      {/* Put OI */}
      <div className="kpi">
        <span className="text-xs text-muted uppercase tracking-wider">
          {isChange ? "Put OI Change" : "Total Put OI"}
        </span>
        <span className={`text-xl font-mono font-bold ${
          isChange ? changeColor(peChg) : "text-green-400"
        }`}>
          {isChange ? signedCompact(peVal) : compact(peVal)}
        </span>
        <span className="text-[10px] text-muted/60">contracts</span>
      </div>

      {/* OI-change ratio (windowed) + dominant Side */}
      <div className="kpi">
        <span className="text-xs text-muted uppercase tracking-wider">Ratio</span>
        <span
          className={`text-xl font-mono font-bold tabular-nums ${
            hasRatio ? "text-yellow-400" : "text-muted"
          }`}
        >
          {ratioMain}
        </span>
        {isChange ? (
          <span className={`text-[10px] font-semibold ${sideColor(cpr.side)}`}>
            {cpr.side === "NEUTRAL" ? "Neutral" : `${sideLabel(cpr.side)} dominant`}
          </span>
        ) : (
          <span className="text-[10px] text-muted/60">CE ÷ PE OI</span>
        )}
      </div>

      {/* PCR */}
      <div className="kpi">
        <span className="text-xs text-muted uppercase tracking-wider">PCR</span>
        <span className={`text-xl font-mono font-bold ${
          pcrVal >= 1.2 ? "text-green-400"
          : pcrVal <= 0.8 ? "text-red-400"
          : "text-yellow-400"
        }`}>
          {pcrVal.toFixed(2)}
        </span>
        <span className="text-[10px] text-muted/60">
          {pcrVal >= 1.2 ? "Bullish bias" : pcrVal <= 0.8 ? "Bearish bias" : "Neutral"}
        </span>
      </div>

      {/* Spot / ATM */}
      <div className="kpi">
        <span className="text-xs text-muted uppercase tracking-wider">Spot / ATM</span>
        <span className="text-xl font-mono font-bold tabular-nums">
          {spot != null ? spot.toFixed(2) : "—"}
        </span>
        {spot != null && (
          <span className="text-[10px] text-muted/60">
            ATM: {atmRound(spot, step)}
          </span>
        )}
      </div>
    </div>
  );
}
