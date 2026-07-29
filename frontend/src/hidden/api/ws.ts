import type { OIChangeResponse, OptionChainFullResponse, Timeframe } from "../types";

export type StreamMessage =
  | { type: "oi_change"; data: OIChangeResponse; bucket?: string }
  | { type: "option_chain_full"; data: OptionChainFullResponse; bucket?: string }
  | { type: "ping" }
  | { type: "error"; message: string; detail?: string };

export interface OIStreamHandlers {
  onMessage: (msg: StreamMessage) => void;
  onStatus?: (status: "connecting" | "open" | "closed" | "reconnecting") => void;
  /** Server-sent error frame (e.g. warming_up, bad params) before close. */
  onStreamError?: (message: string, detail?: string) => void;
}

const BACKOFF_MIN_MS = 500;
const BACKOFF_MAX_MS = 30_000;
/** Must exceed 2 × server ping interval (15s) so throttled tabs still reset the timer. */
const IDLE_CLOSE_MS = 55_000;

export class OIStream {
  private url: string;
  private ws: WebSocket | null = null;
  private handlers: OIStreamHandlers;
  private timeframe: Timeframe;
  private expiry: string | null;
  private symbol: string | null;
  private closed = false;
  private retry = 0;
  private heartbeatTimer: ReturnType<typeof setTimeout> | null = null;
  /** If `set:` is sent while the socket is still CONNECTING, send after `onopen`. */
  private pendingSetPayload: string | null = null;

  constructor(
    timeframe: Timeframe,
    expiry: string | null,
    handlers: OIStreamHandlers,
    symbol: string | null = null,
  ) {
    this.timeframe = timeframe;
    this.expiry = expiry;
    this.symbol = symbol;
    this.handlers = handlers;
    this.url = this.buildUrl();
    this.connect();
  }

  private buildUrl(): string {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const sameOrigin = `${protocol}//${window.location.host}`;
    const explicit = (import.meta.env.VITE_WS_BASE as string | undefined)?.trim();
    // Dev: same-origin `/ws` → Vite proxy unless VITE_DEV_WS_DIRECT=true and VITE_WS_BASE is set.
    // Prod: use VITE_WS_BASE when set (cross-origin API); else same-origin.
    const useExplicitWs =
      !!explicit &&
      (!import.meta.env.DEV || import.meta.env.VITE_DEV_WS_DIRECT === "true");
    const rawBase = useExplicitWs ? explicit! : sameOrigin;
    // Normalise http(s):// → ws(s):// so VITE_WS_BASE can be set to the API origin.
    const base = rawBase
      .replace(/^https:\/\//, "wss://")
      .replace(/^http:\/\//, "ws://");
    const u = new URL("/ws/oi-stream", base);
    u.searchParams.set("timeframe", this.timeframe);
    if (this.expiry) u.searchParams.set("expiry", this.expiry);
    if (this.symbol) u.searchParams.set("symbol", this.symbol);
    return u.toString();
  }

  private buildSetPayload(): string {
    const parts = [`tf=${this.timeframe}`];
    if (this.expiry) parts.push(`exp=${this.expiry}`);
    if (this.symbol) parts.push(`sym=${this.symbol}`);
    return `set:${parts.join(",")}`;
  }

  private teardownSocket() {
    if (this.heartbeatTimer) {
      clearTimeout(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
    const w = this.ws;
    this.ws = null;
    if (!w) return;
    // Detach first so intentional close() / reconnect does not scheduleReconnect.
    w.onopen = null;
    w.onmessage = null;
    w.onerror = null;
    w.onclose = null;
    try {
      w.close();
    } catch {
      /* ignore */
    }
  }

  private connect() {
    if (this.closed) return;
    this.teardownSocket();
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
      if (this.pendingSetPayload && this.ws?.readyState === WebSocket.OPEN) {
        try {
          this.ws.send(this.pendingSetPayload);
        } catch {
          /* ignore */
        }
        this.pendingSetPayload = null;
      }
    };
    socket.onmessage = (ev) => {
      this.armHeartbeat();
      try {
        const msg = JSON.parse(ev.data) as StreamMessage;
        if (msg.type === "ping") {
          this.ws?.send("pong");
          return;
        }
        if (msg.type === "error") {
          this.handlers.onStreamError?.(msg.message, msg.detail);
        }
        this.handlers.onMessage(msg);
      } catch {
        /* ignore malformed */
      }
    };
    socket.onerror = () => {
      this.ws?.close();
    };
    socket.onclose = () => {
      if (this.closed) {
        return;
      }
      if (this.ws === socket) {
        this.ws = null;
      }
      this.handlers.onStatus?.("closed");
      this.scheduleReconnect();
    };
  }

  private armHeartbeat() {
    if (this.heartbeatTimer) clearTimeout(this.heartbeatTimer);
    this.heartbeatTimer = setTimeout(() => {
      this.ws?.close();
    }, IDLE_CLOSE_MS);
  }

  private scheduleReconnect() {
    if (this.closed) return;
    this.retry += 1;
    const jitter = Math.random() * 250;
    const delay = Math.min(BACKOFF_MAX_MS, BACKOFF_MIN_MS * 2 ** Math.min(this.retry, 6)) + jitter;
    setTimeout(() => this.reconnect(), delay);
  }

  private reconnect() {
    if (this.closed) return;
    this.url = this.buildUrl();
    this.connect();
  }

  private sendOrQueue(payload: string) {
    if (!this.ws) {
      this.reconnect();
      return;
    }
    if (this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(payload);
      return;
    }
    if (this.ws.readyState === WebSocket.CONNECTING) {
      this.pendingSetPayload = payload;
      return;
    }
    this.reconnect();
  }

  setTimeframe(tf: Timeframe) {
    this.timeframe = tf;
    this.sendOrQueue(this.buildSetPayload());
  }

  setExpiry(exp: string | null) {
    this.expiry = exp;
    this.sendOrQueue(this.buildSetPayload());
  }

  setSymbol(sym: string | null) {
    this.symbol = sym;
    this.sendOrQueue(this.buildSetPayload());
  }

  close() {
    this.closed = true;
    this.pendingSetPayload = null;
    this.teardownSocket();
  }
}
