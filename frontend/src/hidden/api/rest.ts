import type {
  ActiveSymbolResponse,
  ExpiriesResponse,
  HealthResponse,
  IvScannerResponse,
  LoginRequest,
  LoginResponse,
  NiftyCrossCheckResponse,
  OIChangeResponse,
  OptionChainFullResponse,
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
  optionChainFull: (timeframe: Timeframe, expiry?: string, symbol?: string) =>
    getJSON<OptionChainFullResponse>("/api/option-chain-full", { timeframe, expiry, symbol }),
  /**
   * Multi-symbol IV / OI scanner. `symbols` is comma-separated (max 50).
   * `expiry` = near|next|far, `mode` = latest|historical.
   */
  ivScanner: (opts: {
    symbols: string[];
    expiry?: "near" | "next" | "far";
    mode?: "latest" | "historical";
    timeframe?: string;
  }) =>
    getJSON<IvScannerResponse>("/api/iv-scanner", {
      symbols: opts.symbols.join(","),
      expiry: opts.expiry ?? "near",
      mode: opts.mode ?? "latest",
      timeframe: opts.timeframe ?? "full_day",
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
   * Fixed username+password gate for the /hidden dashboard. On success the backend
   * also establishes the broker session, so allow the same ~55s cap as `login`.
   */
  hiddenLogin: (req: { username: string; password: string }) =>
    postJSON<LoginResponse>("/api/auth/hidden-login", req, { timeoutMs: 55_000 }),
};
