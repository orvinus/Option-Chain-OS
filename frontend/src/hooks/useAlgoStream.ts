/**
 * One live stream per Algo Config page, shared by every panel.
 *
 * Lifted to the page rather than owned per panel on purpose: sub-tabs are
 * conditionally mounted (`AlgoConfigPage.tsx`), so a panel-owned socket would
 * be torn down and re-handshaken on every tab switch — the same cold-refetch
 * storm the 30 s timers already caused. Held here, switching tabs re-renders
 * instantly from the newest frame.
 */
import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { AlgoStream } from "../api/algoWs";
import type { AlgoFrame, AlgoScopeParams, AlgoStreamStatus } from "../api/algoWs";

export interface UseAlgoStreamResult {
  frame: AlgoFrame | null;
  status: AlgoStreamStatus;
  error: string | null;
  /** True only when the backend says a real feed is behind these numbers. */
  live: boolean;
  /** Seconds since the frame was produced — drives the staleness readout. */
  ageS: number;
  /** Point the stream at one exact contract (the UMP chart's), or back to the
   *  zone's band pick with null. Provided by AlgoConfigPage only. */
  pinContract?: (c: { strike: number; optionType: "CE" | "PE" } | null) => void;
}

export function useAlgoStream(
  scope: AlgoScopeParams | null,
  enabled: boolean,
): UseAlgoStreamResult {
  const [frame, setFrame] = useState<AlgoFrame | null>(null);
  const [status, setStatus] = useState<AlgoStreamStatus>("closed");
  const [error, setError] = useState<string | null>(null);
  const [ageS, setAgeS] = useState(0);
  const ref = useRef<AlgoStream | null>(null);
  const lastAt = useRef<number>(0);

  const onFrame = useCallback((f: AlgoFrame) => {
    lastAt.current = Date.now();
    setAgeS(0);
    setError(f.error ?? null);
    setFrame(f);
  }, []);

  // Connect once; re-scope in place afterwards. Re-connecting on every
  // day/zone change would re-handshake several times while the user is still
  // clicking through the selectors.
  useEffect(() => {
    if (!enabled || !scope) {
      ref.current?.close();
      ref.current = null;
      setStatus("closed");
      return;
    }
    if (ref.current) {
      ref.current.setScope(scope);
      return;
    }
    ref.current = new AlgoStream(scope, {
      onFrame,
      onStatus: setStatus,
      onStreamError: (m) => setError(m),
    });
    return () => {
      ref.current?.close();
      ref.current = null;
    };
    // scope is compared field-wise below to avoid reconnecting on identity change
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    enabled,
    scope?.day,
    scope?.zone,
    scope?.symbol,
    scope?.expiry,
    scope?.strike,
    scope?.optionType,
    onFrame,
  ]);

  // A visible, honest age counter. Without it a frozen stream is
  // indistinguishable from a live one that happens not to be moving.
  useEffect(() => {
    if (!enabled) return;
    const t = window.setInterval(() => {
      if (lastAt.current) setAgeS(Math.round((Date.now() - lastAt.current) / 1000));
    }, 1000);
    return () => window.clearInterval(t);
  }, [enabled]);

  return {
    frame,
    status,
    error,
    live: !!frame?.live && status === "open",
    ageS,
  };
}

// ── page-level sharing ────────────────────────────────────────────────────
// The socket is opened once in AlgoConfigPage; deep children (the UMP chart,
// the engine panels) read the newest frame from here rather than having it
// drilled through EnginesPanel. Absent provider => undefined => those children
// simply render their REST snapshot, which is exactly what the Backtesting
// workspace wants (it passes livePoll={false} and has no live feed at all).
export const AlgoStreamContext = createContext<UseAlgoStreamResult | null>(null);

export function useAlgoStreamContext(): UseAlgoStreamResult | null {
  return useContext(AlgoStreamContext);
}
