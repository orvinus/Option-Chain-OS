/**
 * REST client for the Algo Config backend (`/api/algo/*`).
 *
 * Separate from `rest.ts` on purpose: these endpoints are session-cookied
 * (httpOnly `algo_session`), and callers need the HTTP status to react
 * correctly — 401 re-shows the admin gate, 422 carries a structured
 * `{errors: [...]}` list from §15 validation, 503 means the backend has no
 * ALGO_ADMIN_USER configured yet.
 */
import type {
  AlgoConfigDoc,
  AlgoIdentity,
  AuditRow,
  ConfigEnvelope,
  ConfigVersionMeta,
  ExportDataset,
  SaveResponse,
  SignalRow,
  TelegramStatus,
  ValidationReport,
} from "../types/algo";

import { trackRequest } from "./inflight";

const API_BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";

export class AlgoApiError extends Error {
  status: number;
  /** §15 validation errors (422) — one human-readable line each. */
  errors: string[];

  constructor(status: number, message: string, errors: string[] = []) {
    super(message);
    this.status = status;
    this.errors = errors;
  }
}

function parseErrorBody(status: number, statusText: string, raw: string): AlgoApiError {
  if (raw.trim()) {
    try {
      const j = JSON.parse(raw) as { detail?: unknown };
      const d = j.detail;
      if (typeof d === "string") return new AlgoApiError(status, d);
      if (d && typeof d === "object" && "errors" in d) {
        const errs = (d as { errors: unknown }).errors;
        if (Array.isArray(errs)) {
          const list = errs.map((e) => String(e));
          return new AlgoApiError(status, list.join("; "), list);
        }
      }
    } catch {
      /* not JSON — fall through */
    }
  }
  return new AlgoApiError(status, statusText || `HTTP ${status}`);
}

async function request<T>(
  method: "GET" | "POST" | "PUT",
  path: string,
  opts?: { body?: unknown; params?: Record<string, string | undefined> }
): Promise<T> {
  // Counted so the app-wide loading indicator covers every algo call.
  return trackRequest(() => requestInner<T>(method, path, opts));
}

async function requestInner<T>(
  method: "GET" | "POST" | "PUT",
  path: string,
  opts?: { body?: unknown; params?: Record<string, string | undefined> }
): Promise<T> {
  const url = new URL(API_BASE ? `${API_BASE}${path}` : path, window.location.origin);
  if (opts?.params) {
    for (const [k, v] of Object.entries(opts.params)) {
      if (v != null && v !== "") url.searchParams.set(k, v);
    }
  }
  const res = await fetch(url.toString(), {
    method,
    credentials: "same-origin",
    headers: opts?.body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: opts?.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    if (res.status === 401 && !path.endsWith("/auth/login") && !path.endsWith("/auth/me")) {
      // The session died (server restart / expiry) while the UI still holds
      // an identity — tell the gate to drop back to the sign-in form instead
      // of every panel erroring in place.
      window.dispatchEvent(new CustomEvent("algo:unauthorized"));
    }
    throw parseErrorBody(res.status, res.statusText, await res.text());
  }
  return (await res.json()) as T;
}

function buildUrl(path: string, params?: Record<string, string | undefined>): URL {
  const url = new URL(API_BASE ? `${API_BASE}${path}` : path, window.location.origin);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v != null && v !== "") url.searchParams.set(k, v);
    }
  }
  return url;
}

/** Where an export dataset lives: a ledger (paper/live) or a backtest run. */
export type ExportScope =
  | { kind: "ledger"; ledger: "paper" | "live" }
  | { kind: "backtest"; runId: number };

export interface ExportQuery {
  scope: ExportScope;
  dataset: ExportDataset;
  from_date?: string;
  to_date?: string;
  format: "csv" | "ndjson";
  /** decisions / signals only. */
  zone?: string;
  /** signals only. */
  indicator?: string;
}

/** Build the `/api/algo/export/*` URL for a dataset (§6). Ledger is only sent
 *  for the datasets that are ledger-scoped; backtest datasets are run-scoped. */
export function exportUrl(q: ExportQuery): string {
  const params: Record<string, string | undefined> = {
    from_date: q.from_date,
    to_date: q.to_date,
    format: q.format,
    zone: q.dataset === "decisions" || q.dataset === "signals" ? q.zone : undefined,
    indicator: q.dataset === "signals" ? q.indicator : undefined,
  };
  if (q.scope.kind === "backtest") {
    return buildUrl(`/api/algo/export/backtest/${q.scope.runId}/${q.dataset}`, params).toString();
  }
  if (q.dataset === "trades" || q.dataset === "calendar" || q.dataset === "summary") {
    params.ledger = q.scope.ledger;
  }
  return buildUrl(`/api/algo/export/${q.dataset}`, params).toString();
}

