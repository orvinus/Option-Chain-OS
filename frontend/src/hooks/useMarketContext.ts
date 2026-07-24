import { useCallback, useEffect, useMemo, useState } from "react";
import type React from "react";
import { api } from "../api/rest";
import type { HealthResponse, SymbolEntry, SymbolSectorGroup } from "../types";

const HEALTH_POLL_MS = 3_000;
const EXPIRY_POLL_MS = 30_000;

function flattenSymbols(groups: SymbolSectorGroup[]): Record<string, SymbolEntry> {
  const out: Record<string, SymbolEntry> = {};
  for (const g of groups) {
    for (const s of g.symbols) {
      out[s.symbol] = s;
    }
  }
  return out;
}

/** Shared market/session state used by every page (OI Change, Charts).
 *
 * Owns the auth/health poll, symbol registry, expiry list, the active symbol,
 * and the ATM ± N strike window. Lifted to `App` and passed to each page so the
 * selection (and a single health poll) is shared across tab switches.
 */
export interface MarketContextValue {
  authenticated: boolean;
  authChecked: boolean;
  health: HealthResponse | null;
  setAuthenticated: React.Dispatch<React.SetStateAction<boolean>>;
  handleAuthenticated: () => void;
  /** Last error from the automatic broker connect (null while connecting/connected). */
  connectError: string | null;

  symbol: string;
  symbolGroups: SymbolSectorGroup[];
  switching: boolean;
  symbolError: string | null;
  handleSymbolChange: (next: string) => Promise<void>;

  expiry: string | null;
  setExpiry: React.Dispatch<React.SetStateAction<string | null>>;
  expiries: string[];
  expiryError: string | null;

  atmWindow: number;
  setAtmWindow: React.Dispatch<React.SetStateAction<number>>;

  activeEntry: SymbolEntry | undefined;
  fnoEligible: boolean;
  symbolDisplay: string;
  liveSpot: number | null;
}

