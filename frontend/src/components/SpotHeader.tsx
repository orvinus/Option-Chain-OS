import { useCallback, useState } from "react";
import { api } from "../api/rest";
import type { HealthResponse, NiftyCrossCheckResponse, OIChangeResponse } from "../types";
import type { StreamStatus } from "../hooks/useOIStream";

interface Props {
  data: OIChangeResponse | null;
  status: StreamStatus;
  health: HealthResponse | null;
  streamError: string | null;
  /** Display name of the active symbol (e.g. "NIFTY 50", "Reliance Industries"). */
  symbolDisplay: string;
  /** Raw symbol ticker. The NIFTY public cross-check button only renders for NIFTY. */
  symbolTicker: string;
}

/** NSE / snapshot times should read in IST regardless of the viewer's locale. */
const MARKET_TZ = "Asia/Kolkata";

function formatHeaderDate(asof: string | undefined | null): string {
  if (!asof) return "";
  try {
    return new Date(asof).toLocaleDateString("en-IN", {
      timeZone: MARKET_TZ,
      weekday: "short", day: "numeric", month: "short",
    });
  } catch { return ""; }
}

function formatTime(asof: string | undefined | null): string {
  if (!asof) return "";
  try {
    return new Date(asof).toLocaleTimeString("en-IN", {
      timeZone: MARKET_TZ,
      hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    });
  } catch { return ""; }
}

type StatusInfo = { dot: string; label: string; badge: string; shortHint: string };

function statusInfo(status: StreamStatus): StatusInfo {
  switch (status) {
    case "open":
      return {
        dot: "bg-green-500",
        label: "LIVE PUSH ON",
        badge: "border-green-500/30 text-green-400",
        shortHint: "WebSocket /ws/oi-stream",
      };
    case "connecting":
      return {
        dot: "bg-yellow-400 animate-pulse",
        label: "CONNECTING…",
        badge: "border-yellow-500/30 text-yellow-400",
        shortHint: "/ws/oi-stream",
      };
    case "reconnecting":
      return {
        dot: "bg-yellow-400 animate-pulse",
        label: "RECONNECTING…",
        badge: "border-amber-500/35 text-amber-300",
        shortHint: "Automatic backoff",
      };
    case "closed":
      return {
        dot: "bg-amber-600/90",
        label: "LIVE PUSH OFF",
        badge: "border-amber-600/40 text-amber-200/95",
        shortHint: "WS path /ws/oi-stream",
      };
    case "idle":
      return {
        dot: "bg-slate-600",
        label: "STREAM IDLE",
        badge: "border-slate-600/50 text-slate-400",
        shortHint: "Log in + expiry",
      };
    default:
      return {
        dot: "bg-gray-500",
        label: (status as string).toUpperCase(),
        badge: "border-gray-600/50 text-gray-400",
        shortHint: "",
      };
  }
}

/** Longer copy when the dashboard WebSocket is not open — avoids sounding like “all data is dead”. */
function livePushOffCaption(
  status: StreamStatus,
  health: HealthResponse | null,
  streamError: string | null,
): string | null {
  if (status === "open") return null;
  if (streamError) return null;
  if (status === "idle") {
    if (health?.authenticated) {
      return "Pick an expiry (or wait for expiries to load). Until then the live push socket stays closed; numbers can still appear from HTTP once you are logged in.";
    }
    return "After you log in, the app opens a WebSocket for push updates. The chart can still use REST snapshots.";
  }
  if (status === "connecting") {
    return "Opening the WebSocket to your API. AsOf / spot may already reflect the latest HTTP snapshot.";
  }
  if (status === "closed") {
    const parts = [
      "This only means the browser is not connected to the API WebSocket. OI rows can still load from /api (AsOf reflects that snapshot).",
      "Keep the backend running. In dev, Vite should proxy ws://…/ws to port 8000; if you use VITE_WS_BASE, it must match the same host as the API.",
    ];
    if (import.meta.env.DEV) {
      parts.push("Tip: DevTools → Network → WS → confirm /ws/oi-stream is not blocked or 403.");
    }
    return parts.join(" ");
  }
  return null;
}

function referenceSourceLabel(source: string): string {
  switch (source) {
    case "nseindia_allIndices":
      return "NSE India (allIndices API)";
    case "yahoo_nsei":
      return "Yahoo Finance (^NSEI, same index)";
    default:
      return source;
  }
}

