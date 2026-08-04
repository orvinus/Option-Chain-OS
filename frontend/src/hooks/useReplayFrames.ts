import { useEffect, useState } from "react";
import { api } from "../api/rest";
import type { ReplayFrame } from "../types";

interface Opts {
  symbol: string | null;
  expiry: string | null;
  startTs: string | null;
  endTs: string | null;
  step: string;
  enabled: boolean;
  withGreeks?: boolean;
}

/** Fetch the full replay frame list ONCE per (symbol, expiry, window, step). */
export function useReplayFrames({
  symbol,
  expiry,
  startTs,
  endTs,
  step,
  enabled,
  withGreeks = false,
}: Opts): { frames: ReplayFrame[]; loading: boolean; error: string | null } {
  const [frames, setFrames] = useState<ReplayFrame[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled || !symbol || !expiry || !startTs || !endTs) {
      setFrames([]);
      return;
    }
    // Before 09:15 on today's date `maxMinForDate` returns 0, so start === end and
    // /api/replay 400s with "start must be < end". Treat an empty window as "nothing
    // to replay yet" rather than showing the user a server error.
    if (Date.parse(endTs) <= Date.parse(startTs)) {
      setFrames([]);
      setError(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    api
      .replayFrames(symbol, expiry, startTs, endTs, step, false, withGreeks)
      .then((res) => {
        if (cancelled) return;
        setFrames(res.frames);
        setError(null);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [symbol, expiry, startTs, endTs, step, enabled, withGreeks]);

  return { frames, loading, error };
}
