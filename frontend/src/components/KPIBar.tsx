import type { OIChangeResponse } from "../types";
import { atmRound, filterOiRowsByAtmWindow } from "../utils/oiStrikeWindow";
import { callPutRatio } from "../utils/ratio";
import { compact, signedCompact } from "../utils/num";
import { changeColor } from "../utils/ui";
import type { OIMode } from "./OIChangeChart";

interface Props {
  data: OIChangeResponse | null;
  mode: OIMode;
  atmWindow: number;
  /** Live spot from /api/health when the Angel feed is connected — aligns ATM with the active index/stock. */
  liveSpot?: number | null;
  /** The symbol's strike step from the registry (NIFTY 50, SENSEX 100). */
  strikeStep?: number | null;
}

export function KPIBar({ data, mode, atmWindow, liveSpot, strikeStep }: Props) {
  const rows = data?.rows ?? [];
  const spot = liveSpot ?? data?.spot ?? null;
  const step = strikeStep ?? 50;
  const windowRows = filterOiRowsByAtmWindow(rows, spot, atmWindow, strikeStep);

  // Absolute OI totals (in contracts, same unit Sensibull uses)
  const totalCeOI = windowRows.reduce((s, r) => s + r.call_oi, 0);
  const totalPeOI = windowRows.reduce((s, r) => s + r.put_oi, 0);
  // Single canonical PCR = Put OI ÷ Call OI on LEVELS (matches the backend and
  // the Multi-TF header `pcr`). Shown in both absolute and change modes so the
  // OI-Change tab and Multi-TF never report a different PCR for the same window.
  const pcr = totalCeOI > 0 ? totalPeOI / totalCeOI : 0;

  // OI Change totals (match chart window; full-chain API totals only equal sums when atmWindow === 0)
  const ceChg = windowRows.reduce((s, r) => s + r.call_oi_change, 0);
  const peChg = windowRows.reduce((s, r) => s + r.put_oi_change, 0);

  const isChange = mode === "change";
  const ceVal = isChange ? ceChg : totalCeOI;
  const peVal = isChange ? peChg : totalPeOI;
  const pcrVal = pcr;

  // Standardized normalized Call : Put ratio + dominant Side (shared across MTF /
  // Replay / Change-in-OI). Ratio text is magnitude-normalized with the smaller
  // side pinned to 1; Side = the SMALLER signed OI Δ side. On the windowed change totals.
  const cpr = callPutRatio(ceChg, peChg);

  // Absolute-OI mode keeps the legacy CE ÷ PE OI ratio (the primary dashboard
  // only ever renders change mode, but this preserves the absolute-mode caller).
  const absRatio = !isChange && totalPeOI > 0 ? totalCeOI / totalPeOI : null;

  const hasRatio = isChange
    ? cpr.side !== "NEUTRAL"
    : absRatio !== null && Number.isFinite(absRatio);
  const ratioMain = isChange
    ? cpr.text
    : absRatio !== null && Number.isFinite(absRatio)
      ? absRatio.toFixed(3)
      : "—";
  const ratioHint = isChange
    ? cpr.side === "NEUTRAL"
      ? "Neutral"
      : `${cpr.side === "CALL" ? "Call" : "Put"} dominant`
    : "CE ÷ PE OI";

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

      {/* OI-change ratio (windowed) — "1 : X.XX" on the weaker/lower-buildup side */}
      <div className="kpi">
        <span className="text-xs text-muted uppercase tracking-wider">Ratio</span>
        <span
          className={`text-xl font-mono font-bold tabular-nums ${
            hasRatio ? "text-yellow-400" : "text-muted"
          }`}
        >
          {ratioMain}
        </span>
        <span className="text-[10px] text-muted/60">{ratioHint}</span>
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
