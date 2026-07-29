import { useEffect, useRef, useState } from "react";
import { api } from "../api/rest";
import type { RatioPoint } from "../types";

interface Opts {
  symbol: string | null;
  expiry: string | null;
  bucket: string;
  /** Explicit window. When `toTs` is omitted the fetch is treated as live and polled. */
  fromTs?: string | null;
  toTs?: string | null;
  /** ATM ± N strike bounds. Omit both for the full stored chain. */
  strikeMin?: number | null;
  strikeMax?: number | null;
  enabled: boolean;
  pollMs?: number;
}

/** Ratio/PCR-over-time for the Ratio chart. Live = poll; fixed window = one fetch. */
export function useRatioTimeseries({
  symbol,
  expiry,
  bucket,
  fromTs,
  toTs,
  strikeMin,
  strikeMax,
  enabled,
  pollMs = 20_000,
}: Opts): { points: RatioPoint[]; loading: boolean; error: string | null } {
  const [points, setPoints] = useState<RatioPoint[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const hasData = useRef(false);

  useEffect(() => {
    if (!enabled || !symbol || !expiry) {
      setPoints([]);
      hasData.current = false;
      return;
    }
    let cancelled = false;
    const live = !toTs;
    const fetchOnce = async () => {
      if (!hasData.current) setLoading(true);
      try {
        const res = await api.ratioTimeseries(
          symbol, expiry, bucket, fromTs ?? undefined, toTs ?? undefined,
          strikeMin ?? undefined, strikeMax ?? undefined,
        );
        if (cancelled) return;
        setPoints(res.points);
        setError(null);
        hasData.current = true;
      } catch (e) {
        if (!cancelled) setError(String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void fetchOnce();
    if (!live) return () => { cancelled = true; };
    const id = setInterval(() => void fetchOnce(), pollMs);
    return () => { cancelled = true; clearInterval(id); };
  }, [symbol, expiry, bucket, fromTs, toTs, strikeMin, strikeMax, enabled, pollMs]);

  return { points, loading, error };
}
