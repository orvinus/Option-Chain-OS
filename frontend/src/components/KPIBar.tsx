import type { OIChangeResponse } from "../types";
import { filterOiRowsByAtmWindow } from "../utils/oiStrikeWindow";
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

/** Indian compact notation matching Sensibull's display */
function compact(n: number, showSign = false): string {
  const sign = n < 0 ? "-" : showSign && n > 0 ? "+" : "";
  const abs = Math.abs(n);
  if (abs >= 1e7) return `${sign}${(abs / 1e7).toFixed(2)} Cr`;
  if (abs >= 1e5) return `${sign}${(abs / 1e5).toFixed(2)} L`;
  if (abs >= 1e3) return `${sign}${(abs / 1e3).toFixed(1)} K`;
  return `${sign}${abs === 0 ? "0" : abs.toLocaleString()}`;
}

export function KPIBar({ data, mode, atmWindow, liveSpot, strikeStep }: Props) {
  const rows = data?.rows ?? [];
  const spot = liveSpot ?? data?.spot ?? null;
  const step = strikeStep ?? 50;
  const windowRows = filterOiRowsByAtmWindow(rows, spot, atmWindow, strikeStep);

  // Absolute OI totals (in contracts, same unit Sensibull uses)
  const totalCeOI = windowRows.reduce((s, r) => s + r.call_oi, 0);
  const totalPeOI = windowRows.reduce((s, r) => s + r.put_oi, 0);
  const pcr = totalCeOI > 0 ? totalPeOI / totalCeOI : 0;

  // OI Change totals (match chart window; full-chain API totals only equal sums when atmWindow === 0)
  const ceChg = windowRows.reduce((s, r) => s + r.call_oi_change, 0);
  const peChg = windowRows.reduce((s, r) => s + r.put_oi_change, 0);
  const pcrChg = ceChg !== 0 ? Math.abs(peChg / ceChg) : 0;

  const isChange = mode === "change";
  const ceVal = isChange ? ceChg : totalCeOI;
  const peVal = isChange ? peChg : totalPeOI;
  const pcrVal = isChange ? pcrChg : pcr;

  // OI-Change Ratio — highlight the WEAKER (lower-buildup) side, which better
  // reflects current sentiment. Ratio = higher ÷ lower (always ≥ 1), shown as
  // "1 : X.XX" tagged with the weaker side. Only meaningful in change mode when
  // BOTH sides are building OI; otherwise show "—".
  const bothBuild = isChange && ceChg > 0 && peChg > 0;
  const higher = bothBuild ? Math.max(ceChg, peChg) : null;
  const lower = bothBuild ? Math.min(ceChg, peChg) : null;
  const weakerRatio =
    higher !== null && lower !== null && lower > 0 && Number.isFinite(higher / lower)
      ? higher / lower // ≥ 1 by construction
      : null;
  const weakerSide: "Call" | "Put" | null =
    weakerRatio === null ? null : ceChg < peChg ? "Call" : peChg < ceChg ? "Put" : null;

  // Absolute-OI mode keeps the legacy CE ÷ PE OI ratio (the primary dashboard
  // only ever renders change mode, but this preserves the absolute-mode caller).
  const absRatio = !isChange && totalPeOI > 0 ? totalCeOI / totalPeOI : null;

  const hasRatio = isChange
    ? weakerRatio !== null
    : absRatio !== null && Number.isFinite(absRatio);
  const ratioMain = isChange
    ? weakerRatio !== null
      ? `1 : ${weakerRatio.toFixed(2)}`
      : "—"
    : absRatio !== null && Number.isFinite(absRatio)
      ? absRatio.toFixed(3)
      : "—";
  const ratioHint = weakerSide
    ? `${weakerSide} weaker`
    : isChange
      ? "Call ÷ Put chg"
      : "CE ÷ PE OI";

  return (
    <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
      {/* Call OI */}
      <div className="kpi">
        <span className="text-xs text-muted uppercase tracking-wider">
          {isChange ? "Call OI Change" : "Total Call OI"}
        </span>
        <span className={`text-xl font-mono font-bold ${
          isChange
            ? ceChg > 0 ? "text-red-400" : ceChg < 0 ? "text-green-400" : "text-muted"
            : "text-red-400"
        }`}>
          {compact(ceVal, isChange)}
        </span>
        <span className="text-[10px] text-muted/60">contracts</span>
      </div>

      {/* Put OI */}
      <div className="kpi">
        <span className="text-xs text-muted uppercase tracking-wider">
          {isChange ? "Put OI Change" : "Total Put OI"}
        </span>
        <span className={`text-xl font-mono font-bold ${
          isChange
            ? peChg > 0 ? "text-green-400" : peChg < 0 ? "text-red-400" : "text-muted"
            : "text-green-400"
        }`}>
          {compact(peVal, isChange)}
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
            ATM: {Math.round(spot / step) * step}
          </span>
        )}
      </div>
    </div>
  );
}
