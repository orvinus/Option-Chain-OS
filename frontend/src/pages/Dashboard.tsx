import { useCallback, useEffect, useMemo, useState } from "react";
import { AtmWindowSelect } from "../components/AtmWindowSelect";
import { api } from "../api/rest";
import { ConnectBanner } from "../components/ConnectBanner";
import { ExpirySelect } from "../components/ExpirySelect";
import { KPIBar } from "../components/KPIBar";
import { NetOICalculator } from "../components/NetOICalculator";
import { OIChangeChart, type OIMode } from "../components/OIChangeChart";
import { SpotHeader } from "../components/SpotHeader";
import { SymbolSelect } from "../components/SymbolSelect";
import { TimeframeBar } from "../components/TimeframeBar";
import { TimeRangeSlider } from "../components/TimeRangeSlider";
import { useOIChange } from "../hooks/useOIChange";
import { useOIChangeRange } from "../hooks/useOIChangeRange";
import { useOptionChainFull } from "../hooks/useOptionChainFull";
import { useOIStream } from "../hooks/useOIStream";
import type {
  HealthResponse,
  SymbolEntry,
  SymbolSectorGroup,
  Timeframe,
} from "../types";
import { filterOiRowsByAtmWindow } from "../utils/oiStrikeWindow";

const HEALTH_POLL_MS = 3_000;
const EXPIRY_POLL_MS = 30_000;
const ATM_MAX_WINDOW = 50;

// NSE regular session in minutes-from-midnight (IST). The custom-range slider
// spans 09:15 → 15:30 (375 minutes).
const SESSION_OPEN_MIN = 9 * 60 + 15;
const SESSION_CLOSE_MIN = 15 * 60 + 30;
const SESSION_SPAN_MIN = SESSION_CLOSE_MIN - SESSION_OPEN_MIN; // 375
const RANGE_DEFAULT_LOOKBACK_MIN = 30;

const clamp = (v: number, lo: number, hi: number) => Math.min(Math.max(v, lo), hi);

/** Parse health.now_ist ("YYYY-MM-DDTHH:MM:SS...+05:30") into IST date + minutes-from-open. */
function parseNowIst(nowIst: string | undefined): { date: string; sinceOpen: number } | null {
  if (!nowIst || nowIst.length < 16) return null;
  const date = nowIst.slice(0, 10);
  const hh = Number(nowIst.slice(11, 13));
  const mm = Number(nowIst.slice(14, 16));
  if (Number.isNaN(hh) || Number.isNaN(mm)) return null;
  return { date, sinceOpen: hh * 60 + mm - SESSION_OPEN_MIN };
}

