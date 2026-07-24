import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/rest";
import type { IvScannerResponse } from "../types";

const DEFAULT_POLL_MS = 45_000;

export function useIvScanner(
  symbols: string[],
  expiry: "near" | "next" | "far",
  mode: "latest" | "historical",
  enabled: boolean,
  pollMs = DEFAULT_POLL_MS,
) {
  const [data, setData] = useState<IvScannerResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<string | null>(null);
  const seq = useRef(0);

  const fetchOnce = useCallback(async () => {
    if (!enabled || symbols.length === 0) {
      setData(null);
      return;
    }
    const my = ++seq.current;
    setLoading(true);
    setError(null);
    try {
      const res = await api.ivScanner({ symbols, expiry, mode });
      if (my !== seq.current) return;
      setData(res);
      setUpdatedAt(new Date().toISOString());
    } catch (e) {
      if (my !== seq.current) return;
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (my === seq.current) setLoading(false);
    }
  }, [symbols, expiry, mode, enabled]);

  useEffect(() => {
    void fetchOnce();
    if (!enabled || symbols.length === 0) return;
    const id = window.setInterval(() => void fetchOnce(), pollMs);
    return () => window.clearInterval(id);
  }, [fetchOnce, enabled, symbols.length, pollMs]);

  return { data, error, loading, updatedAt, refresh: fetchOnce };
}