function filenameFromDisposition(res: Response, fallback: string): string {
  const cd = res.headers.get("content-disposition") ?? "";
  const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(cd);
  return m ? decodeURIComponent(m[1]) : fallback;
}

/** Fetch a CSV export with the session cookie and hand it to the browser as a
 *  download. 4xx/5xx bodies surface through the same error parser as JSON
 *  calls (400 range errors, 404 unknown run, 401 → gate). */
export async function downloadExportCsv(q: ExportQuery, fallbackName?: string): Promise<string> {
  const res = await fetch(exportUrl({ ...q, format: "csv" }), { credentials: "same-origin" });
  if (!res.ok) {
    if (res.status === 401) window.dispatchEvent(new CustomEvent("algo:unauthorized"));
    throw parseErrorBody(res.status, res.statusText, await res.text());
  }
  const blob = await res.blob();
  const name = filenameFromDisposition(res, fallbackName ?? `algo-${q.dataset}.csv`);
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
  return name;
}

export interface ExportRows {
  headers: string[];
  rows: (string | number | null)[][];
  /** Filename the server suggested (Content-Disposition), extension stripped. */
  filenameBase: string;
}

/** Stream an NDJSON export into `{headers, rows}` for the client-side XLSX
 *  writer. The first line is `{"__headers__": [...]}`, then one object per row;
 *  cells are projected in header order so the sheet has a stable column set. */
export async function fetchExportRows(
  q: ExportQuery,
  onProgress?: (rows: number) => void
): Promise<ExportRows> {
  const res = await fetch(exportUrl({ ...q, format: "ndjson" }), { credentials: "same-origin" });
  if (!res.ok) {
    if (res.status === 401) window.dispatchEvent(new CustomEvent("algo:unauthorized"));
    throw parseErrorBody(res.status, res.statusText, await res.text());
  }
  const suggested = filenameFromDisposition(res, `algo-${q.dataset}.ndjson`);
  const filenameBase = suggested.replace(/\.(ndjson|csv|json)$/i, "");
  let headers: string[] = [];
  const rows: (string | number | null)[][] = [];

  const toCell = (v: unknown): string | number | null => {
    if (v == null) return null;
    if (typeof v === "number") return Number.isFinite(v) ? v : null;
    if (typeof v === "boolean") return v ? "true" : "false";
    if (typeof v === "string") return v;
    return JSON.stringify(v);
  };
  const consume = (line: string) => {
    const s = line.trim();
    if (!s) return;
    const obj = JSON.parse(s) as Record<string, unknown>;
    if (Array.isArray(obj.__headers__)) {
      headers = (obj.__headers__ as unknown[]).map((h) => String(h));
      return;
    }
    if (headers.length === 0) headers = Object.keys(obj);
    rows.push(headers.map((h) => toCell(obj[h])));
  };

  if (res.body) {
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    let lastReport = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let nl = buf.indexOf("\n");
      while (nl >= 0) {
        consume(buf.slice(0, nl));
        buf = buf.slice(nl + 1);
        nl = buf.indexOf("\n");
      }
      if (onProgress && rows.length - lastReport >= 1000) {
        lastReport = rows.length;
        onProgress(rows.length);
      }
    }
    buf += decoder.decode();
    if (buf.trim()) consume(buf);
  } else {
    for (const line of (await res.text()).split("\n")) consume(line);
  }
  onProgress?.(rows.length);
  return { headers, rows, filenameBase };
}