/** Build an IST ISO timestamp for a given minutes-from-open on the trading date. */
function isoForSessionMinute(dateStr: string, min: number): string {
  const total = SESSION_OPEN_MIN + Math.round(min);
  const hh = Math.floor(total / 60);
  const mm = total % 60;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${dateStr}T${p(hh)}:${p(mm)}:00+05:30`;
}

export type DashTab = "oi_change" | "oi_absolute" | "net_oi";

const DASH_TABS: { id: DashTab; label: string }[] = [
  { id: "oi_change", label: "OI Change" },
  { id: "oi_absolute", label: "OI Absolute" },
  { id: "net_oi", label: "Net OI Calculator" },
];

function flattenSymbols(groups: SymbolSectorGroup[]): Record<string, SymbolEntry> {
  const out: Record<string, SymbolEntry> = {};
  for (const g of groups) {
    for (const s of g.symbols) {
      out[s.symbol] = s;
    }
  }
  return out;
}

export function Dashboard() {
  const [timeframe, setTimeframe] = useState<Timeframe>("5m");
  const [expiry, setExpiry] = useState<string | null>(null);
  const [expiries, setExpiries] = useState<string[]>([]);
  const [expiryError, setExpiryError] = useState<string | null>(null);

  const [symbol, setSymbol] = useState<string>("NIFTY");
  const [symbolGroups, setSymbolGroups] = useState<SymbolSectorGroup[]>([]);
  const [switching, setSwitching] = useState(false);
  const [symbolError, setSymbolError] = useState<string | null>(null);

  const [dashTab, setDashTab] = useState<DashTab>("oi_change");
  const oiMode: OIMode = dashTab === "oi_absolute" ? "absolute" : "change";

  // Custom time-range selection (minutes from 09:15). `rangeMode` switches the
  // chart from preset timeframes to an explicit [from, to] window; `toAtLive`
  // keeps the right handle pinned to "now" so the window stays live until the
  // user drags it back.
  const [rangeMode, setRangeMode] = useState(false);
  const [fromMin, setFromMin] = useState(0);
  const [toMin, setToMin] = useState(SESSION_SPAN_MIN);
  const [toAtLive, setToAtLive] = useState(true);

  const [atmWindow, setAtmWindow] = useState<number>(5);

  const [authenticated, setAuthenticated] = useState(false);
  const [authChecked, setAuthChecked] = useState(false);
  const [health, setHealth] = useState<HealthResponse | null>(null);

  const symbolIndex = useMemo(() => flattenSymbols(symbolGroups), [symbolGroups]);
  const activeEntry: SymbolEntry | undefined = symbolIndex[symbol];
  const fnoEligible = activeEntry?.fno_eligible ?? true;
  const symbolDisplay = activeEntry?.display ?? symbol;

  // Poll /api/health for auth, market session, and ingestion
  useEffect(() => {
    let cancelled = false;
    const check = async () => {
      try {
        const h = await api.health();
        if (!cancelled) {
          setHealth(h);
          setAuthenticated(h.authenticated);
          setAuthChecked(true);
          // Sync local symbol with backend if it changed out-of-band (e.g. server restart).
          if (h.active_symbol && h.active_symbol !== symbol) {
            setSymbol(h.active_symbol);
          }
        }
      } catch {
        if (!cancelled) setAuthChecked(true);
      }
    };
    void check();
    const id = setInterval(() => void check(), HEALTH_POLL_MS);
    return () => { cancelled = true; clearInterval(id); };
    // symbol intentionally excluded from deps — we only want to sync once on initial mount/poll.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Load the symbol registry once authenticated; retry every 3s on transient failure
  // or when groups is empty (e.g. backend came up after frontend).
  useEffect(() => {
    if (!authenticated) return;
    let cancelled = false;
    const load = () => {
      api.symbols().then((res) => {
        if (cancelled) return;
        setSymbolGroups(res.groups);
        setSymbolError(null);
        if (res.active_symbol) setSymbol(res.active_symbol);
      }).catch((e: unknown) => {
        if (!cancelled) setSymbolError(String(e));
      });
    };
    load();
    const id = setInterval(() => {
      // Re-fetch until we have at least one group; thereafter stop polling.
      setSymbolGroups((curr) => {
        if (curr.length === 0) load();
        return curr;
      });
    }, 3_000);
    return () => { cancelled = true; clearInterval(id); };
  }, [authenticated]);

  // Load expiries for the current symbol when it changes (or auth flips on).
  useEffect(() => {
    if (!authenticated) return;
    if (!fnoEligible) {
      setExpiries([]);
      setExpiry(null);
      setExpiryError(null);
      return;
    }
    let cancelled = false;
    const tick = async () => {
      try {
        const res = await api.expiries(symbol);
        if (cancelled) return;
        setExpiries(res.expiries);
        setExpiryError(null);
        setExpiry((curr) => {
          if (curr && res.expiries.includes(curr)) return curr;
          return res.expiries[0] ?? null;
        });
      } catch (e) { if (!cancelled) setExpiryError(String(e)); }
    };
    void tick();
    const id = setInterval(() => void tick(), EXPIRY_POLL_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, [authenticated, symbol, fnoEligible]);

  const handleSymbolChange = useCallback(async (next: string) => {
    if (next === symbol) return;
    setSwitching(true);
    setSymbolError(null);
    // Optimistic UI: clear the chart so the user knows a switch is in flight.
    setExpiry(null);
    setExpiries([]);
    try {
      const res = await api.setActiveSymbol(next);
      setSymbol(res.symbol);
      setExpiries(res.expiries);
      setExpiry(res.expiries[0] ?? null);
    } catch (e) {
      setSymbolError(String(e));
    } finally {
      setSwitching(false);
    }
  }, [symbol]);

  // Drive the streaming hooks only when we have a valid F&O context.
  const activeTimeframe: Timeframe | null = authenticated && expiry && fnoEligible ? timeframe : null;
  const initial = useOIChange(activeTimeframe, expiry, fnoEligible ? symbol : null);
  const initialOc = useOptionChainFull(activeTimeframe, expiry, fnoEligible ? symbol : null);
  const live = useOIStream(activeTimeframe, expiry, fnoEligible ? symbol : null);

  // ── Custom time-range mode ───────────────────────────────────────────────
  const nowInfo = useMemo(() => parseNowIst(health?.now_ist), [health?.now_ist]);
  const maxMin = useMemo(
    () => (nowInfo ? clamp(nowInfo.sinceOpen, 0, SESSION_SPAN_MIN) : SESSION_SPAN_MIN),
    [nowInfo],
  );

  // Not in range mode: park the handles at the live edge (last N minutes) so the
  // slider is pre-positioned when the user grabs it.
  useEffect(() => {
    if (rangeMode) return;
    setToMin(maxMin);
    setFromMin(clamp(maxMin - RANGE_DEFAULT_LOOKBACK_MIN, 0, maxMin));
  }, [maxMin, rangeMode]);

  // Right handle pinned to "now": keep it pinned to maxMin as the session advances.
  useEffect(() => {
    if (!rangeMode || !toAtLive) return;
    setToMin(maxMin);
    setFromMin((f) => Math.min(f, Math.max(0, maxMin - 1)));
  }, [maxMin, rangeMode, toAtLive]);

  const rangeFromTs = nowInfo ? isoForSessionMinute(nowInfo.date, fromMin) : null;
  const rangeToTs = toAtLive ? null : nowInfo ? isoForSessionMinute(nowInfo.date, toMin) : null;
  const rangeActive = rangeMode && authenticated && fnoEligible && !!expiry && !!rangeFromTs;
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
      setToAtLive(to >= maxMin);
    },
    [maxMin],
  );
  const handleSliderClear = useCallback(() => {
    setRangeMode(false);
    setToAtLive(true);
  }, []);
  const handleTimeframeChange = useCallback((tf: Timeframe) => {
    setTimeframe(tf);
    setRangeMode(false);
    setToAtLive(true);
  }, []);

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

  // In custom-range mode the chart is driven by the range fetch; otherwise by
  // the preset-timeframe live/REST data.
  const data = rangeMode ? range.data : presetData;

  const optionChainMerged = useMemo(() => {
    const liveOk =
      live.optionChainData != null &&
      activeTimeframe != null &&
      live.optionChainData.timeframe === activeTimeframe &&
      live.optionChainData.expiry === expiry;
    const initialOk =
      initialOc.data != null &&
      activeTimeframe != null &&
      initialOc.data.timeframe === activeTimeframe &&
      initialOc.data.expiry === expiry;
    return (liveOk ? live.optionChainData : null) ?? (initialOk ? initialOc.data : null);
  }, [live.optionChainData, initialOc.data, activeTimeframe, expiry]);

  const hasMatchingSnapshot = useMemo(() => {
    if (activeTimeframe == null || expiry == null) return false;
    const ok = (d: typeof live.data) =>
      d != null && d.timeframe === activeTimeframe && d.expiry === expiry;
    return ok(live.data) || ok(initial.data);
  }, [live.data, initial.data, activeTimeframe, expiry]);

  const hasMatchingOptionChain = useMemo(() => {
    if (activeTimeframe == null || expiry == null) return false;
    const ok = (d: typeof live.optionChainData) =>
      d != null && d.timeframe === activeTimeframe && d.expiry === expiry;
    return ok(live.optionChainData) || ok(initialOc.data);
  }, [live.optionChainData, initialOc.data, activeTimeframe, expiry]);

  const isLoading = rangeMode
    ? range.loading && !range.data
    : initial.loading && !hasMatchingSnapshot;
  const isLoadingOc = initialOc.loading && !hasMatchingOptionChain;

  const liveSpot =
    health?.feed_connected && health.latest_spot != null && health.active_symbol === symbol
      ? health.latest_spot
      : null;

  const showChart = dashTab === "oi_change" || dashTab === "oi_absolute";

  const noOiChangeInAtmWindow = useMemo(() => {
    if (!showChart || !data || oiMode !== "change") return false;
    const wr = filterOiRowsByAtmWindow(data.rows, liveSpot ?? data.spot ?? null, atmWindow);
    return wr.length > 0 && wr.every((r) => r.call_oi_change === 0 && r.put_oi_change === 0);
  }, [data, oiMode, atmWindow, liveSpot, showChart]);

  const handleAuthenticated = useCallback(() => {
    setAuthenticated(true);
  }, []);

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
      />

      <main className="flex flex-col gap-4">
        {!authenticated ? (
          <ConnectBanner onAuthenticated={handleAuthenticated} />
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
                  <ExpirySelect
                    expiries={expiries}
                    value={expiry}
                    onChange={setExpiry}
                    error={expiryError}
                  />
                )}
              </div>

              {fnoEligible && (
                <div className="flex flex-wrap items-center gap-4 pt-1 border-t border-border">
                  <div className="flex flex-wrap items-center gap-1 bg-surface/50 rounded-lg p-0.5">
                    {DASH_TABS.map((t) => (
                      <button
                        key={t.id}
                        type="button"
                        onClick={() => setDashTab(t.id)}
                        className={`px-2.5 py-1.5 rounded-md text-xs font-medium transition-all ${
                          dashTab === t.id
                            ? "bg-accent text-black shadow"
                            : "text-muted hover:text-foreground"
                        }`}
                      >
                        {t.label}
                      </button>
                    ))}
                  </div>

                  <div className="flex items-center gap-2">
                    <span className="text-xs text-muted">Strikes ATM ±</span>
                    <AtmWindowSelect value={atmWindow} max={ATM_MAX_WINDOW} onChange={setAtmWindow} />
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

            {fnoEligible && dashTab === "oi_change" && (
              <KPIBar data={data} mode={oiMode} atmWindow={atmWindow} liveSpot={liveSpot} />
            )}

            {fnoEligible && showChart && (
              <OIChangeChart
                data={data}
                mode={oiMode}
                atmWindow={atmWindow}
                isLoading={isLoading}
                underlyingLabel={symbolDisplay}
                liveSpot={liveSpot}
                nseSessionOpen={health?.nse_session_open}
              />
            )}

            {fnoEligible && dashTab === "net_oi" && (
              <NetOICalculator
                data={optionChainMerged}
                liveSpot={liveSpot}
                atmWindow={atmWindow}
                isLoading={isLoadingOc}
              />
            )}

            {fnoEligible && initial.error && !data && (
              <div className="panel p-4 text-sm text-red-400 border border-red-500/20">
                Failed to load: {initial.error}
              </div>
            )}
            {fnoEligible && initialOc.error && !optionChainMerged && dashTab === "net_oi" && (
              <div className="panel p-4 text-sm text-red-400 border border-red-500/20">
                Failed to load option chain: {initialOc.error}
              </div>
            )}
            {fnoEligible && expiryError && expiries.length === 0 && (
              <div className="panel p-4 text-sm text-red-400 border border-red-500/20">
                Error loading expiries: {expiryError}
              </div>
            )}
            {fnoEligible && showChart &&
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
                      and <b>WS LIVE</b> in the header. Try another timeframe or wait for the next minute bucket.
                      Switch to <b>OI Absolute</b> tab to confirm snapshots are updating.
                    </>
                  ) : (
                    <>
                      Outside the regular cash/F&amp;O window (Mon–Fri <b>9:15 AM – 3:30 PM IST</b>), brokers often expose
                      static end-of-day style OI, so intraday deltas stay flat. Switch to <b>OI Absolute</b> tab for the
                      distribution.
                    </>
                  )}
                </span>
              </div>
            )}
            {fnoEligible && showChart && data && oiMode === "absolute" && (
              <div className="panel p-2 text-xs text-slate-400/70 border border-slate-700/30 flex items-center gap-2">
                <svg className="w-3.5 h-3.5 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M13 16h-1v-4h-1m1-4h.01" />
                </svg>
                Absolute OI shows the <b>latest snapshot</b> — values are identical across timeframes. Timeframe only affects <b>OI Change</b> calculations.
              </div>
            )}
          </>
        )}
      </main>

      <footer className="text-center text-xs text-muted py-6 mt-2">
        Multi-symbol option-chain intelligence • Powered by Shrilakshmi/Symphony XTS WebSocket
      </footer>
    </div>
  );
}
