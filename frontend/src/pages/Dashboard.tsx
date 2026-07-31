import { useCallback, useEffect, useMemo, useState } from "react";
import { AtmWindowSelect } from "../components/AtmWindowSelect";
import { ExpirySelect } from "../components/ExpirySelect";
import { KPIBar } from "../components/KPIBar";
import { OIChangeChart } from "../components/OIChangeChart";
import { SpotHeader } from "../components/SpotHeader";
import { SymbolSelect } from "../components/SymbolSelect";
import { TimeframeBar } from "../components/TimeframeBar";
import { TimeRangeSlider } from "../components/TimeRangeSlider";
import { DatePicker } from "../components/DatePicker";
import { ExportButton } from "../components/ExportButton";
import { buildSnapshotTable } from "../utils/exportData";
import { useOIChange } from "../hooks/useOIChange";
import { useOIChangeRange } from "../hooks/useOIChangeRange";
import { useOIStream } from "../hooks/useOIStream";
import { useAvailableDates } from "../hooks/useAvailableDates";
import type { MarketContextValue } from "../hooks/useMarketContext";
import type { Timeframe } from "../types";
import { filterOiRowsByAtmWindow } from "../utils/oiStrikeWindow";
import {
  SESSION_SPAN_MIN,
  clamp,
  isToday,
  isoForSessionMinuteOnDate,
  maxMinForDate,
  todayIstDate,
} from "../utils/sessionTime";

const ATM_MAX_WINDOW = 50;
const RANGE_DEFAULT_LOOKBACK_MIN = 30;

