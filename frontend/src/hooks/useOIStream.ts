import { useEffect, useRef, useState } from "react";
import { OIStream } from "../api/ws";
import type { OIChangeResponse, OptionChainFullResponse, Timeframe } from "../types";

export type StreamStatus = "connecting" | "open" | "closed" | "reconnecting" | "idle";

export interface UseOIStreamResult {
  data: OIChangeResponse | null;
  /** Live rich option chain + IV / PCR per strike (same WS as OI change). */
  optionChainData: OptionChainFullResponse | null;
  status: StreamStatus;
  /** Last server `error` frame on the OI stream (cleared on successful `oi_change` or new open). */
  streamError: string | null;
}

/**
 * Opens a live OI WebSocket stream.
 *
 * Pass `null` as *timeframe* to disable the connection entirely (e.g. while
 * waiting for auth). The hook will return `{ data: null, status: "idle" }` and
 * tear down any existing socket.
 *
 * Timeframe / expiry / symbol changes reuse the same socket via `set:` messages
 * instead of reconnecting, so the chart is not blanked on every control change.
 */
export function useOIStream(
  timeframe: Timeframe | null,
  expiry: string | null,
  symbol: string | null = null,
): UseOIStreamResult {
  const [data, setData] = useState<OIChangeResponse | null>(null);
  const [optionChainData, setOptionChainData] = useState<OptionChainFullResponse | null>(null);
  const [status, setStatus] = useState<StreamStatus>("idle");
  const [streamError, setStreamError] = useState<string | null>(null);
  const streamRef = useRef<OIStream | null>(null);
  const timeframeRef = useRef(timeframe);
  const expiryRef = useRef(expiry);
  const symbolRef = useRef(symbol);
  const prevControlsRef = useRef<{ tf: Timeframe | null; exp: string | null; sym: string | null }>({
    tf: null,
    exp: null,
    sym: null,
  });

  timeframeRef.current = timeframe;
  expiryRef.current = expiry;
  symbolRef.current = symbol;

  useEffect(() => {
    return () => {
      streamRef.current?.close();
      streamRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (timeframe === null) {
      streamRef.current?.close();
      streamRef.current = null;
      prevControlsRef.current = { tf: null, exp: null, sym: null };
      setStatus("idle");
      setStreamError(null);
      setData(null);
      setOptionChainData(null);
      return;
    }

    const handlers = {
      onMessage: (m: import("../api/ws").StreamMessage) => {
        if (m.type === "oi_change") {
          const tf = timeframeRef.current;
          const exp = expiryRef.current;
          const expiryOk = exp == null || m.data.expiry === exp;
          if (tf == null || m.data.timeframe !== tf || !expiryOk) {
            return;
          }
          setStreamError(null);
          setData(m.data);
        }
        if (m.type === "option_chain_full") {
          const tf = timeframeRef.current;
          const exp = expiryRef.current;
          const expiryOk = exp == null || m.data.expiry === exp;
          if (tf == null || m.data.timeframe !== tf || !expiryOk) {
            return;
          }
          setStreamError(null);
          setOptionChainData(m.data);
        }
      },
      onStatus: (st: Exclude<StreamStatus, "idle">) => {
        setStatus(st);
        if (st === "open") {
          setStreamError(null);
        }
      },
      onStreamError: (message: string, detail?: string) => {
        const extra = detail ? ` — ${detail}` : "";
        setStreamError(`${message}${extra}`);
      },
    };

    if (!streamRef.current) {
      streamRef.current = new OIStream(timeframe, expiry, handlers, symbol);
      prevControlsRef.current = { tf: timeframe, exp: expiry, sym: symbol };
      return;
    }

    const prev = prevControlsRef.current;
    if (prev.sym !== symbol) {
      streamRef.current.setSymbol(symbol);
      // Clear stale data so the UI doesn't flash the previous symbol's snapshot
      // while waiting for the new symbol's first frame.
      setData(null);
      setOptionChainData(null);
    }
    if (prev.exp !== expiry) {
      streamRef.current.setExpiry(expiry);
    }
    if (prev.tf !== timeframe) {
      streamRef.current.setTimeframe(timeframe);
    }
    prevControlsRef.current = { tf: timeframe, exp: expiry, sym: symbol };
  }, [timeframe, expiry, symbol]);

  return { data, optionChainData, status, streamError };
}
