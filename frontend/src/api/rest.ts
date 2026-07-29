import type {
  ActiveSymbolResponse,
  ExpiriesResponse,
  HealthResponse,
  HistoryDatesResponse,
  LoginRequest,
  LoginResponse,
  MultiTimeframeResponse,
  NiftyCrossCheckResponse,
  OIChangeResponse,
  OITimeseriesResponse,
  OptionChainFullResponse,
  RatioTimeseriesResponse,
  ReplayFramesResponse,
  SpotResponse,
  SymbolsResponse,
  Timeframe,
} from "../types";

const API_BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";

/** Prefer FastAPI `detail` field so login/API errors are readable in the UI. */
function formatErrorBody(res: Response, raw: string): string {
  if (!raw.trim()) return res.statusText || "Unknown error";
  try {
    const j = JSON.parse(raw) as { detail?: unknown };
    if (typeof j.detail === "string") return j.detail;
    if (Array.isArray(j.detail)) {
      return j.detail
        .map((item: unknown) =>
          typeof item === "object" && item !== null && "msg" in item
            ? String((item as { msg: string }).msg)
            : JSON.stringify(item)
        )
        .join("; ");
    }
  } catch {
    /* not JSON — use raw */
  }
  return raw;
}

async function getJSON<T>(path: string, params?: Record<string, string | undefined>): Promise<T> {
  const url = new URL(API_BASE ? `${API_BASE}${path}` : path, window.location.origin);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v != null) url.searchParams.set(k, v);
    }
  }
  const res = await fetch(url.toString(), { credentials: "same-origin" });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${res.statusText} :: ${formatErrorBody(res, text)}`);
  }
  return (await res.json()) as T;
}

type PostOpts = { timeoutMs?: number };

async function postJSON<T>(path: string, body: unknown, opts?: PostOpts): Promise<T> {
  const url = API_BASE ? `${API_BASE}${path}` : path;
  const controller = opts?.timeoutMs != null ? new AbortController() : undefined;
  const timer =
    controller != null && opts?.timeoutMs != null
      ? setTimeout(() => controller.abort(), opts.timeoutMs)
      : undefined;
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify(body),
      signal: controller?.signal,
    });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(`${res.status} ${res.statusText} :: ${formatErrorBody(res, text)}`);
    }
    return (await res.json()) as T;
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") {
      throw new Error(
        `Request timed out after ${opts?.timeoutMs ?? 0} ms — Angel One or the database may be slow or unreachable.`
      );
    }
    throw e;
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

export const api = {
  health: () => getJSON<HealthResponse>("/api/health"),
  spot: (symbol?: string) => getJSON<SpotResponse>("/api/spot", { symbol }),
  niftyCrossCheck: () => getJSON<NiftyCrossCheckResponse>("/api/verify/nifty-cross-check"),
  expiries: (symbol?: string) => getJSON<ExpiriesResponse>("/api/expiries", { symbol }),
  oiChange: (timeframe: Timeframe, expiry?: string, symbol?: string) =>
    getJSON<OIChangeResponse>("/api/oi-change", { timeframe, expiry, symbol }),
  /**
   * OI change over an explicit window. `toTs` omitted => "up to latest" (live,
   * left-anchored window). Timestamps are ISO-8601 (IST offset recommended).
   */
  oiChangeRange: (fromTs: string, toTs: string | undefined, expiry?: string, symbol?: string) =>
    getJSON<OIChangeResponse>("/api/oi-change", { from_ts: fromTs, to_ts: toTs, expiry, symbol }),
  /**
   * Total Call/Put OI per time bucket across a strike range, for the Charts page.
   * `strikeMin`/`strikeMax` bound the ATM ± N window; `bucket` defaults to "1m"
   * (higher intervals are aggregated into candles on the client).
   */
  oiTimeseries: (
    symbol: string,
    expiry: string,
    strikeMin: number,
    strikeMax: number,
    bucket = "1m",
    fromTs?: string,
    toTs?: string,
  ) =>
    getJSON<OITimeseriesResponse>("/api/oi-timeseries", {
      symbol,
      expiry,
      strike_min: String(strikeMin),
      strike_max: String(strikeMax),
      bucket,
      from_ts: fromTs,
      to_ts: toTs,
    }),
  optionChainFull: (timeframe: Timeframe, expiry?: string, symbol?: string) =>
    getJSON<OptionChainFullResponse>("/api/option-chain-full", { timeframe, expiry, symbol }),
  /** IST trading days that have stored data — powers the historical date picker. */
  historyDates: (symbol?: string, expiry?: string) =>
    getJSON<HistoryDatesResponse>("/api/history-dates", { symbol, expiry }),
  /**
   * Call/Put ratio + PCR per bucket over the (optional) window, for the Ratio chart.
   * `strikeMin`/`strikeMax` bound the ATM ± N window (omit for the full stored chain).
   */
  ratioTimeseries: (
    symbol: string,
    expiry: string,
    bucket = "1m",
    fromTs?: string,
    toTs?: string,
    strikeMin?: number,
    strikeMax?: number,
  ) =>
    getJSON<RatioTimeseriesResponse>("/api/ratio-timeseries", {
      symbol,
      expiry,
      bucket,
      from_ts: fromTs,
      to_ts: toTs,
      strike_min: strikeMin != null ? String(strikeMin) : undefined,
      strike_max: strikeMax != null ? String(strikeMax) : undefined,
    }),
  /**
   * One OI-change row per timeframe + shared level ratio/pcr/spot/atm. `asOf` =>
   * historical instant; `atmWindow` (>=0) restricts every sum to strikes within ATM ± N.
   */
  multiTimeframe: (symbol?: string, expiry?: string, asOf?: string, atmWindow?: number) =>
    getJSON<MultiTimeframeResponse>("/api/multi-timeframe", {
      symbol,
      expiry,
      as_of: asOf,
      atm_window: atmWindow != null ? String(atmWindow) : undefined,
    }),
  /**
   * Enriched historical replay frames from `start` to `end` at `step` resolution.
   * `summary` drops per-strike rows for a lean scrubber payload.
   */
  replayFrames: (
    symbol: string,
    expiry: string,
    start: string,
    end: string,
    step = "1m",
    summary = false,
    withGreeks = false,
  ) =>
    getJSON<ReplayFramesResponse>("/api/replay", {
      symbol,
      expiry,
      start,
      end,
      step,
      summary: summary ? "true" : undefined,
      with_greeks: withGreeks ? "true" : undefined,
    }),
  symbols: () => getJSON<SymbolsResponse>("/api/symbols"),
  /** Switch the live WebSocket subscription to a new symbol. Allow ~45s — Angel resubscribe can be slow. */
  setActiveSymbol: (symbol: string) =>
    postJSON<ActiveSymbolResponse>("/api/active-symbol", { symbol }, { timeoutMs: 45_000 }),
  authStart: () =>
    getJSON<{ login_url: string; state: string }>("/api/auth/publisher/start"),
  /** ~55s client cap vs backend SMARTAPI_LOGIN_TIMEOUT_S (45s default) + persist margin */
  login: (req: LoginRequest) =>
    postJSON<LoginResponse>("/api/auth/login", req, { timeoutMs: 55_000 }),
  /**
   * Fixed-credential gate for the main dashboard. Verified server-side against
   * MAIN_USER / MAIN_PASSWORD (distinct from the /hidden pair). In live mode a
   * successful sign-in also starts the broker feed, so allow ~55s.
   */
  gateLogin: (req: { username: string; password: string }) =>
    postJSON<LoginResponse>("/api/auth/main-login", req, { timeoutMs: 55_000 }),
};
