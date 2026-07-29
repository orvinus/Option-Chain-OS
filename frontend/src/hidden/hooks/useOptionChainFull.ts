import { useEffect, useState } from "react";
import { api } from "../api/rest";
import type { OptionChainFullResponse, Timeframe } from "../types";

/**
 * One-shot option-chain-full snapshot from REST (bootstrap before WS frames arrive).
 * Refetches when timeframe, expiry, or symbol changes so the panel paints for every
 * timeframe even if the live WS stream is slow or has not yet pushed a matching frame.
 */
export function useOptionChainFull(
  timeframe: Timeframe | null,
  expiry: string | null,
  symbol: string | null = null,
) {
  const [data, setData] = useState<OptionChainFullResponse | null>(null);
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
      .optionChainFull(timeframe!, expiry ?? undefined, symbol ?? undefined)
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
  }, [timeframe, expiry, streamReady, symbol]);

  return { data, error, loading };
}
