import { useEffect, useState } from "react";
import { api } from "../api/rest";
import type { OITimeseriesResponse } from "../types";

const POLL_MS = 20_000;

/**
 * Fetches the total Call/Put OI time-series for the Charts page and polls so the
 * series extends as new minutes flush during the session.
 *
 * Pass `null` for any of *expiry* / *strikeMin* / *strikeMax* (or `null` symbol)
 * to disable the fetch — e.g. while spot/ATM is unknown or auth is pending.
 * Refetches whenever the symbol, expiry or strike window changes.
 */
export function useOITimeseries(
  symbol: string | null,
  expiry: string | null,
  strikeMin: number | null,
  strikeMax: number | null,
) {
  const [data, setData] = useState<OITimeseriesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const enabled =
    symbol !== null && expiry !== null && strikeMin !== null && strikeMax !== null;

  useEffect(() => {
    if (!enabled) {
      setData(null);
      setError(null);
      setLoading(false);
      return;
    }

    let cancelled = false;
    const load = (initial: boolean) => {
      if (initial) setLoading(true);
      api
        .oiTimeseries(symbol!, expiry!, strikeMin!, strikeMax!)
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
          if (!cancelled && initial) setLoading(false);
        });
    };

    load(true);
    const id = setInterval(() => load(false), POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [enabled, symbol, expiry, strikeMin, strikeMax]);

  return { data, error, loading };
}
