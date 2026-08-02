import { useMemo, useState } from "react";
import { AtmWindowSelect } from "../components/AtmWindowSelect";
import { ChartIntervalBar } from "../components/ChartIntervalBar";
import { ConnectBanner } from "../components/ConnectBanner";
import { DatePicker } from "../components/DatePicker";
import { ExpirySelect } from "../components/ExpirySelect";
import { FeedOfflineBanner } from "../components/FeedOfflineBanner";
import { OICandleChart } from "../components/OICandleChart";
import { SpotHeader } from "../components/SpotHeader";
import { SymbolSelect } from "../components/SymbolSelect";
import { useAvailableDates } from "../hooks/useAvailableDates";
import { useMultiTimeframe } from "../hooks/useMultiTimeframe";
import { useOITimeseries } from "../hooks/useOITimeseries";
import { useOIStream } from "../hooks/useOIStream";
import type { MarketContextValue } from "../hooks/useMarketContext";
import type { ChartInterval } from "../utils/oiCandles";
import { atmRound, effectiveAtmWindow } from "../utils/oiStrikeWindow";
import { isToday, isoForSessionMinuteOnDate, maxMinForDate } from "../utils/sessionTime";

const ATM_MAX_WINDOW = 50;

export function ChartsPage({ mc }: { mc: MarketContextValue }) {
  const {
    authenticated, dataReady, authChecked, health, handleAuthenticated,
    symbol, symbolGroups, switching, symbolError, handleSymbolChange,
    expiry, setExpiry, expiries, expiryError,
    atmWindow, setAtmWindow,
    activeEntry, fnoEligible, symbolDisplay, liveSpot,
  } = mc;

  const [interval, setChartInterval] = useState<ChartInterval>(5);
  const [selectedDate, setSelectedDate] = useState<string | null>(null);

  const avail = useAvailableDates(
    fnoEligible ? symbol : null,
    expiry,
    dataReady && fnoEligible && !!expiry,
  );

  // A past date is read as a fixed full-session window; today/null stays live.
  const historical = selectedDate != null && !isToday(selectedDate, health);
  const fromTs = historical ? isoForSessionMinuteOnDate(selectedDate!, 0) : undefined;
  const toTs = historical ? isoForSessionMinuteOnDate(selectedDate!, maxMinForDate(selectedDate!, health)) : undefined;

  // Historical: resolve THAT day's spot (as of session close) so the ATM window is
  // centred on the selected day, not today. One-shot fetch, only while historical.
  const histMtf = useMultiTimeframe({
    symbol: historical && fnoEligible ? symbol : null,
    expiry: historical ? expiry : null,
    asOf: toTs,
    enabled: historical && dataReady && fnoEligible && !!expiry,
  });

  // Resolve the ATM ± N strike window from the spot + the symbol's strike step
  // (same ATM math as the backend: round(spot / step) * step). These bounds are
  // sent to /api/oi-timeseries which sums every strike inside [min, max].
  const step = activeEntry?.strike_step ?? 50;
  // NOTE: no `?? health.latest_spot` fallback. `liveSpot` is already guarded (feed
  // connected AND health.active_symbol === this symbol); the raw health value is the
  // GLOBALLY active symbol's price, so falling back to it centred this symbol's strike
  // window on another instrument's spot and sent a wrong [strikeMin,strikeMax] to
  // /api/oi-timeseries. The page already renders a "waiting for spot" state below.
  const spot = historical ? histMtf.data?.spot ?? null : liveSpot;
  const { strikeMin, strikeMax, atm } = useMemo(() => {
    if (spot == null) return { strikeMin: null, strikeMax: null, atm: null };
    const a = atmRound(spot, step);
    // "All" (atmWindow<0) → full window; otherwise one fewer strike each side than picked.
    const w = atmWindow < 0 ? ATM_MAX_WINDOW : effectiveAtmWindow(atmWindow);
    return { strikeMin: a - w * step, strikeMax: a + w * step, atm: a };
  }, [spot, step, atmWindow]);

  const ts = useOITimeseries(
    fnoEligible ? symbol : null,
    expiry,
    strikeMin,
    strikeMax,
    fromTs,
    toTs,
  );
  const points = ts.data?.points ?? null;

  // Open the live OI WebSocket so the "live wire" status badge at the top reflects
  // the real push connection (same socket the dashboard uses). We only consume the
  // connection status here — the charts are driven by the polled time-series.
  // No live push when viewing a past date — that session's data is closed.
  const live = useOIStream(!historical && authenticated && fnoEligible && expiry ? "5m" : null, expiry, fnoEligible ? symbol : null);

  // Real-time total Call/Put OI for the selected ATM ± N window. Prefer the live
  // WS frame (updates on every push); fall back to the latest polled time-series
  // point. Both sum the SAME [strikeMin, strikeMax] bounds the backend uses for
  // the time-series SQL, so the badge cannot disagree with the chart about which
  // strikes are included (previously the WS path re-derived its own ATM anchor).
  const liveOn = live.status === "open";
  const { callTotal, putTotal } = useMemo(() => {
    const rows =
      live.data && strikeMin != null && strikeMax != null
        ? live.data.rows.filter((r) => r.strike >= strikeMin && r.strike <= strikeMax)
        : [];
    if (rows.length > 0) {
      return {
        callTotal: rows.reduce((s, r) => s + r.call_oi, 0),
        putTotal: rows.reduce((s, r) => s + r.put_oi, 0),
      };
    }
    const last = points && points.length > 0 ? points[points.length - 1] : null;
    return {
      callTotal: last ? last.total_call_oi : null,
      putTotal: last ? last.total_put_oi : null,
    };
  }, [live.data, strikeMin, strikeMax, points]);

  if (!authChecked) {
    return (
      <div className="min-h-screen flex items-center justify-center text-muted text-sm">
        <div className="flex items-center gap-3">
          <svg className="w-5 h-5 animate-spin text-accent" viewBox="0 0 24 24" fill="none">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4l3-3-3-3v4a8 8 0 00-8 8h4z" />
          </svg>
          Connecting…
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen w-full max-w-[1500px] mx-auto px-4 md:px-6 py-3">
      <SpotHeader
        data={live.data}
        status={live.status}
        health={health}
        streamError={live.streamError}
        symbolDisplay={symbolDisplay}
        symbolTicker={symbol}
        atmStrike={atm}
        spot={spot}
      />

      <main className="flex flex-col gap-4">
        {/* Gate on OUR backend, not the broker — stored candles need no live session. */}
        {!dataReady ? (
          <ConnectBanner onAuthenticated={handleAuthenticated} />
        ) : (
          <>
            {!authenticated && <FeedOfflineBanner />}
            {/* ── Controls row ──────────────────────────────────── */}
            <div className="panel px-4 py-3 flex flex-col gap-3">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex flex-wrap items-center gap-3">
                  <div className="flex flex-col gap-1">
                    <SymbolSelect
                      groups={symbolGroups}
                      value={symbol}
                      onChange={(s) => void handleSymbolChange(s)}
                      switching={switching}
                      error={symbolError}
                    />
                    {symbolError && (
                      <span className="text-[10px] text-red-400 max-w-[260px] truncate" title={symbolError}>
                        {symbolError}
                      </span>
                    )}
                  </div>
                  <ChartIntervalBar value={interval} onChange={setChartInterval} />
                </div>
                {fnoEligible && (
                  <div className="flex flex-wrap items-center gap-3">
                    <DatePicker value={selectedDate} available={avail.dates} onChange={setSelectedDate} />
                    <ExpirySelect
                      expiries={expiries}
                      value={expiry}
                      onChange={setExpiry}
                      error={expiryError}
                    />
                  </div>
                )}
              </div>

              {historical && (
                <div className="text-[11px] text-amber-300/90">
                  Viewing historical session <b>{selectedDate}</b> — candles are built from stored snapshots
                  for that day (live stream paused); the ATM window is centred on that day's spot.
                </div>
              )}

              {fnoEligible && (
                <div className="flex flex-wrap items-center gap-4 pt-1 border-t border-border">
                  <span className="text-xs font-medium text-accent">Total OI Change</span>
                  <div className="flex items-center gap-2">
                    <span className="text-xs text-muted">Strikes ATM ±</span>
                    <AtmWindowSelect value={atmWindow} max={ATM_MAX_WINDOW} onChange={setAtmWindow} />
                  </div>
                  {atm != null && (
                    <span className="text-xs text-muted">
                      Summing strikes{" "}
                      <span className="font-mono text-foreground/90">{strikeMin}</span>–
                      <span className="font-mono text-foreground/90">{strikeMax}</span>{" "}
                      (ATM <span className="font-mono text-foreground/90">{atm}</span>)
                    </span>
                  )}
                </div>
              )}
            </div>

            {!fnoEligible && (
              <div className="panel p-6 text-sm text-muted border border-amber-500/20 flex flex-col gap-2">
                <div className="flex items-center gap-2 text-amber-300">
                  <svg className="w-4 h-4 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v2m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
                  </svg>
                  <b>No F&amp;O contracts available for {symbolDisplay}.</b>
                </div>
                <p className="leading-snug">OI time-series charts need option contracts; this instrument has none.</p>
              </div>
            )}

            {fnoEligible && (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <OICandleChart
                  points={points}
                  side="call"
                  interval={interval}
                  title="Call OI Change"
                  isLoading={ts.loading && !points}
                  liveTotal={callTotal}
                  liveOn={liveOn}
                />
                <OICandleChart
                  points={points}
                  side="put"
                  interval={interval}
                  title="Put OI Change"
                  isLoading={ts.loading && !points}
                  liveTotal={putTotal}
                  liveOn={liveOn}
                />
              </div>
            )}

            {fnoEligible && ts.error && !points && (
              <div className="panel p-4 text-sm text-red-400 border border-red-500/20">
                Failed to load OI time-series: {ts.error}
              </div>
            )}
            {fnoEligible && spot == null && (
              <div className="panel p-3 text-xs text-yellow-300/90 border border-yellow-500/25">
                Waiting for a live spot price to resolve the ATM strike window…
              </div>
            )}

            <p className="text-xs text-muted leading-snug px-1">
              Each chart sums the total Call / Put open interest across the selected ATM ± N strikes,
              plotted as <b>change since session open</b> (0 ±). 1 Min shows a line; 5/10/15/30 Min show
              OHLC candles bucketed from the per-minute series.
            </p>
          </>
        )}
      </main>

      <footer className="text-center text-xs text-muted py-6 mt-2">
        NIFTY 50 &amp; SENSEX OI-change intelligence • Powered by Shrilakshmi/Symphony XTS WebSocket
      </footer>
    </div>
  );
}