export function useMarketContext(): MarketContextValue {
  const [expiry, setExpiry] = useState<string | null>(null);
  const [expiries, setExpiries] = useState<string[]>([]);
  const [expiryError, setExpiryError] = useState<string | null>(null);

  const [symbol, setSymbol] = useState<string>("NIFTY");
  const [symbolGroups, setSymbolGroups] = useState<SymbolSectorGroup[]>([]);
  const [switching, setSwitching] = useState(false);
  const [symbolError, setSymbolError] = useState<string | null>(null);

  const [atmWindow, setAtmWindow] = useState<number>(5);

  const [authenticated, setAuthenticated] = useState(false);
  const [authChecked, setAuthChecked] = useState(false);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [connectError, setConnectError] = useState<string | null>(null);

  const symbolIndex = useMemo(() => flattenSymbols(symbolGroups), [symbolGroups]);
  const activeEntry: SymbolEntry | undefined = symbolIndex[symbol];
  const fnoEligible = activeEntry?.fno_eligible ?? true;
  const symbolDisplay = activeEntry?.display ?? symbol;

  // Poll /api/health for auth, market session, and ingestion.
  useEffect(() => {
    let cancelled = false;
    const check = async () => {
      try {
        const h = await api.health();
        if (!cancelled) {
          setHealth(h);
          setAuthenticated(h.authenticated);
          setAuthChecked(true);
          // Sync local symbol with backend if it changed out-of-band (e.g. server restart).
          if (h.active_symbol && h.active_symbol !== symbol) {
            setSymbol(h.active_symbol);
          }
        }
      } catch {
        if (!cancelled) setAuthChecked(true);
      }
    };
    void check();
    const id = setInterval(() => void check(), HEALTH_POLL_MS);
    return () => { cancelled = true; clearInterval(id); };
    // symbol intentionally excluded from deps — we only want to sync once on initial mount/poll.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Auto-connect the broker using the appKey/secretKey in .env. The XTS market-data
  // API authenticates with the API key alone (no MPIN/TOTP/per-user login), so there
  // is no manual "Connect to Broker" page — establish the session automatically and
  // retry until it succeeds.
  useEffect(() => {
    if (!authChecked || authenticated) return;
    // Replay instances never contact the broker (single-session-per-appKey safety):
    // don't auto-connect — the backend would 409 anyway, and hammering it every 60s
    // just shows a perpetual "connect error" on this dev/replay dashboard.
    if (health?.run_mode === "replay") return;
    let cancelled = false;
    let inFlight = false;
    const connect = async () => {
      if (inFlight || cancelled) return;
      inFlight = true;
      try {
        await api.login({ mpin: "" });
        if (!cancelled) setConnectError(null);
      } catch (e) {
        if (!cancelled) setConnectError(String(e));
      } finally {
        inFlight = false;
      }
    };
    void connect();
    // Gentle retry while disconnected — XTS market-data login is rate-limited and
    // every login restarts the feed, so don't hammer it. Stops once authenticated.
    const id = setInterval(() => void connect(), 60_000);
    return () => { cancelled = true; clearInterval(id); };
  }, [authChecked, authenticated, health?.run_mode]);

  // Load the symbol registry once authenticated; retry every 3s on transient failure
  // or when groups is empty (e.g. backend came up after frontend).
  useEffect(() => {
    if (!authenticated) return;
    let cancelled = false;
    const load = () => {
      api.symbols().then((res) => {
        if (cancelled) return;
        setSymbolGroups(res.groups);
        setSymbolError(null);
        if (res.active_symbol) setSymbol(res.active_symbol);
      }).catch((e: unknown) => {
        if (!cancelled) setSymbolError(String(e));
      });
    };
    load();
    const id = setInterval(() => {
      // Re-fetch until we have at least one group; thereafter stop polling.
      setSymbolGroups((curr) => {
        if (curr.length === 0) load();
        return curr;
      });
    }, 3_000);
    return () => { cancelled = true; clearInterval(id); };
  }, [authenticated]);

  // Load expiries for the current symbol when it changes (or auth flips on).
  useEffect(() => {
    if (!authenticated) return;
    if (!fnoEligible) {
      setExpiries([]);
      setExpiry(null);
      setExpiryError(null);
      return;
    }
    let cancelled = false;
    const tick = async () => {
      try {
        const res = await api.expiries(symbol);
        if (cancelled) return;
        setExpiries(res.expiries);
        setExpiryError(null);
        setExpiry((curr) => {
          if (curr && res.expiries.includes(curr)) return curr;
          return res.expiries[0] ?? null;
        });
      } catch (e) { if (!cancelled) setExpiryError(String(e)); }
    };
    void tick();
    const id = setInterval(() => void tick(), EXPIRY_POLL_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, [authenticated, symbol, fnoEligible]);

  const handleSymbolChange = useCallback(async (next: string) => {
    if (next === symbol) return;
    setSwitching(true);
    setSymbolError(null);
    // Optimistic UI: clear the chart so the user knows a switch is in flight.
    setExpiry(null);
    setExpiries([]);
    try {
      const res = await api.setActiveSymbol(next);
      setSymbol(res.symbol);
      setExpiries(res.expiries);
      setExpiry(res.expiries[0] ?? null);
    } catch (e) {
      setSymbolError(String(e));
    } finally {
      setSwitching(false);
    }
  }, [symbol]);

  const handleAuthenticated = useCallback(() => {
    setAuthenticated(true);
  }, []);

  const liveSpot =
    health?.feed_connected && health.latest_spot != null && health.active_symbol === symbol
      ? health.latest_spot
      : null;

  return {
    authenticated, authChecked, health, setAuthenticated, handleAuthenticated, connectError,
    symbol, symbolGroups, switching, symbolError, handleSymbolChange,
    expiry, setExpiry, expiries, expiryError,
    atmWindow, setAtmWindow,
    activeEntry, fnoEligible, symbolDisplay, liveSpot,
  };
}
