export type Timeframe =
  | "1s"
  | "15s"
  | "30s"
  | "45s"
  | "1m"
  | "3m"
  | "5m"
  | "10m"
  | "15m"
  | "30m"
  | "1h"
  | "2h"
  | "3h"
  | "full_day";

export interface OIChangeRow {
  strike: number;
  call_oi: number;
  put_oi: number;
  call_oi_change: number;
  put_oi_change: number;
  call_ltp: number | null;
  put_ltp: number | null;
  call_ltp_change: number | null;
  put_ltp_change: number | null;
}

export interface OIChangeResponse {
  /** A preset timeframe, or "range" for a custom from/to window. */
  timeframe: Timeframe | "range";
  expiry: string;
  spot: number | null;
  /** Latest exchange timestamp on stored option rows (feed clock; may freeze after hours). */
  asof: string;
  /** IST wall-clock when this snapshot was built — use for “last updated” on the dashboard. */
  computed_at?: string;
  total_call_oi_change: number;
  total_put_oi_change: number;
  rows: OIChangeRow[];
}

export interface ExpiriesResponse {
  expiries: string[];
}

export interface SpotResponse {
  symbol: string;
  spot: number | null;
  asof: string | null;
}

export interface NiftyCrossCheckResponse {
  our_spot: number | null;
  reference_last: number | null;
  reference_source: string;
  diff_points: number | null;
  aligned: boolean | null;
  tolerance_points: number;
  pipeline_note: string;
  fetch_detail: string | null;
}

export interface HealthResponse {
  status: string;
  session_open_ist?: string;
  session_close_ist?: string;
  authenticated: boolean;
  latest_spot: number | null;
  tokens_subscribed: number;
  last_flush_at: string | null;
  expiries: string[];
  run_mode: string;
  now_ist: string;
  nse_session_open: boolean;
  feed_connected: boolean;
  active_symbol: string;
}

export interface SymbolEntry {
  symbol: string;
  display: string;
  kind: "index" | "stock";
  sector: string;
  fno_eligible: boolean;
  lot_size: number;
  strike_step: number;
}

export interface SymbolSectorGroup {
  sector: string;
  symbols: SymbolEntry[];
}

export interface SymbolsResponse {
  active_symbol: string;
  groups: SymbolSectorGroup[];
}

export interface ActiveSymbolResponse {
  symbol: string;
  display: string;
  fno_eligible: boolean;
  spot: number | null;
  expiries: string[];
}

export interface InterpretationRow {
  strike: number;
  call: string;
  put: string;
}

/** Empty by design — the XTS market-data session authenticates with the
 *  appKey/secretKey in the backend's .env, not per-user credentials. */
export type LoginRequest = Record<string, never>;

export interface LoginResponse {
  status: string;
  message: string;
  authenticated: boolean;
}

/** Rich option chain row (OI change, volume, IV, Greeks, trends) — from `/api/option-chain-full` + WS. */
export interface OptionChainFullRow {
  strike: number;
  call_oi: number;
  put_oi: number;
  call_oi_change: number;
  put_oi_change: number;
  call_ltp: number | null;
  put_ltp: number | null;
  call_ltp_change: number | null;
  put_ltp_change: number | null;
  call_volume: number;
  put_volume: number;
  call_iv: number | null;
  put_iv: number | null;
  call_trend: string;
  put_trend: string;
  pcr_oi: number | null;
  pcr_volume: number | null;
  pe_ce_oi: number;
  pe_ce_oi_change: number;
  call_delta?: number | null;
  call_gamma?: number | null;
  call_theta?: number | null;
  call_vega?: number | null;
  put_delta?: number | null;
  put_gamma?: number | null;
  put_theta?: number | null;
  put_vega?: number | null;
}

export interface OptionChainFullResponse {
  timeframe: Timeframe;
  expiry: string;
  spot: number | null;
  asof: string;
  computed_at: string;
  lot_size: number;
  rows: OptionChainFullRow[];
  synthetic_future?: number | null;
  atm_iv?: number | null;
  ivp?: number | null;
}

export interface IvScannerRow {
  symbol: string;
  display: string;
  price: number | null;
  price_chg: number | null;
  price_chg_pct: number | null;
  total_oi: number;
  total_oi_chg: number;
  total_oi_chg_pct: number | null;
  pcr: number | null;
  iv: number | null;
  iv_chg_pct: number | null;
  iv_range_1y_low: number | null;
  iv_range_1y_high: number | null;
  hv_10: number | null;
  hv_20: number | null;
  hv_30: number | null;
  ivr: number | null;
  ivp: number | null;
  iv_hv10: number | null;
  iv_hv20: number | null;
  iv_hv30: number | null;
  history_ready: boolean;
}

export interface IvScannerResponse {
  mode: "latest" | "historical" | string;
  expiry_slot: "near" | "next" | "far" | string;
  expiry: string | null;
  asof: string;
  max_symbols: number;
  rows: IvScannerRow[];
  note: string | null;
}