export function SpotHeader({ data, status, health, streamError, symbolDisplay, symbolTicker }: Props) {
  const [cross, setCross] = useState<NiftyCrossCheckResponse | null>(null);
  const [crossLoading, setCrossLoading] = useState(false);
  const [crossErr, setCrossErr] = useState<string | null>(null);

  const runCrossCheck = useCallback(async () => {
    setCrossLoading(true);
    setCrossErr(null);
    try {
      setCross(await api.niftyCrossCheck());
    } catch (e) {
      setCross(null);
      setCrossErr(String(e));
    } finally {
      setCrossLoading(false);
    }
  }, []);

  const exchangeTs = data?.asof;
  const snapshotBuilt = data?.computed_at ?? data?.asof;
  const dateStr = formatHeaderDate(snapshotBuilt);
  const timeStr = formatTime(snapshotBuilt);
  const exchangeTimeStr = exchangeTs ? formatTime(exchangeTs) : "";
  const spot =
    health?.latest_spot != null && health.feed_connected
      ? health.latest_spot
      : data?.spot;
  const si = statusInfo(status);
  const offCaption = livePushOffCaption(status, health, streamError);

  return (
    <header className="flex flex-col gap-2 px-2 py-4">
      <div className="flex items-center justify-between gap-6 flex-wrap">
        <div className="flex flex-col">
          <h1 className="text-lg font-semibold tracking-tight">
            OI Change{dateStr ? ` on ${dateStr}` : ""}
          </h1>
          <span className="text-xs text-muted">{symbolDisplay} • Smart money intelligence</span>
        </div>

        <div className="flex items-center gap-5 flex-wrap">
          <div className="flex flex-col items-end gap-1 max-w-[220px]">
            <span className="text-xs text-muted uppercase tracking-wider">Spot</span>
            <span className="font-mono text-2xl font-bold tabular-nums text-foreground">
              {spot != null ? spot.toFixed(2) : "—"}
            </span>
            {symbolTicker === "NIFTY" && (
              <button
                type="button"
                onClick={() => void runCrossCheck()}
                disabled={crossLoading}
                className="text-[10px] text-accent hover:underline disabled:opacity-50"
              >
                {crossLoading ? "Checking public NIFTY 50…" : "Verify vs public NIFTY 50"}
              </button>
            )}
            {crossErr && (
              <span className="text-[10px] text-red-400 text-right leading-tight">{crossErr}</span>
            )}
            {cross && (
              <div className="text-[10px] text-muted text-right leading-snug space-y-0.5">
                <div>
                  Ref ({referenceSourceLabel(cross.reference_source)}):{" "}
                  <span className="font-mono text-foreground/90">
                    {cross.reference_last != null ? cross.reference_last.toFixed(2) : "—"}
                  </span>
                </div>
                {cross.our_spot != null && cross.reference_last != null && cross.diff_points != null && (
                  <div>
                    Δ {cross.diff_points >= 0 ? "+" : ""}
                    {cross.diff_points.toFixed(2)} pts
                    {cross.aligned === true && (
                      <span className="text-emerald-400 font-medium"> · within {cross.tolerance_points} pts</span>
                    )}
                    {cross.aligned === false && (
                      <span className="text-amber-400 font-medium"> · outside {cross.tolerance_points} pts</span>
                    )}
                  </div>
                )}
                {cross.reference_source === "yahoo_nsei" && cross.fetch_detail && (
                  <div className="text-slate-500 text-[9px]">NSE API unavailable; used fallback.</div>
                )}
              </div>
            )}
          </div>

          {timeStr && (
            <div className="flex flex-col items-end gap-0.5">
              <span className="text-xs text-muted uppercase tracking-wider">Snapshot updated</span>
              <span className="font-mono text-base tabular-nums">{timeStr}</span>
              {data?.computed_at && exchangeTimeStr && exchangeTimeStr !== timeStr && (
                <span className="text-[10px] text-muted/80 text-right max-w-[200px] leading-tight" title="Timestamp from the broker feed on option rows; often frozen after market close.">
                  Feed quote time: {exchangeTimeStr}
                </span>
              )}
            </div>
          )}

          <div className="flex flex-col items-end gap-1.5 max-w-md">
            <div
              className={`flex flex-col items-end gap-0.5 px-3 py-1.5 rounded-xl border bg-panel/60 ${si.badge}`}
              title={si.shortHint || undefined}
            >
              <div className="flex items-center gap-2">
                <span className={`w-2 h-2 rounded-full shrink-0 ${si.dot}`} />
                <span className="text-xs font-semibold tracking-wide">{si.label}</span>
              </div>
              {si.shortHint && (
                <span className="text-[10px] text-muted/85 font-normal tracking-normal text-right leading-tight">
                  {si.shortHint}
                </span>
              )}
            </div>
            {offCaption && (
              <p className="text-[11px] text-muted leading-snug text-right pl-1 border-r-2 border-amber-600/40 pr-2">
                {offCaption}
              </p>
            )}
          </div>
        </div>
      </div>

      {health && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted border-t border-border/60 pt-2">
          <span>
            Server IST <span className="font-mono text-foreground/90">{formatTime(health.now_ist)}</span>
            {" · "}
            <span className={health.nse_session_open ? "text-emerald-400 font-medium" : "text-slate-400"}>
              {health.nse_session_open ? "NSE session open" : "NSE session closed"}
            </span>
          </span>
          <span>
            XTS feed:{" "}
            <span className={health.feed_connected ? "text-emerald-400 font-medium" : "text-slate-400"}>
              {health.feed_connected ? "connected" : "disconnected"}
            </span>
          </span>
          <span>
            Last DB flush:{" "}
            <span className="font-mono text-foreground/90">
              {health.last_flush_at ? formatTime(health.last_flush_at) : "—"}
            </span>
          </span>
          <span className="text-slate-500">API run mode: {health.run_mode}</span>
        </div>
      )}

      {streamError && (
        <div className="text-xs text-red-300/95 border border-red-500/30 rounded-lg px-3 py-2 bg-red-950/30">
          <span className="font-semibold text-red-200">OI stream: </span>
          {streamError}
        </div>
      )}
    </header>
  );
}
