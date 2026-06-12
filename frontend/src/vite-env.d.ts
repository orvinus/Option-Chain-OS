/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE?: string;
  readonly VITE_WS_BASE?: string;
  /** When "true" in dev, WebSocket uses VITE_WS_BASE instead of same-origin proxy. */
  readonly VITE_DEV_WS_DIRECT?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
