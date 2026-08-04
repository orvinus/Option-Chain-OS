import { useMemo, useState } from "react";
import { AtmWindowSelect } from "../components/AtmWindowSelect";
import { DatePicker } from "../components/DatePicker";
import { ExpirySelect } from "../components/ExpirySelect";
import { FeedOfflineBanner } from "../components/FeedOfflineBanner";
import { SymbolSelect } from "../components/SymbolSelect";
import {
  TimeSeriesChart,
  type CrosshairInfo,
  type LinePoint,
  type LineSeriesSpec,
} from "../components/charts/TimeSeriesChart";
import { SERIES_COLORS } from "../components/charts/chartTheme";
import { istIsoToChartTime } from "../components/charts/chartTime";
import type { MarketContextValue } from "../hooks/useMarketContext";
import { compact, fmt2 } from "../utils/num";
import { effectiveAtmWindow } from "../utils/oiStrikeWindow";
import { useAvailableDates } from "../hooks/useAvailableDates";
import { useMultiTimeframe } from "../hooks/useMultiTimeframe";
import { useRatioTimeseries } from "../hooks/useRatioTimeseries";
import { isToday, isoForSessionMinuteOnDate, maxMinForDate } from "../utils/sessionTime";

// Timeframe pills: backend time_bucket sizes + a client-side "Full Day" cumulative mode.
const BUCKETS = ["1m", "5m", "10m", "15m", "30m", "full_day"] as const;
const BUCKET_LABEL: Record<string, string> = {
  "1m": "1m", "5m": "5m", "10m": "10m", "15m": "15m", "30m": "30m", full_day: "Full Day",
};
const ATM_MAX_WINDOW = 50;

export function RatioChartPage({ mc }: { mc: MarketContextValue }) {
  const {
    feedLive, dataReady, health, symbol, symbolGroups, switching, symbolError, handleSymbolChange,
    expiry, setExpiry, expiries, expiryError, fnoEligible, symbolDisplay,
    atmWindow, setAtmWindow, activeEntry,
  } = mc;
  const strikeStep = activeEntry?.strike_step ?? 50;

  const [bucket, setBucket] = useState<string>("5m");
  const [hover, setHover] = useState<CrosshairInfo | null>(null);
  const [selectedDate, setSelectedDate] = useState<string | null>(null);

  // "Full Day" is a client-side cumulative-since-open mode, not a backend interval —
  // fetch fine-grained 1m levels over the session and accumulate the change locally.
  const isFullDay = bucket === "full_day";
  const effBucket = isFullDay ? "1m" : bucket;

  const avail = useAvailableDates(
    fnoEligible ? symbol : null,
    expiry,
    dataReady && fnoEligible && !!expiry,
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
    enabled: dataReady && fnoEligible && !!expiry,
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
    bucket: effBucket,
    fromTs,
    toTs,
    strikeMin,
    strikeMax,
    enabled: dataReady && fnoEligible && !!expiry,
  });

  // Build the two ratio lines plus a per-time position lookup for the tooltip.
  // Full Day: cumulative Call/Put OI change since the session's first bucket (09:15),
  //   ratio = cumCall ÷ cumPut, inverse/PCR = cumPut ÷ cumCall.
  // Other buckets: the endpoint's per-bucket level ratios (unchanged behavior).
  const { series, positions } = useMemo(() => {
    const meta = new Map<number, { call: number; put: number }>();
    const ratioData: LinePoint[] = []; // Call ÷ Put
    const pcrData: LinePoint[] = []; // Put ÷ Call

    if (isFullDay) {
      const base = points[0];
      const baseCall = base?.total_call_oi ?? 0;
      const basePut = base?.total_put_oi ?? 0;
      for (const p of points) {
        const t = istIsoToChartTime(p.ts);
        const cumCall = p.total_call_oi - baseCall;
        const cumPut = p.total_put_oi - basePut;
        meta.set(t, { call: cumCall, put: cumPut });
        if (cumPut !== 0) ratioData.push({ time: t, value: cumCall / cumPut });
        if (cumCall !== 0) pcrData.push({ time: t, value: cumPut / cumCall });
      }
    } else {
      for (const p of points) {
        const t = istIsoToChartTime(p.ts);
        meta.set(t, { call: p.total_call_oi, put: p.total_put_oi });
        if (p.ratio != null) ratioData.push({ time: t, value: p.ratio });
        if (p.pcr != null) pcrData.push({ time: t, value: p.pcr });
      }
    }

    const series: LineSeriesSpec[] = [
      { id: "ratio", label: "Ratio (Call ÷ Put)", color: SERIES_COLORS.ratio, data: ratioData, priceFormat: (v) => v.toFixed(2) },
      { id: "pcr", label: "PCR — inverse (Put ÷ Call)", color: SERIES_COLORS.pcr, data: pcrData, priceFormat: (v) => v.toFixed(2) },
    ];
    return { series, positions: meta };
  }, [points, isFullDay]);

  // Legend readout: hover value, else the LAST PLOTTED value (matches the chart in
  // both level and cumulative modes — raw last.ratio/pcr would be level-only).
  const lastVal = (id: "ratio" | "pcr") => {
    const d = series.find((s) => s.id === id)?.data;
    return d && d.length ? d[d.length - 1].value : null;
  };
  const shown = hover?.time != null ? hover.values : { ratio: lastVal("ratio"), pcr: lastVal("pcr") };

  return (
    <div className="min-h-screen w-full max-w-[1500px] mx-auto px-4 md:px-6 py-3">
      {!feedLive && <FeedOfflineBanner />}
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
              {BUCKET_LABEL[b] ?? b}
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
              Ratio &amp; PCR over time · {BUCKET_LABEL[bucket] ?? bucket}
              {isFullDay && <span className="text-muted"> (cumulative since 09:15)</span>} · {atmWindow < 0 ? "all strikes" : `ATM ± ${effectiveAtmWindow(atmWindow)}`}
              {historical && <span className="text-amber-300/90"> · {selectedDate}</span>}
            </div>
            <div className="flex items-center gap-4 font-mono text-xs">
              <span style={{ color: SERIES_COLORS.ratio }}>Ratio {fmt2(shown.ratio)}</span>
              <span style={{ color: SERIES_COLORS.pcr }}>PCR {fmt2(shown.pcr)}</span>
            </div>
          </div>
          {error && <div className="px-1 pb-2 text-xs text-red-400">Failed to load: {error}</div>}
          <div className="relative">
            <TimeSeriesChart
              series={series}
              height={420}
              onCrosshair={setHover}
              extraTooltipRows={(t) => {
                const pos = positions.get(t);
                if (!pos) return [];
                return [
                  { label: isFullDay ? "Call positions (cum)" : "Call OI", value: compact(pos.call), color: SERIES_COLORS.call },
                  { label: isFullDay ? "Put positions (cum)" : "Put OI", value: compact(pos.put), color: SERIES_COLORS.put },
                ];
              }}
            />
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
