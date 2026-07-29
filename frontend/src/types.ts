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

/** One time bucket of total Call/Put OI for the Charts page — from `/api/oi-timeseries`. */
export interface OITimeseriesPoint {
  /** ISO-8601 IST bucket-start timestamp. */
  ts: string;
  total_call_oi: number;
  total_put_oi: number;
  /** Call/Put ratio and PCR of the bucketed totals (null when a side is 0). */
  ratio?: number | null;
  pcr?: number | null;
}

export interface OITimeseriesResponse {
  symbol: string;
  expiry: string;
  bucket: string;
  points: OITimeseriesPoint[];
}

/** IST trading days with stored data — from `/api/history-dates` (date picker). */
export interface HistoryDatesResponse {
  symbol: string;
  expiry: string;
  /** "YYYY-MM-DD", newest first. */
  dates: string[];
}

/** One bucket of ratio/PCR over time — from `/api/ratio-timeseries` (Ratio chart). */
export interface RatioPoint {
  ts: string;
  ratio: number | null;
  pcr: number | null;
  total_call_oi: number;
  total_put_oi: number;
}

export interface RatioTimeseriesResponse {
  symbol: string;
  expiry: string;
  bucket: string;
  points: RatioPoint[];
}

/** One timeframe row of the multi-timeframe grid — from `/api/multi-timeframe`. */
export interface MtfRow {
  timeframe: Timeframe;
  call_oi_change: number;
  put_oi_change: number;
  /** Ratio of the changes (call_oi_change / put_oi_change), null on 0 put change. */
  oi_change_ratio: number | null;
}

export interface MultiTimeframeResponse {
  symbol: string;
  expiry: string;
  asof: string;
  computed_at: string;
  /** Point-in-time levels, shared by every row. */
  spot: number | null;
  atm_strike: number | null;
  total_call_oi: number;
  total_put_oi: number;
  ratio: number | null; // call/put level
  pcr: number | null; // put/call level
  rows: MtfRow[];
}

/** One per-strike row inside a replay frame. */
export interface ReplayRow {
  strike: number;
  call_oi: number;
  put_oi: number;
  call_oi_change: number;
  put_oi_change: number;
  /** Greeks/IV (present only when ?with_greeks and the populator has run). */
  call_iv?: number | null;
  put_iv?: number | null;
  call_delta?: number | null;
  put_delta?: number | null;
  call_gamma?: number | null;
  put_gamma?: number | null;
  call_theta?: number | null;
  put_theta?: number | null;
  call_vega?: number | null;
  put_vega?: number | null;
}

/** One time-step of a replay session — from `/api/replay`. */
export interface ReplayFrame {
  ts: string;
  spot: number | null;
  atm: number | null;
  total_call_oi: number;
  total_put_oi: number;
  total_call_oi_change: number;
  total_put_oi_change: number;
  ratio: number | null;
  pcr: number | null;
  rows: ReplayRow[];
}

export interface ReplayFramesResponse {
  symbol: string;
  expiry: string;
  frames: ReplayFrame[];
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
  auth_mode: string;
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

export interface LoginRequest {
  client_code?: string;
  mpin: string;
  totp_code?: string;
}

export interface LoginResponse {
  status: string;
  message: string;
  authenticated: boolean;
}

/** Rich option chain row (OI change, volume, IV, trends) — from `/api/option-chain-full` + WS. */
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
}

export interface OptionChainFullResponse {
  timeframe: Timeframe;
  expiry: string;
  spot: number | null;
  asof: string;
  computed_at: string;
  lot_size: number;
  rows: OptionChainFullRow[];
}
