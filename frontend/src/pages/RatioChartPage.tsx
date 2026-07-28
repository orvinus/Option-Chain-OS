import { useMemo, useState } from "react";
import { AtmWindowSelect } from "../components/AtmWindowSelect";
import { DatePicker } from "../components/DatePicker";
import { ExpirySelect } from "../components/ExpirySelect";
import { SymbolSelect } from "../components/SymbolSelect";
import {
  TimeSeriesChart,
  type CrosshairInfo,
  type LineSeriesSpec,
} from "../components/charts/TimeSeriesChart";
import { SERIES_COLORS } from "../components/charts/chartTheme";
import { istIsoToChartTime } from "../components/charts/chartTime";
import type { MarketContextValue } from "../hooks/useMarketContext";
import { effectiveAtmWindow } from "../utils/oiStrikeWindow";
import { useAvailableDates } from "../hooks/useAvailableDates";
import { useMultiTimeframe } from "../hooks/useMultiTimeframe";
import { useRatioTimeseries } from "../hooks/useRatioTimeseries";
import { isToday, isoForSessionMinuteOnDate, maxMinForDate } from "../utils/sessionTime";

// Buckets supported by the backend time_bucket aggregation.
const BUCKETS = ["1m", "5m", "10m", "15m", "30m"] as const;
const ATM_MAX_WINDOW = 50;

const fmt2 = (v: number | null | undefined) => (v == null ? "—" : v.toFixed(2));

export function RatioChartPage({ mc }: { mc: MarketContextValue }) {
  const {
    authenticated, health, symbol, symbolGroups, switching, symbolError, handleSymbolChange,
    expiry, setExpiry, expiries, expiryError, fnoEligible, symbolDisplay,
    atmWindow, setAtmWindow, activeEntry,
  } = mc;
  const strikeStep = activeEntry?.strike_step ?? 50;

  const [bucket, setBucket] = useState<string>("5m");
  const [hover, setHover] = useState<CrosshairInfo | null>(null);
  const [selectedDate, setSelectedDate] = useState<string | null>(null);

  const avail = useAvailableDates(
    fnoEligible ? symbol : null,
    expiry,
    authenticated && fnoEligible && !!expiry,
  );

  // A past date is read as a fixed full-session window (one fetch); today/null stays live.
  const historical = selectedDate != null && !isToday(selectedDate, health);
  const fromTs = historical ? isoForSessionMinuteOnDate(selectedDate!, 0) : undefined;
  const toTs = historical ? isoForSessionMinuteOnDate(selectedDate!, maxMinForDate(selectedDate!, health)) : undefined;

  // ATM center for the selected instant — via multi-timeframe (works live AND replay,
  // where there is no live spot). "All" (atmWindow<0) or unknown ATM => full chain.
  const mtf = useMultiTimeframe({
    symbol: fnoEligible ? symbol : null,
    expiry,
    asOf: historical ? toTs : undefined,
    enabled: authenticated && fnoEligible && !!expiry,
  });
  const atm = mtf.data?.atm_strike ?? null;
  const { strikeMin, strikeMax } = useMemo(() => {
    if (atm == null || atmWindow < 0) return { strikeMin: undefined, strikeMax: undefined };
    const w = effectiveAtmWindow(atmWindow); // one fewer strike each side than the picked N
    return { strikeMin: atm - w * strikeStep, strikeMax: atm + w * strikeStep };
  }, [atm, atmWindow, strikeStep]);

  const { points, loading, error } = useRatioTimeseries({
    symbol: fnoEligible ? symbol : null,
    expiry,
    bucket,
    fromTs,
    toTs,
    strikeMin,
    strikeMax,
    enabled: authenticated && fnoEligible && !!expiry,
  });

  const series: LineSeriesSpec[] = useMemo(() => {
    const ratioData = points
      .filter((p) => p.ratio != null)
      .map((p) => ({ time: istIsoToChartTime(p.ts), value: p.ratio as number }));
    const pcrData = points
      .filter((p) => p.pcr != null)
      .map((p) => ({ time: istIsoToChartTime(p.ts), value: p.pcr as number }));
    return [
      { id: "ratio", label: "Ratio (Call ÷ Put)", color: SERIES_COLORS.ratio, data: ratioData, priceFormat: (v) => v.toFixed(2) },
      { id: "pcr", label: "PCR (Put ÷ Call)", color: SERIES_COLORS.pcr, data: pcrData, priceFormat: (v) => v.toFixed(2) },
    ];
  }, [points]);

  const last = points.length ? points[points.length - 1] : null;
  const shown = hover?.time != null ? hover.values : { ratio: last?.ratio ?? null, pcr: last?.pcr ?? null };

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
        <div className="flex items-center gap-1">
          {BUCKETS.map((b) => (
            <button
              key={b}
              type="button"
              onClick={() => setBucket(b)}
              className={`pill ${bucket === b ? "pill-active" : ""}`}
            >
              {b}
            </button>
          ))}
        </div>
      </div>

      {!fnoEligible ? (
        <div className="panel p-6 text-sm text-muted">No F&amp;O contracts for {symbolDisplay}.</div>
      ) : (
        <div className="panel p-3">
          <div className="flex items-center justify-between px-1 pb-2 text-sm">
            <div className="font-medium text-accent">
              Ratio &amp; PCR over time · {bucket} · {atmWindow < 0 ? "all strikes" : `ATM ± ${effectiveAtmWindow(atmWindow)}`}
              {historical && <span className="text-amber-300/90"> · {selectedDate}</span>}
            </div>
            <div className="flex items-center gap-4 font-mono text-xs">
              <span style={{ color: SERIES_COLORS.ratio }}>Ratio {fmt2(shown.ratio)}</span>
              <span style={{ color: SERIES_COLORS.pcr }}>PCR {fmt2(shown.pcr)}</span>
            </div>
          </div>
          {error && <div className="px-1 pb-2 text-xs text-red-400">Failed to load: {error}</div>}
          <div className="relative">
            <TimeSeriesChart series={series} height={420} onCrosshair={setHover} />
            {loading && points.length === 0 && (
              <div className="absolute inset-0 flex items-center justify-center text-xs text-muted">Loading…</div>
            )}
            {!loading && points.length === 0 && (
              <div className="absolute inset-0 flex items-center justify-center text-xs text-muted">
                No ratio data for this window.
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
