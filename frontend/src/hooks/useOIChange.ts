import { useEffect, useState } from "react";
import { api } from "../api/rest";
import type { OIChangeResponse, Timeframe } from "../types";

/**
 * Fetches a one-shot OI change snapshot from REST.
 *
 * Pass `null` as *timeframe* to disable the fetch (e.g. while waiting for
 * auth or when expiry is not yet known).
 *
 * Refetches when **timeframe**, **expiry**, or **symbol** changes (or when the
 * stream becomes enabled after login). The WebSocket layers live updates on top,
 * but REST is the authoritative bootstrap for every timeframe so the chart paints
 * even if the live stream is slow, blocked, or has not yet pushed a frame for the
 * newly-selected timeframe.
 */
export function useOIChange(
  timeframe: Timeframe | null,
  expiry: string | null,
  symbol: string | null = null,
  /** Historical instant (ISO) — computes the timeframe as of a past date's close.
   *  Omit / null for the live latest snapshot. */
  asOf?: string | null,
) {
  const [data, setData] = useState<OIChangeResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const streamReady = timeframe !== null && expiry !== null;

  useEffect(() => {
    if (!streamReady) {
      setData(null);
      setError(null);
      setLoading(false);
      return;
    }

    let cancelled = false;
    setLoading(true);
    api
      .oiChange(timeframe!, expiry ?? undefined, symbol ?? undefined, asOf ?? undefined)
      .then((d) => {
        if (!cancelled) {
          setData(d);
          setError(null);
        }
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [timeframe, expiry, streamReady, symbol, asOf]);

  return { data, error, loading };
}
