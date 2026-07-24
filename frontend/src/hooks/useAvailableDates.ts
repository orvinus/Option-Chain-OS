import { useEffect, useState } from "react";
import { api } from "../api/rest";

/** IST trading days with stored data for the symbol+expiry (date-picker source). */
export function useAvailableDates(
  symbol: string | null,
  expiry: string | null,
  enabled: boolean,
): { dates: string[]; latest: string | null; loading: boolean; error: string | null } {
  const [dates, setDates] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled || !symbol || !expiry) {
      setDates([]);
      return;
    }
    let cancelled = false;
    setLoading(true);
    api
      .historyDates(symbol, expiry)
      .then((res) => {
        if (cancelled) return;
        setDates(res.dates);
        setError(null);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [symbol, expiry, enabled]);

  return { dates, latest: dates[0] ?? null, loading, error };
}