export const algoApi = {
  // ── auth ──
  login: (username: string, password: string) =>
    request<AlgoIdentity>("POST", "/api/algo/auth/login", { body: { username, password } }),
  logout: () => request<{ status: string }>("POST", "/api/algo/auth/logout", { body: {} }),
  /** Change the Algo Config / Backtesting sign-in ID and password. The session
   *  is cleared server-side on success, so the caller must sign in again. */
  changeCredentials: (body: {
    current_username: string;
    current_password: string;
    new_username: string;
    new_password: string;
  }) => request<AlgoIdentity>("POST", "/api/algo/auth/change-credentials", { body }),
  me: () => request<AlgoIdentity>("GET", "/api/algo/auth/me"),

  // ── config document ──
  getConfig: () => request<ConfigEnvelope>("GET", "/api/algo/config"),
  saveConfig: (config: AlgoConfigDoc, note: string, baseVersion?: number | null) =>
    request<SaveResponse>("POST", "/api/algo/config", {
      // base_version = optimistic concurrency: the server refuses (409) when
      // the live version moved on since this draft was loaded.
      body: { config, note, base_version: baseVersion ?? undefined },
    }),
  versions: (limit = 25) =>
    request<ConfigVersionMeta[]>("GET", "/api/algo/config/versions", {
      params: { limit: String(limit) },
    }),
  version: (v: number) => request<ConfigEnvelope>("GET", `/api/algo/config/versions/${v}`),
  restore: (v: number) =>
    request<SaveResponse>("POST", "/api/algo/config/restore", { body: { version: v } }),
  validation: () => request<ValidationReport>("GET", "/api/algo/config/validation"),

  // ── engine evaluations ──
  // ``at`` (HH:MM IST) freezes the evaluation at that minute; ``configVersion``
  // pins the config document (how the Backtesting tab replays its dashboards
  // under a run's exact settings).
  oiStructureEval: (opts: {
    day: string; zone: string; date?: string; symbol?: string; expiry?: string;
    at?: string; configVersion?: number; configRun?: number; configSandbox?: boolean;
  }) =>
    request<import("../types/algo").OiStructureEvalResponse>(
      "GET",
      "/api/algo/engines/oi-structure",
      {
        params: {
          day: opts.day, zone: opts.zone, date: opts.date,
          symbol: opts.symbol, expiry: opts.expiry, at: opts.at,
          config_version:
            opts.configVersion != null ? String(opts.configVersion) : undefined,
          config_run: opts.configRun != null ? String(opts.configRun) : undefined,
          config_sandbox: opts.configSandbox ? "true" : undefined,
        },
      }
    ),
  mtfRatioEval: (opts: {
    day: string; zone: string; date?: string; symbol?: string; expiry?: string;
    at?: string; configVersion?: number; configRun?: number; configSandbox?: boolean;
  }) =>
    request<import("../types/algo").MtfRatioEvalResponse>(
      "GET",
      "/api/algo/engines/mtf-ratio",
      {
        params: {
          day: opts.day, zone: opts.zone, date: opts.date,
          symbol: opts.symbol, expiry: opts.expiry, at: opts.at,
          config_version:
            opts.configVersion != null ? String(opts.configVersion) : undefined,
          config_run: opts.configRun != null ? String(opts.configRun) : undefined,
          config_sandbox: opts.configSandbox ? "true" : undefined,
        },
      }
    ),
  mqaeEval: (opts: {
    day: string; zone: string; date?: string; symbol?: string; expiry?: string;
    at?: string; configVersion?: number; configRun?: number; configSandbox?: boolean;
    timeframe?: string;
    /** JSON of the panel's unsaved MqaeParams (preview before Save). */
    params?: string;
  }) =>
    request<import("../types/algo").MqaeEvalResponse>(
      "GET",
      "/api/algo/engines/mqae",
      {
        params: {
          day: opts.day, zone: opts.zone, date: opts.date,
          symbol: opts.symbol, expiry: opts.expiry, at: opts.at,
          config_version:
            opts.configVersion != null ? String(opts.configVersion) : undefined,
          config_run: opts.configRun != null ? String(opts.configRun) : undefined,
          config_sandbox: opts.configSandbox ? "true" : undefined,
          timeframe: opts.timeframe,
          params: opts.params,
        },
      }
    ),
  umpEval: (opts: {
    day: string;
    zone: string;
    date?: string;
    symbol?: string;
    expiry?: string;
    strike?: number;
    optionType?: "CE" | "PE";
    at?: string;
    configVersion?: number;
    configRun?: number;
    configSandbox?: boolean;
    /** Display candle interval (§3); the engine always evaluates its entry TF. */
    interval?: import("../types/algo").UmpDisplayInterval;
    /** Ask for the level/state histories the chart replay reconstructs from. */
    history?: boolean;
  }) =>
    request<import("../types/algo").UmpEvalResponse>("GET", "/api/algo/engines/ump", {
      params: {
        day: opts.day,
        zone: opts.zone,
        date: opts.date,
        symbol: opts.symbol,
        expiry: opts.expiry,
        strike: opts.strike != null ? String(opts.strike) : undefined,
        option_type: opts.optionType,
        at: opts.at,
        config_version:
          opts.configVersion != null ? String(opts.configVersion) : undefined,
        config_run: opts.configRun != null ? String(opts.configRun) : undefined,
        config_sandbox: opts.configSandbox ? "true" : undefined,
        interval: opts.interval && opts.interval !== "entry" ? opts.interval : undefined,
        history: opts.history ? "true" : undefined,
      },
    }),

  // ── backtest sandbox (the standalone workspace's editable document) ──
  sandboxGet: () =>
    request<import("../types/algo").SandboxEnvelope>(
      "GET", "/api/algo/backtest/sandbox"
    ),
  sandboxPut: (config: AlgoConfigDoc) =>
    request<{ status: string; changed_fields: number; warnings: string[] }>(
      "PUT", "/api/algo/backtest/sandbox", { body: { config } }
    ),
  sandboxCopy: (source: "live" | "version", version?: number) =>
    request<{ status: string; from_version: number | null }>(
      "POST", "/api/algo/backtest/sandbox/copy", { body: { source, version } }
    ),
  validateDocument: (document: AlgoConfigDoc) =>
    request<ValidationReport>("POST", "/api/algo/config/validation", {
      body: { document },
    }),

  // ── backtesting ──
  backtestCoverage: () =>
    request<import("../types/algo").BacktestCoverage>(
      "GET", "/api/algo/backtest/coverage"
    ),
  backtestPreflight: (body: import("../types/algo").BacktestRunRequest) =>
    request<import("../types/algo").BacktestPreflight>(
      "POST", "/api/algo/backtest/preflight", { body }
    ),
  backtestCreate: (body: import("../types/algo").BacktestRunRequest) =>
    request<{ id: number; status: string; config_version: number | null; warnings: string[] }>(
      "POST", "/api/algo/backtest/runs", { body }
    ),
  backtestRuns: () =>
    request<import("../types/algo").BacktestRunListItem[]>(
      "GET", "/api/algo/backtest/runs"
    ),
  backtestRun: (id: number) =>
    request<import("../types/algo").BacktestRun>("GET", `/api/algo/backtest/runs/${id}`),
  backtestCancel: (id: number) =>
    request<{ status: string }>("POST", `/api/algo/backtest/runs/${id}/cancel`, { body: {} }),
  backtestResume: (id: number) =>
    request<{ id: number; status: string }>(
      "POST", `/api/algo/backtest/runs/${id}/resume`, { body: {} }
    ),
  backtestDelete: (id: number) =>
    request<{ status: string }>("POST", `/api/algo/backtest/runs/${id}/delete`, { body: {} }),
  backtestDays: (id: number) =>
    request<import("../types/algo").BacktestDayRow[]>(
      "GET", `/api/algo/backtest/runs/${id}/days`
    ),
  backtestTrades: (id: number, opts?: { date?: string; zone?: string; limit?: number }) =>
    request<import("../types/algo").TradeRow[]>(
      "GET", `/api/algo/backtest/runs/${id}/trades`,
      {
        params: {
          date: opts?.date, zone: opts?.zone,
          limit: opts?.limit != null ? String(opts.limit) : undefined,
        },
      }
    ),
  backtestPnlSummary: (id: number) =>
    request<import("../types/algo").PnlSummary>(
      "GET", `/api/algo/backtest/runs/${id}/pnl/summary`
    ),
  backtestPnlCalendar: (id: number, month: string) =>
    request<import("../types/algo").PnlCalendar>(
      "GET", `/api/algo/backtest/runs/${id}/pnl/calendar`, { params: { month } }
    ),
  backtestEquity: (id: number) =>
    request<import("../types/algo").BacktestEquity>(
      "GET", `/api/algo/backtest/runs/${id}/equity`
    ),
  backtestDayBundle: (id: number, date: string) =>
    request<import("../types/algo").BacktestDayBundle>(
      "GET", `/api/algo/backtest/runs/${id}/day/${date}`
    ),

  // ── broker live account (A-to-Z read-only state) ──
  brokerAccount: () =>
    request<import("../types/algo").BrokerAccount>("GET", "/api/algo/broker/account"),

  // ── Appendix-A reference defaults (source for every Reset-to-Default) ──
  configDefaults: () =>
    request<AlgoConfigDoc>("GET", "/api/algo/config/defaults"),

  // ── per-day journal notes ──
  dayNotes: (month: string) =>
    request<Record<string, string>>("GET", "/api/algo/notes", { params: { month } }),
  putDayNote: (date: string, note: string) =>
    request<{ status: string }>("PUT", `/api/algo/notes/${date}`, { body: { note } }),

  // ── trades / P&L / status ──
  trades: (opts?: {
    date?: string;
    day?: string;
    zone?: string;
    ledger?: string;
    from_date?: string;
    to_date?: string;
    limit?: number;
  }) =>
    request<import("../types/algo").TradeRow[]>("GET", "/api/algo/trades", {
      params: {
        date: opts?.date,
        day: opts?.day,
        zone: opts?.zone,
        ledger: opts?.ledger,
        from_date: opts?.from_date,
        to_date: opts?.to_date,
        limit: opts?.limit != null ? String(opts.limit) : undefined,
      },
    }),
  pnlSummary: (opts?: { ledger?: string; from_date?: string; to_date?: string }) =>
    request<import("../types/algo").PnlSummary>("GET", "/api/algo/pnl/summary", {
      params: { ledger: opts?.ledger, from_date: opts?.from_date, to_date: opts?.to_date },
    }),
  pnlCalendar: (month: string, ledger: string) =>
    request<import("../types/algo").PnlCalendar>("GET", "/api/algo/pnl/calendar", {
      params: { month, ledger },
    }),
  paperSession: () =>
    request<import("../types/algo").PaperSession>("GET", "/api/algo/paper/session"),
  paperReset: () =>
    request<{ status: string; deleted_rows: number }>("POST", "/api/algo/paper/reset", {
      body: {},
    }),
  orchestratorStatus: () =>
    request<import("../types/algo").OrchestratorStatus>("GET", "/api/algo/status"),
  // ── decision trace (one row per evaluated minute) ──
  decisions: (opts?: { date?: string; zone?: string; limit?: number; compact?: boolean; since?: string }) =>
    request<{ date: string; zone: string; rows: import("../types/algo").DecisionRow[] }>(
      "GET", "/api/algo/decisions",
      {
        params: {
          date: opts?.date, zone: opts?.zone,
          limit: opts?.limit != null ? String(opts.limit) : undefined,
          compact: opts?.compact ? "true" : undefined, since: opts?.since,
        },
      }
    ),
  decisionLatest: () =>
    request<{ row: import("../types/algo").DecisionRow | null }>("GET", "/api/algo/decisions/latest"),
  backtestDecisions: (id: number, opts?: { day?: string; compact?: boolean }) =>
    request<import("../types/algo").DecisionRow[]>(
      "GET", `/api/algo/backtest/runs/${id}/decisions`,
      { params: { day: opts?.day, compact: opts?.compact ? "true" : undefined } }
    ),
  backtestDecisionAt: (id: number, day: string, ts: string) =>
    request<import("../types/algo").DecisionRow>(
      "GET", `/api/algo/backtest/runs/${id}/decisions/at`, { params: { day, ts } }
    ),
  resumeEngine: () => request<{ status: string }>("POST", "/api/algo/resume", { body: {} }),

  // ── live signal transitions (algo_signals read) ──
  signals: (opts?: { date?: string; zone?: string; indicator?: string; limit?: number }) =>
    request<{ date: string; zone: string; rows: SignalRow[] }>("GET", "/api/algo/signals", {
      params: {
        date: opts?.date, zone: opts?.zone, indicator: opts?.indicator,
        limit: opts?.limit != null ? String(opts.limit) : undefined,
      },
    }),

  // ── Telegram (§5) ──
  // ``force`` bypasses the backend's getMe cache (rate-limited server-side).
  telegramStatus: (force = false) =>
    request<TelegramStatus>("GET", "/api/algo/telegram/status", {
      params: { force: force ? "true" : undefined },
    }),
  // 200 {status:"sent"} · 409 not configured · 502 Telegram refused.
  telegramTest: () =>
    request<{ status: string; detail: string }>("POST", "/api/algo/telegram/test", { body: {} }),

  // ── exports (§6) — see exportUrl / downloadExportCsv / fetchExportRows ──
  exportUrl,
  downloadExportCsv,
  fetchExportRows,

  // ── audit ──
  audit: (opts?: { username?: string; since?: string; until?: string; limit?: number }) =>
    request<AuditRow[]>("GET", "/api/algo/audit", {
      params: {
        username: opts?.username,
        since: opts?.since,
        until: opts?.until,
        limit: opts?.limit != null ? String(opts.limit) : undefined,
      },
    }),
};
