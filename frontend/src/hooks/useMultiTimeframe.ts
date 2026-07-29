import { useEffect, useRef, useState } from "react";
import { api } from "../api/rest";
import type { MultiTimeframeResponse } from "../types";

interface Opts {
  symbol: string | null;
  expiry: string | null;
  enabled: boolean;
  /** Historical instant (replay clock or picked date). Omitted => live (polled). */
  asOf?: string | null;
  /** Restrict every sum to strikes within ATM ± N. Omit / <0 => full chain. */
  atmWindow?: number | null;
  pollMs?: number;
}

/** All-timeframes-at-once grid. Single `/api/multi-timeframe` call; polls when live. */
export function useMultiTimeframe({
  symbol,
  expiry,
  enabled,
  asOf,
  atmWindow,
  pollMs = 15_000,
}: Opts): { data: MultiTimeframeResponse | null; loading: boolean; error: string | null } {
  const [data, setData] = useState<MultiTimeframeResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const hasData = useRef(false);

  useEffect(() => {
    if (!enabled || !symbol || !expiry) {
      setData(null);
      hasData.current = false;
      return;
    }
    let cancelled = false;
    const live = !asOf;
    const run = async () => {
      if (!hasData.current) setLoading(true);
      try {
        const res = await api.multiTimeframe(
          symbol, expiry, asOf ?? undefined,
          atmWindow != null && atmWindow >= 0 ? atmWindow : undefined,
        );
        if (cancelled) return;
        setData(res);
        setError(null);
        hasData.current = true;
      } catch (e) {
        if (!cancelled) setError(String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void run();
    if (!live) return () => { cancelled = true; };
    const id = setInterval(() => void run(), pollMs);
    return () => { cancelled = true; clearInterval(id); };
  }, [symbol, expiry, enabled, asOf, atmWindow, pollMs]);

  return { data, loading, error };
}
