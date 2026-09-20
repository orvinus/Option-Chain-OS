/**
 * WebSocket client for the Algo Config live stream (`/ws/algo-stream`).
 *
 * Modelled on `api/ws.ts` (class `OIStream`) and deliberately kept to the same
 * reconnect contract — 500 ms→30 s jittered backoff, a 55 s idle-close that
 * must exceed 2× the server's 15 s ping so throttled background tabs still
 * reset it, and `pong` replies. Separate class rather than a shared base
 * because the two carry different frames and, more importantly, this one is
 * authenticated: the browser sends the httpOnly `algo_session` cookie on a
 * same-origin handshake, so there is no token to plumb.
 */

export interface AlgoReadings {
  intrabar: Record<string, { signal?: string; reading?: string; confidence?: number | null }> & {
    combined?: string;
  };
  closed: Record<string, { signal?: string; reading?: string; confidence?: number | null }> & {
    combined?: string;
  };
  closed_as_of: string | null;
  /** True when the intrabar preview disagrees with the basis the engine trades. */
  diverged: boolean;
  tail?: { ts: string; call_cr: number; put_cr: number } | null;
}

export interface AlgoBar {
  o: number;
  h: number;
  l: number;
  c: number;
}

export interface AlgoFrame {
  type: "algo";
  seq: number;
  ts: string;
  scope: {
    day: string;
    zone: string;
    symbol: string;
    expiry: string;
    strike: number | null;
    option_type: string;
  };
  live: boolean;
  /** replay · session_closed · other_symbol · no_data · stale · live */
  source: string;
  option_row_age_s: number | null;
  run_mode: string;
  nse_session_open: boolean;
  config_version?: number;
  error?: string;
  strike?: number | null;
  strike_source?: string;
  /** Set when the live window was empty and the frame shows the last stored bar. */
  frozen_at?: string | null;
  price?: { ltp: number | null };
  candle?: {
    m1: AlgoBar | null;
    b5: AlgoBar | null;
    m1_start: string;
    b5_start: string;
    /** The zone's UMP entry timeframe the b5 bar is bucketed on (5 | 15). */
    tf_min?: number;
    forming: boolean;
    as_of: string | null;
  } | null;
  readings?: AlgoReadings;
  /** The indicators the scoped zone actually trades on (§4.1 unanimous set). */
  enabled_indicators?: string[];
  status?: Record<string, unknown>;
}

export type AlgoStreamMessage =
  | AlgoFrame
  | { type: "ping" }
  | { type: "error"; message: string; detail?: string };

export interface AlgoScopeParams {
  day: string;
  zone: string;
  symbol?: string | null;
  expiry?: string | null;
  strike?: number | null;
  optionType?: string | null;
}

export type AlgoStreamStatus = "connecting" | "open" | "closed" | "reconnecting";

export interface AlgoStreamHandlers {
  onFrame: (frame: AlgoFrame) => void;
  onStatus?: (s: AlgoStreamStatus) => void;
  onStreamError?: (message: string, detail?: string) => void;
}

const BACKOFF_MIN_MS = 500;
const BACKOFF_MAX_MS = 30_000;
const IDLE_CLOSE_MS = 55_000;

export class AlgoStream {
  private ws: WebSocket | null = null;
  private url = "";
  private scope: AlgoScopeParams;
  private handlers: AlgoStreamHandlers;
  private closed = false;
  private retry = 0;
  private heartbeatTimer: ReturnType<typeof setTimeout> | null = null;
  private pendingSet: string | null = null;

  constructor(scope: AlgoScopeParams, handlers: AlgoStreamHandlers) {
    this.scope = scope;
    this.handlers = handlers;
    this.url = this.buildUrl();
    this.connect();
  }