export function Dashboard({ mc }: { mc: MarketContextValue }) {
  const {
    authenticated, authChecked, health, connectError,
    symbol, symbolGroups, switching, symbolError, handleSymbolChange,
    expiry, setExpiry, expiries, expiryError,
    atmWindow, setAtmWindow,
    activeEntry, fnoEligible, symbolDisplay, liveSpot,
  } = mc;

  const [timeframe, setTimeframe] = useState<Timeframe>("5m");
  // Registry strike step (NIFTY 50, SENSEX 100) — drives ATM math and windowing.
  const strikeStep = activeEntry?.strike_step ?? 50;

  // This build (Ayush Bhai branch) ships only the OI Change view.
  // Custom time-range selection (minutes from 09:15). `rangeMode` switches the
  // chart from preset timeframes to an explicit [from, to] window; `toAtLive`
  // keeps the right handle pinned to "now" so the window stays live until the
  // user drags it back.
  const [rangeMode, setRangeMode] = useState(false);
  const [fromMin, setFromMin] = useState(0);
  const [toMin, setToMin] = useState(SESSION_SPAN_MIN);
  const [toAtLive, setToAtLive] = useState(true);
  // Selected historical trading date (null = live / today). A past date forces an
  // explicit fixed window and reads exclusively from the range fetch.
  const [selectedDate, setSelectedDate] = useState<string | null>(null);

  const avail = useAvailableDates(
    fnoEligible ? symbol : null,
    expiry,
    authenticated && fnoEligible && !!expiry,
  );
  const effectiveDate = selectedDate ?? todayIstDate(health);
  const historical = selectedDate != null && !isToday(selectedDate, health);

  // The preset timeframe drives the view live, OR — when a past date is picked
  // and the user hasn't opened a custom window — "as of that date's close", so it
  // matches the Multi-TF grid's row for the same timeframe/date. Only an explicit
  // custom window (rangeMode) disables the preset fetch.
  const asOfTs = historical && effectiveDate
    ? isoForSessionMinuteOnDate(effectiveDate, maxMinForDate(effectiveDate, health))
    : undefined;
  const activeTimeframe: Timeframe | null =
    authenticated && expiry && fnoEligible && !rangeMode ? timeframe : null;
  const initial = useOIChange(activeTimeframe, expiry, fnoEligible ? symbol : null, asOfTs);
  // Live WS only for the current session; a historical date reads the REST as-of snapshot.
  const live = useOIStream(historical ? null : activeTimeframe, expiry, fnoEligible ? symbol : null);

  // ── Custom time-range / historical mode ──────────────────────────────────
  // maxMin: today clamps to the live edge; a past date exposes the full session.
  const maxMin = useMemo(() => maxMinForDate(effectiveDate, health), [effectiveDate, health]);

  // Live-edge parking (today, not in range mode): pre-position the slider handles.
  useEffect(() => {
    if (rangeMode || historical) return;
    setToMin(maxMin);
    setFromMin(clamp(maxMin - RANGE_DEFAULT_LOOKBACK_MIN, 0, maxMin));
  }, [maxMin, rangeMode, historical]);

  // Right handle pinned to "now" (today only): keep it at maxMin as time advances.
  useEffect(() => {
    if (historical || !rangeMode || !toAtLive) return;
    setToMin(maxMin);
    setFromMin((f) => Math.min(f, Math.max(0, maxMin - 1)));
  }, [maxMin, rangeMode, toAtLive, historical]);

  // Build timestamps on the EFFECTIVE date (not just today) — the blank-chart fix.
  const rangeFromTs = effectiveDate ? isoForSessionMinuteOnDate(effectiveDate, fromMin) : null;
  const liveEdge = !historical && toAtLive; // open-ended only for the current session
  const rangeToTs = liveEdge ? null : effectiveDate ? isoForSessionMinuteOnDate(effectiveDate, toMin) : null;
  // Only an explicit custom window reads the range fetch; a plain historical date
  // uses the preset timeframe as-of close (presetData) so it matches Multi-TF.
  const windowActive = rangeMode;
  const rangeActive = windowActive && authenticated && fnoEligible && !!expiry && !!rangeFromTs;
  const range = useOIChangeRange(
    rangeFromTs,
    rangeToTs,
    expiry,
    fnoEligible ? symbol : null,
    Boolean(rangeActive),
  );

  const handleSliderChange = useCallback(
    (from: number, to: number) => {
      setRangeMode(true);
      setFromMin(from);
      setToMin(to);
      setToAtLive(!historical && to >= maxMin);
    },
    [maxMin, historical],
  );
  const handleSliderClear = useCallback(() => {
    if (historical) {
      // Keep the historical date; reset the window to the full session.
      setFromMin(0);
      setToMin(SESSION_SPAN_MIN);
      setToAtLive(false);
      return;
    }
    setRangeMode(false);
    setToAtLive(true);
  }, [historical]);
  const handleTimeframeChange = useCallback((tf: Timeframe) => {
    // A preset timeframe shows that timeframe live, or — when a past date is
    // selected — as of that date's close. Keep the date; just exit any custom window.
    setTimeframe(tf);
    setRangeMode(false);
  }, []);
  const handleDateChange = useCallback(
    (date: string | null) => {
      setSelectedDate(date);
      const isPast = date != null && !isToday(date, health);
      // A past date defaults to the selected TIMEFRAME as of that day's close
      // (matches the Multi-TF grid). Range mode is entered only if the user drags
      // the slider; reset the slider bounds to the full session for when they do.
      setRangeMode(false);
      setToAtLive(!isPast);
      if (isPast) {
        setFromMin(0);
        setToMin(SESSION_SPAN_MIN);
      }
    },
    [health],
  );

  const presetData = useMemo(() => {
    const liveOk =
      live.data != null &&
      activeTimeframe != null &&
      live.data.timeframe === activeTimeframe &&
      live.data.expiry === expiry;
    const initialOk =
      initial.data != null &&
      activeTimeframe != null &&
      initial.data.timeframe === activeTimeframe &&
      initial.data.expiry === expiry;
    return (liveOk ? live.data : null) ?? (initialOk ? initial.data : null);
  }, [live.data, initial.data, activeTimeframe, expiry]);

  // In custom-range / historical mode the chart is driven by the range fetch;
  // otherwise by the preset-timeframe live/REST data.
  const data = windowActive ? range.data : presetData;

  const hasMatchingSnapshot = useMemo(() => {
    if (activeTimeframe == null || expiry == null) return false;
    const ok = (d: typeof live.data) =>
      d != null && d.timeframe === activeTimeframe && d.expiry === expiry;
    return ok(live.data) || ok(initial.data);
  }, [live.data, initial.data, activeTimeframe, expiry]);

  const isLoading = windowActive
    ? range.loading && !range.data
    : initial.loading && !hasMatchingSnapshot;

  // Spot to use for ATM centring and display. A HISTORICAL session must use that
  // day's stored spot — using today's live price re-centred the strike window (and
  // the header) on a price the selected session never traded at.
  const effSpot = historical ? data?.spot ?? null : liveSpot;

  const noOiChangeInAtmWindow = useMemo(() => {
    if (!data) return false;
    const wr = filterOiRowsByAtmWindow(data.rows, effSpot ?? data.spot ?? null, atmWindow, strikeStep);
    return wr.length > 0 && wr.every((r) => r.call_oi_change === 0 && r.put_oi_change === 0);
  }, [data, atmWindow, effSpot, strikeStep]);

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
        data={data}
        status={live.status}
        health={health}
        streamError={live.streamError}
        symbolDisplay={symbolDisplay}
        symbolTicker={symbol}
        spot={effSpot}
      />

      <main className="flex flex-col gap-4">
        {!authenticated ? (
          <div className="panel p-8 flex flex-col items-center gap-4 max-w-md mx-auto w-full text-center border border-accent/30">
            <svg className="w-8 h-8 animate-spin text-accent" viewBox="0 0 24 24" fill="none">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4l3-3-3-3v4a8 8 0 00-8 8h4z" />
            </svg>
            <div>
              <h2 className="text-lg font-semibold text-foreground">Connecting to broker…</h2>
              <p className="mt-1 text-sm text-muted">
                Starting live option-chain ingestion via the XTS API key in .env.
              </p>
            </div>
            {connectError && (
              <div className="rounded-lg bg-red-500/10 border border-red-500/30 px-3 py-2 text-sm text-red-400">
                {connectError}
                <div className="text-xs text-muted mt-1">Retrying automatically…</div>
              </div>
            )}
          </div>
        ) : (
          <>
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
                  <TimeframeBar value={rangeMode ? null : timeframe} onChange={handleTimeframeChange} />
                </div>
                {fnoEligible && (
                  <div className="flex flex-wrap items-center gap-3">
                    <DatePicker value={selectedDate} available={avail.dates} onChange={handleDateChange} />
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
                  Viewing historical session <b>{effectiveDate}</b> —{" "}
                  {rangeMode ? "custom window" : `${timeframe} as of the session close`}, read from stored snapshots.
                </div>
              )}

              {fnoEligible && (
                <div className="flex flex-wrap items-center gap-4 pt-1 border-t border-border">
                  <span className="text-xs font-medium text-accent">OI Change</span>
                  <div className="flex items-center gap-2">
                    <span className="text-xs text-muted">Strikes ATM ±</span>
                    <AtmWindowSelect value={atmWindow} max={ATM_MAX_WINDOW} onChange={setAtmWindow} />
                  </div>
                  <div className="ml-auto">
                    <ExportButton
                      getTable={() => (data ? buildSnapshotTable(data) : null)}
                      filenameBase={`oi-change_${symbol}_${effectiveDate ?? "live"}_${historical ? "hist" : timeframe}`}
                      disabled={!data}
                    />
                  </div>
                </div>
              )}

              {fnoEligible && maxMin >= 2 && (
                <div className="pt-1 border-t border-border">
                  <TimeRangeSlider
                    fromMin={fromMin}
                    toMin={toMin}
                    maxMin={maxMin}
                    active={rangeMode}
                    onChange={handleSliderChange}
                    onClear={handleSliderClear}
                  />
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
                <p className="leading-snug">
                  This instrument is either spot-only on NSE or not yet listed for derivatives.
                  Live spot updates appear in the header above; OI, IV and option-chain panels are hidden because there is no contract data to compute them from.
                </p>
              </div>
            )}

            {fnoEligible && (
              <KPIBar data={data} mode="change" atmWindow={atmWindow} liveSpot={effSpot} strikeStep={strikeStep} />
            )}

            {fnoEligible && (
              <OIChangeChart
                data={data}
                mode="change"
                atmWindow={atmWindow}
                isLoading={isLoading}
                underlyingLabel={symbolDisplay}
                liveSpot={effSpot}
                nseSessionOpen={health?.nse_session_open}
                strikeStep={strikeStep}
              />
            )}

            {/* Surface the error from whichever fetch is actually driving the view.
                The custom-window/historical path uses `range`, whose failures were
                never rendered — the chart just said "Waiting for data…" forever. */}
            {fnoEligible && (windowActive ? range.error : initial.error) && !data && (
              <div className="panel p-4 text-sm text-red-400 border border-red-500/20">
                Failed to load: {initial.error}
              </div>
            )}
            {fnoEligible && expiryError && expiries.length === 0 && (
              <div className="panel p-4 text-sm text-red-400 border border-red-500/20">
                Error loading expiries: {expiryError}
              </div>
            )}
            {fnoEligible &&
              data &&
              data.rows.length > 0 &&
              noOiChangeInAtmWindow && (
              <div className="panel p-3 text-xs text-yellow-300/90 border border-yellow-500/25 flex items-start gap-2">
                <svg className="w-4 h-4 shrink-0 mt-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v2m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
                </svg>
                <span>
                  <b>No OI change in this window for any strike.</b>{" "}
                  {health?.nse_session_open ? (
                    <>
                      NSE session is <b>open</b> (server clock), so this usually means the feed is quiet, OI has not moved
                      versus the comparison snapshot, or DB flushes are stale — check <b>XTS feed</b>, <b>Last DB flush</b>,
                      and <b>WS LIVE</b> in the header. Try a longer timeframe (OI refreshes from the exchange ~once/min).
                    </>
                  ) : (
                    <>
                      Outside the regular cash/F&amp;O window (Mon–Fri <b>9:15 AM – 3:30 PM IST</b>), brokers often expose
                      static end-of-day style OI, so intraday deltas stay flat.
                    </>
                  )}
                </span>
              </div>
            )}
          </>
        )}
      </main>

      <footer className="text-center text-xs text-muted py-6 mt-2">
        NIFTY 50 &amp; SENSEX OI-change intelligence • Powered by Shrilakshmi/Symphony XTS WebSocket
      </footer>
    </div>
  );
}
