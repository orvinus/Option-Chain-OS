import { useEffect, useRef, useState } from "react";
import { api } from "../api/rest";
import type { OIChangeResponse } from "../types";

/**
 * Fetches OI change over an explicit [fromTs, toTs] window via REST.
 *
 * - Pass `enabled = false` to disable (e.g. when not in custom-range mode).
 * - When `toTs` is `null` the window is open-ended ("up to latest"), so the hook
 *   polls every `LIVE_POLL_MS` to keep the live edge fresh. A fixed historical
 *   window (both bounds set) is fetched once per dependency change.
 */
const LIVE_POLL_MS = 3_000;

export function useOIChangeRange(
  fromTs: string | null,
  toTs: string | null,
  expiry: string | null,
  symbol: string | null,
  enabled: boolean,
) {
  const [data, setData] = useState<OIChangeResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const reqId = useRef(0);

  const active = enabled && fromTs !== null && expiry !== null;

  useEffect(() => {
    if (!active) {
      setData(null);
      setError(null);
      setLoading(false);
      return;
    }

    let cancelled = false;
    const myReq = ++reqId.current;
    setLoading(true);

    const fetchOnce = () => {
      api
        .oiChangeRange(fromTs!, toTs ?? undefined, expiry ?? undefined, symbol ?? undefined)
        .then((d) => {
          if (cancelled || myReq !== reqId.current) return;
          setData(d);
          setError(null);
        })
        .catch((e: unknown) => {
          if (cancelled || myReq !== reqId.current) return;
          setError(String(e));
        })
        .finally(() => {
          if (!cancelled && myReq === reqId.current) setLoading(false);
        });
    };

    fetchOnce();
    // Poll only when the upper bound tracks "now".
    const id = toTs === null ? setInterval(fetchOnce, LIVE_POLL_MS) : undefined;
    return () => {
      cancelled = true;
      if (id) clearInterval(id);
    };
  }, [active, fromTs, toTs, expiry, symbol]);

  return { data, error, loading };
}