  private buildUrl(): string {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const sameOrigin = `${protocol}//${window.location.host}`;
    const explicit = (import.meta.env.VITE_WS_BASE as string | undefined)?.trim();
    const useExplicit =
      !!explicit && (!import.meta.env.DEV || import.meta.env.VITE_DEV_WS_DIRECT === "true");
    const base = (useExplicit ? explicit! : sameOrigin)
      .replace(/^https:\/\//, "wss://")
      .replace(/^http:\/\//, "ws://")
      .replace(/\/$/, "");
    const p = new URLSearchParams();
    p.set("day", this.scope.day);
    p.set("zone", this.scope.zone);
    if (this.scope.symbol) p.set("symbol", this.scope.symbol);
    if (this.scope.expiry) p.set("expiry", this.scope.expiry);
    if (this.scope.strike != null) p.set("strike", String(this.scope.strike));
    if (this.scope.optionType) p.set("option_type", this.scope.optionType);
    return `${base}/ws/algo-stream?${p.toString()}`;
  }

  private connect() {
    if (this.closed) return;
    this.teardown();
    this.handlers.onStatus?.(this.retry === 0 ? "connecting" : "reconnecting");
    let socket: WebSocket;
    try {
      socket = new WebSocket(this.url);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.ws = socket;
    socket.onopen = () => {
      this.retry = 0;
      this.handlers.onStatus?.("open");
      this.armHeartbeat();
      if (this.pendingSet && this.ws?.readyState === WebSocket.OPEN) {
        try {
          this.ws.send(this.pendingSet);
        } catch {
          /* ignore */
        }
        this.pendingSet = null;
      }
    };
    socket.onmessage = (ev) => {
      this.armHeartbeat();
      try {
        const msg = JSON.parse(ev.data) as AlgoStreamMessage;
        if (msg.type === "ping") {
          this.ws?.send("pong");
          return;
        }
        if (msg.type === "error") {
          this.handlers.onStreamError?.(msg.message, msg.detail);
          return;
        }
        this.handlers.onFrame(msg);
      } catch {
        /* ignore malformed */
      }
    };
    socket.onerror = () => this.ws?.close();
    socket.onclose = () => {
      if (this.closed) return;
      if (this.ws === socket) this.ws = null;
      this.handlers.onStatus?.("closed");
      this.scheduleReconnect();
    };
  }

  private armHeartbeat() {
    if (this.heartbeatTimer) clearTimeout(this.heartbeatTimer);
    this.heartbeatTimer = setTimeout(() => this.ws?.close(), IDLE_CLOSE_MS);
  }

  private scheduleReconnect() {
    if (this.closed) return;
    this.retry += 1;
    const jitter = Math.random() * 250;
    const delay =
      Math.min(BACKOFF_MAX_MS, BACKOFF_MIN_MS * 2 ** Math.min(this.retry, 6)) + jitter;
    setTimeout(() => {
      if (this.closed) return;
      this.url = this.buildUrl();
      this.connect();
    }, delay);
  }

  /** Re-scope in place. The server picks the new scope up on its next tick. */
  setScope(scope: AlgoScopeParams) {
    this.scope = scope;
    const payload =
      `set:day=${scope.day},zone=${scope.zone}` +
      (scope.symbol ? `,sym=${scope.symbol}` : "") +
      (scope.expiry ? `,exp=${scope.expiry}` : "") +
      `,strike=${scope.strike ?? "auto"}` +
      (scope.optionType ? `,ot=${scope.optionType}` : "");
    if (this.ws?.readyState === WebSocket.OPEN) {
      try {
        this.ws.send(payload);
        return;
      } catch {
        /* fall through to queue */
      }
    }
    this.pendingSet = payload;
    if (!this.ws) {
      this.url = this.buildUrl();
      this.connect();
    }
  }

  private teardown() {
    if (this.heartbeatTimer) {
      clearTimeout(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
    if (this.ws) {
      this.ws.onopen = null;
      this.ws.onmessage = null;
      this.ws.onerror = null;
      this.ws.onclose = null;
      try {
        this.ws.close();
      } catch {
        /* ignore */
      }
      this.ws = null;
    }
  }

  close() {
    this.closed = true;
    this.teardown();
  }
}
