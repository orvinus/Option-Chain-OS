/**
 * TypeScript mirror of the backend Algo Config document
 * (backend/app/algo/config_models.py, serialized with by_alias=True — the
 * global scope arrives under the key "global").
 *
 * Keep field names in exact sync with the backend: the save endpoint validates
 * the whole document server-side, so a drifted field silently disappears on
 * the next round-trip instead of erroring.
 */

export type Weekday = "monday" | "tuesday" | "wednesday" | "thursday" | "friday";
export type ZoneId = "Z1" | "Z2" | "Z3";
export type IndicatorKey = "oi_change" | "multi_tf" | "ratio";
export type Side3 = "Call" | "Put" | "Ignore";
/** Signal Console F1 — how enabled readings become one decision. */
export type CombineRule = "unanimous" | "majority";
/** Signal Console F1 — what an enabled NO_TRADE reading means. */
export type NeutralMode = "block" | "abstain";
/** Signal Console F5 — which candle the red-candle rule reads. */
export type CandleRead = "closed" | "forming";

export const WEEKDAYS: Weekday[] = ["monday", "tuesday", "wednesday", "thursday", "friday"];
export const ZONE_IDS: ZoneId[] = ["Z1", "Z2", "Z3"];
export const WEEKDAY_LABEL: Record<Weekday, string> = {
  monday: "Monday",
  tuesday: "Tuesday",
  wednesday: "Wednesday",
  thursday: "Thursday",
  friday: "Friday",
};
export const INDICATOR_LABEL: Record<IndicatorKey, string> = {
  oi_change: "OI Change",
  multi_tf: "Multi-TF",
  ratio: "Ratio",
};

// ── Entry-filter indicator №1: OI Structure Engine ("OI Change" slot) ──

export interface OIStructureMatrix {
  call: Record<string, Side3>;
  put: Record<string, Side3>;
}

export interface OIStructureParams {
  left_bars: number;
  right_bars: number;
  min_swing_move_cr: number;
  eq_tolerance_cr: number;
  breakout_buffer_cr: number;
  swing_weight: number;
  breakout_weight: number;
  sweep_weight: number;
  red_candle_weight: number;
  agreement_bonus: number;
  confidence_threshold: number;
  engine_1m_on: boolean;
  engine_5m_on: boolean;
  /** Only swings confirmed at or after (current bar − N) contribute POINTS.
   *  0 = the whole session (the original behaviour). Zones and breakouts are
   *  never filtered. Optional on configs saved before 2026-09-19 (= 0). */
  swing_scoring_lookback?: number;
  /** 1-minute bars per candle for the red-candle rule. Optional on configs
   *  saved before 2026-09-19 (= 5). */
  candle_size_bars?: number;
  /** Which candle the red rule reads. Optional on configs saved before
   *  2026-09-19 (= "closed"). */
  candle_read?: CandleRead;
  strikes_atm_window: number;
  matrix: OIStructureMatrix;
}

// ── Entry-filter indicator №2: MTF Ratio rules ("Multi-TF" slot) ──

export interface MtfCondition {
  timeframe: string;
  side: "Any" | "Call" | "Put";
  operator: "Any" | "Above" | "Below";
  threshold: string;
  call_sign: "Any" | "Positive" | "Negative";
  put_sign: "Any" | "Positive" | "Negative";
}

export interface MtfRule {
  name: string;
  on: boolean;
  out: "Call" | "Put" | "Neutral";
  conditions: MtfCondition[];
}

export interface MtfRatioParams {
  timeframes: string[];
  strikes_atm_window: number;
  rules: MtfRule[];
}

// ── Entry-filter indicator №3: MQAE ("Ratio" slot) ──

export interface MqaeParams {
  risk_mode: "aggressive" | "conservative";
  strikes_atm_window: number;
  master_threshold: number;
  velocity_on: boolean;
  velocity_period: number;
  velocity_surge_threshold: number;
  order_blocks_on: boolean;
  ob_impulse_trigger: number;
  ob_zone_width: number;
  smc_on: boolean;
  smc_swing_bars: number;
  smc_min_move: number;
  trend_rider_on: boolean;
  trail_period: number;
  trail_buffer: number;
  crossover_on: boolean;
  /** The candle the five models run on; optional on configs saved before 2026-09-15 (= "1m"). */
  timeframe?: "1m" | "5m" | "10m" | "15m" | "30m" | "full_day";
}

// ── Execution engine: NIFTY Ultra Master Pro (5 groups) ──

export interface UmpInstitutionalLaws {
  expansion_law_pct: number;
  bridge_law_pct: number;
  median_law_pct: number;
  price_floor_inr: number;
}

export interface UmpStructuralDetection {
  body_match_pts: number;
  scan_depth_bars: number;
}

export interface UmpVisual {
  line_extension_bars: number;
  label_right_offset_bars: number;
  show_struct: boolean;
  show_discovery: boolean;
  show_bridge: boolean;
  show_median: boolean;
  median_color: string;
  median_line_width: number;
  zone_width_pct: number;
}

export interface UmpEntryModel {
  show_entry_signals: boolean;
  enable_long: boolean;
  enable_retest: boolean;
  show_sl_lines: boolean;
  show_trail_labels: boolean;
  max_sl_pct: number;
  trigger_timeout_bars: number;
  /** Trade-entry candle timeframe (§1 2026-09-09). Levels are unaffected. */
  entry_timeframe_min: 5 | 15;
  /** Per-entry-kind switches under the two master switches (§2). Missing
   *  keys mean ON. */
  scenarios: Record<string, boolean>;
}

/** Every UMP entry kind, in display order (mirrors backend UMP_SCENARIOS). */
export const UMP_SCENARIOS = [
  "S1A", "S1B", "S1C", "S2A", "S2B", "S3A", "S3B", "S3C", "R1", "R2",
] as const;
export type UmpScenario = (typeof UMP_SCENARIOS)[number];

/** UMP chart display interval (§3) — TradingView's NSE set. "entry" = the
 *  zone's entry timeframe. Sub-minute intervals come from the 1-second live
 *  rows (cursor day only); minute+ intervals are session-anchored at 09:15. */
export const UMP_DISPLAY_INTERVALS = [
  "1s", "5s", "15s", "30s",
  "1m", "3m", "5m", "10m", "15m", "30m",
  "1h", "2h", "3h", "1d", "1W",
  "entry",
] as const;
export type UmpDisplayInterval = (typeof UMP_DISPLAY_INTERVALS)[number];

export interface UmpDashboard {
  show_dashboard: boolean;
  show_key_levels: boolean;
  dashboard_position: string;
  levels_position: string;
}

export interface UmpParams {
  institutional: UmpInstitutionalLaws;
  structural: UmpStructuralDetection;
  visual: UmpVisual;
  entry: UmpEntryModel;
  dashboard: UmpDashboard;
}

/** Engine exit codes → readable labels (§12.4's human naming — the raw code
 *  stays in the data/CSV; only the display translates). */
export const EXIT_REASON_LABEL: Record<string, string> = {
  MAX_SL: "Stop-Loss (Max cap)",
  BASE_SL: "Stop-Loss (Base)",
  TRAIL_EXIT: "Trailing Stop",
  SB_EXIT: "Zone Trail (System B)",
  TARGET: "Target",
  ZONE_END_EXIT: "Zone End-Exit",
  EOD_FORCE_CLOSE: "EOD Force Close",
  EXPIRY_FORCE_CLOSE: "Expiry Force Close",
};

export function exitReasonLabel(code: string | null | undefined): string {
  if (!code) return "—";
  return EXIT_REASON_LABEL[code] ?? code;
}

// ── Zone / day / global scopes ──

export interface ZoneConfig {
  start: string;
  end: string;
  premium_min: number;
  premium_max: number;
  /** Multi-strike hunting: the N in-band strikes nearest the band midpoint
   *  each hunt with their own UMP engine; first valid entry wins.
   *  1 = the original single-strike behaviour. */
  strike_scan_count: number;
  max_trades: number;
  zone_kill: boolean;
  strategy_active: boolean;
  reeval_cadence: "every_candle" | "zone_start";
  /** EXPERIMENTAL: keep a hunt alive N minutes through NO_TRADE
   *  flickers (opposite side still discards). 0 = spec behavior. */
  direction_hold_min: number;
  enabled_indicators: IndicatorKey[];
  /** How the enabled indicators' readings become one decision. A DISABLED
   *  indicator is simply absent from enabled_indicators and never votes.
   *  Optional on configs saved before 2026-09-19 (= "unanimous"). */
  combine_rule?: CombineRule;
  /** What an enabled-but-undecided (NO_TRADE) reading means while combining.
   *  Optional on configs saved before 2026-09-19 (= "block"). */
  neutral_mode?: NeutralMode;
  oi_structure: OIStructureParams;
  mtf_ratio: MtfRatioParams;
  mqae: MqaeParams;
  ump: UmpParams;
}

export interface DayAlertToggles {
  telegram_trade_entry_exit: boolean;
}

export interface DayConfig {
  index_symbol: string;
  day_kill: boolean;
  /** ATM ± N strikes of live DATA the feed collects for this day's index
   *  (drives the TrueData subscription window; >12 truncates on trial cap). */
  data_strike_window: number;
  all_in: boolean;
  allocation_pct: number;
  max_loss_pct: number;
  max_profit_lock_pct: number;
  max_consec_losses: number;
  max_consec_loss_pct: number;
  end_exit_enabled: boolean;
  alerts: DayAlertToggles;
  zones: Record<ZoneId, ZoneConfig>;
}

export interface FeeConfig {
  brokerage_per_order: number;
  stt_sell_premium_pct: number;
  exchange_txn_pct: number;
  sebi_turnover_pct: number;
  ipft_pct: number;
  gst_pct: number;
  stamp_duty_buy_pct: number;
}

export interface PaperConfig {
  paper_mode: boolean;
  virtual_balance: number;
  session_started: string;
  slippage_pct: number;
  latency_ms: number;
  fill_source: "ltp" | "bid_ask_mid";
  shadow_mode: boolean;
}

export interface HolidayEntry {
  date: string;
  occasion: string;
}

export interface ConfigLock {
  lock_market_hours: boolean;    // refuse saves during the NSE session
  require_save_confirm: boolean; // show the diff modal before every save
}

export interface GlobalConfig {
  master_kill: boolean;
  /** Pine parity (locked 2026-08-19): positions carry overnight — only the
   *  exit ladder closes them; End-Exit is ignored; expiry-day 15:25 IST
   *  force-close is the sole automatic day-close. */
  overnight_carry: boolean;
  demat_balance: number;
  paper: PaperConfig;
  fees: FeeConfig;
  holidays: HolidayEntry[];
  config_lock: ConfigLock;
}

export interface AlgoConfigDoc {
  schema_rev: number;
  global: GlobalConfig;
  days: Record<Weekday, DayConfig>;
}

// ── API envelopes ──

export interface ConfigEnvelope {
  version: number;
  saved_by: string;
  saved_at: string;
  note: string;
  config: AlgoConfigDoc;
}

export interface SaveResponse {
  version: number;
  warnings: string[];
}

/** One evaluated minute of the orchestrator (algo_decisions / algo_backtest_decisions). */
export interface DecisionIndexRow {
  id: number;
  ts: string;
  trade_date: string;
  day: string;
  zone_id: string;
  symbol: string;
  stage: string;
  state: string;
  direction: string;
  unanimous: boolean | null;
  decision: "accept" | "reject" | "manage" | "skip";
  reason: string;
  data_age_s: number | null;
  trade_id: number | null;
  n_candidates?: number;
}

export interface DecisionRow extends DecisionIndexRow {
  expiry: string | null;
  ledger: string;
  config_version: number | null;
  gate_blocks: string[] | null;
  readings: Record<string, Record<string, unknown>> | null;
  candidates: Record<string, unknown>[] | null;
  sizing: Record<string, unknown> | null;
  position: Record<string, unknown> | null;
  zone_snapshot: Record<string, unknown> | null;
}

export interface ValidationReport {
  errors: string[];
  warnings: string[];
  zone_gate: Record<string, string[]>;
}

export interface AlgoIdentity {
  username: string;
  role: string;
}

export interface ConfigVersionMeta {
  version: number;
  saved_by: string;
  saved_at: string;
  note: string;
}

export interface AuditRow {
  id: number;
  ts: string;
  user_id: number | null;
  username: string;
  event_type: string;
  scope: string;
  field: string;
  old_value: string | null;
  new_value: string | null;
  detail: Record<string, unknown> | null;
}

// ── Engine evaluation responses ──

export interface EngineSwing {
  index: number;
  value: number;
  type: "high" | "low";
  label: string; // H/L, HH/HL/LH/LL, EQH/EQL
}

export interface EngineContribution {
  label: string;
  side: string;
  points: number;
}

export interface MtfRatioRow {
  timeframe: string;
  call_delta_cr: number;
  put_delta_cr: number;
  side: "Call" | "Put" | "Neutral";
  factor: number | null; // null = infinite (one side has zero Δ)
  text: string;
  lowest_side: "Call" | "Put" | "Neutral";
  call_sign: "Positive" | "Negative" | "Zero";
  put_sign: "Positive" | "Negative" | "Zero";
}

export interface MtfRuleTrace {
  name: string;
  on: boolean;
  matched: boolean;
  out: string;
  checks: { timeframe: string; passed: boolean; reason: string }[];
}

export interface MtfRatioEvalResponse {
  day: Weekday;
  zone: ZoneId;
  symbol: string;
  expiry: string;
  strike_min: number;
  strike_max: number;
  spot: number | null;
  as_of: string;
  rows: MtfRatioRow[];
  direction: "Call" | "Put" | "Neutral";
  reading: "CALL" | "PUT" | "NO_TRADE";
  matched_rule: string | null;
  traces: MtfRuleTrace[];
  params: Record<string, unknown>;
}

// ── Trades / P&L / status ──

export interface TradeRow {
  id: number;
  trade_date: string;
  day: string;
  zone_id: string;
  index_symbol: string;
  side: "CALL" | "PUT";
  strike: number | null;
  expiry: string | null;
  entry_ts: string;
  entry_price: number;
  exit_ts: string | null;
  exit_price: number | null;
  lots: number;
  pnl_rupees: number | null;
  pnl_pct: number | null;
  exit_reason: string;
  ledger: "live" | "paper";
  sub_scenario: string;
  fees: Record<string, number> | null;
}

export interface PnlGroupRow {
  group: string;
  trades: number;
  wins: number;
  losses: number;
  pnl: number;
  gross_win: number;
  gross_loss: number;
  allocated?: number;
  pnl_pct?: number | null;
}

export interface PnlSummary {
  ledger: string;
  from_date: string;
  to_date: string;
  trades: number;
  wins: number;
  losses: number;
  win_rate: number | null;
  pnl: number;
  avg_win: number | null;
  avg_loss: number | null;
  profit_factor: number | null;
  pnl_pct_blended: number | null;
  by_day: PnlGroupRow[];
  by_zone: PnlGroupRow[];
  by_date: PnlGroupRow[];
  /** §7 max drawdown over the period's closed trades (peak-to-trough ₹, % of
   *  peak equity, exit timestamps of the peak and trough). */
  max_drawdown?: number | null;
  max_drawdown_pct?: number | null;
  max_drawdown_from?: string | null;
  max_drawdown_to?: string | null;
  drawdown_basis?: string | null;
}

export interface PnlCalendar {
  month: string;
  ledger: string;
  days: Record<string, { pnl: number; pnl_pct: number | null; trades: number }>;
}

/** A-to-Z broker account state; balance/positions/orders arrive as the
 *  gateway's own structures — the panel renders known keys plus a generic
 *  detail view so nothing is hidden. */
export interface BrokerAccount {
  connected: boolean;
  error: string | null;
  balance: Record<string, unknown> | null;
  positions: Record<string, unknown>[];
  orders: Record<string, unknown>[];
  /** Present when the gateway is offline: the last successful payload. */
  last_snapshot?: {
    payload: {
      balance: Record<string, unknown> | null;
      positions: Record<string, unknown>[];
      orders: Record<string, unknown>[];
    };
    fetched_at: string;
  } | null;
}

export interface PaperOpenPosition {
  trade_id: number;
  strike: number | null;
  option_type: "CE" | "PE" | null;
  side: "CALL" | "PUT";
  lots: number;
  entry_fill: number;
  entry_ts: string;
  last_close: number | null;
  unrealized_gross: number | null;
  unrealized_pct: number | null;
}

export interface PaperSession {
  session_started: string;
  starting_balance: number;
  realized_pnl: number;
  current_balance: number;
  /** §8 paper completeness — the simulator's open position (null when flat). */
  open_position?: PaperOpenPosition | null;
  unrealized_pnl?: number;
  /** current_balance + unrealized_pnl. */
  equity?: number;
  trades_today?: number;
  realized_today?: number;
}

/** `GET /api/algo/telegram/status` (§5). `ok` is null until a probe ran. */
export interface TelegramStatus {
  configured: boolean;
  ok: boolean | null;
  bot_username: string | null;
  chat_id_masked: string | null;
  checked_at: string | null;
  error: string | null;
  min_interval_s: number;
}

/** One `algo_signals` row (`GET /api/algo/signals`). */
export interface SignalRow {
  id: number;
  ts: string;
  trade_date: string;
  day: string;
  zone_id: string;
  indicator: string;
  reading: string;
  payload: Record<string, unknown> | null;
}

/** Datasets served by `/api/algo/export/*` (§6). Ledger datasets take
 *  `ledger=paper|live`; backtest datasets are scoped to a run id. */
export type ExportDataset =
  | "trades" | "decisions" | "signals" | "calendar" | "summary"
  | "days" | "equity" | "settings";

export interface OrchestratorStatus {
  running: boolean;
  state?: string;
  active_zone?: string;
  direction?: string;
  readings?: Record<string, string>;
  position?: Record<string, unknown> | null;
  gate_blocks?: string[];
  paused_reason?: string;
  realized_pnl_today?: number;
  trades_today?: number;
  last_evaluated?: string;
  /** How many strikes are currently being hunted in parallel. */
  hunting_strikes?: number;
}

export interface UmpCandle {
  ts: string;
  o: number;
  h: number;
  l: number;
  c: number;
}

export interface UmpStateRow {
  ts: string;
  in_trade: boolean;
  sub: string;
  entry_price: number | null;
  base_level: number | null;
  max_sl: number | null;
  trail_sl: number | null;
  sb_trail_sl: number | null;
  sb_stage: number;
  trig: boolean;
  levels_ready: boolean;
}

export interface UmpEvalResponse {
  day: Weekday;
  zone: ZoneId;
  symbol: string;
  expiry: string;
  strike: number;
  /** How the strike was chosen: the zone's premium-band pick (what the
   *  orchestrator hunts), an explicit manual override, or ATM fallback. */
  strike_source: "band" | "manual" | "atm_fallback";
  /** The top-N band candidates the orchestrator would hunt (nearest
   *  band-mid first). */
  band_candidates: { strike: number; premium: number }[];
  /** EVERY stored strike with its last premium (manual-selection ladder). */
  all_strikes: { strike: number; premium: number }[];
  /** IST moment the candidates/ladder premiums are from — off-hours this is
   *  the chain's last stored trade (the ladder frozen at the close). */
  candidates_as_of: string | null;
  option_type: "CE" | "PE";
  /** The as-of cursor's IST date (= last replayed day). The replay itself
   *  spans the contract's FULL stored life: first_session_date → session_date. */
  session_date: string;
  first_session_date: string;
  /** Every IST date present in the replay (sparse early life included). */
  session_dates: string[];
  /** The resolved as-of cursor instant (IST ISO). */
  as_of: string;
  /** The expiry-to-expiry week the chart opens zoomed to (prev-expiry+1 →
   *  expiry, clamped to stored data). Null when nothing replayed. */
  focus_window: { from: string; to: string } | null;
  gate: {
    daily_feeds: number;
    weekly_feed: boolean;
    h1_candles: number;
    levels_ready: boolean;
  };
  state: "IDLE" | "WATCHING" | "IN_TRADE";
  sub_scenario: string;
  entry_price: number | null;
  base_level: number | null;
  max_sl: number | null;
  trail_sl: number | null;
  sb_trail_sl: number | null;
  sb_stage: number;
  levels: { price: number; type: number; name: string }[];
  active_zone: {
    base: number;
    zone_top: number;
    upper_median: number;
    lower_median: number;
    zone_bottom: number;
  } | null;
  q_levels: { q1: number; q2: number; q3: number; nb: number } | null;
  /** Pine dashboard rows: the nearest plotted level above/below the last
   *  close, with its unsigned %-distance. Null until levels + a close exist. */
  nearest_resistance: { price: number; distance_pct: number } | null;
  nearest_support: { price: number; distance_pct: number } | null;
  /** Previous session's daily close (the dashboard's change-% base). */
  prev_close: number | null;
  /** The zone's entry timeframe the engine evaluated (5 | 15). */
  entry_timeframe_min: number;
  /** Entry-timeframe candles (what the engine evaluated), full stored life. */
  entry_candles: UmpCandle[];
  /** DISPLAY candles for the requested `interval` (= entry_candles when
   *  interval is "entry"). 1s covers the cursor day only. */
  candles: UmpCandle[];
  /** Seconds per display candle: 1 | 60 | 300 | 900. */
  candles_interval: number;
  candles_source: "entry" | "minutes" | "live_1s";
  /** Set when 1s was requested but unavailable (fell back to 1m) or to
   *  state the 1s coverage. */
  candles_note: string | null;
  candles_window: { from: string; to: string } | null;
  /** REPLAY ONLY (sent with `history=true`, and only when the display interval
   *  is coarser than a minute): the same span at 1-minute resolution, folded
   *  from the SAME filled series as `candles`. The replay animates price moving
   *  inside a forming candle from these. */
  subcandles?: UmpCandle[] | null;
  subcandles_interval?: number | null;
  /** Present only with `history=true`: the level set after every change
   *  (one row per rebuild that changed something) and the engine state after
   *  every change — the client reconstructs any earlier instant from these. */
  levels_history: { ts: string; levels: { price: number; type: number; name: string }[] }[] | null;
  state_history: UmpStateRow[] | null;
  events: { ts: string; kind: string; price: number; text: string }[];
  trades: {
    entry_ts: string;
    entry_price: number;
    sub_scenario: string;
    base_level: number;
    exit_ts: string | null;
    exit_price: number | null;
    exit_reason: string;
    pnl_points: number | null;
  }[];
  params: Record<string, unknown>;
}

export interface MqaeModelLog {
  model: string;
  text: string;
  points: number;
}

export interface MqaeEvalResponse {
  day: Weekday;
  zone: ZoneId;
  symbol: string;
  expiry: string;
  replay_at: string | null;
  strike_min: number;
  strike_max: number;
  spot: number | null;
  timestamps: string[];
  green_pcr: number[];
  yellow_ratio: number[];
  /** Total CE/PE OI per closed bucket (index-aligned with `timestamps`); optional on older backends. */
  total_call_oi?: number[];
  total_put_oi?: number[];
  /** The timeframe the models ran on and the exact series they saw — swing,
   *  order-block, rider and velocity indices and history bars index these. */
  timeframe?: "1m" | "5m" | "10m" | "15m" | "30m" | "full_day";
  /** True when the evaluation used the panel's unsaved parameters. */
  preview?: boolean;
  engine_timestamps?: string[];
  engine_green?: number[];
  engine_yellow?: number[];
  signal: "CALL" | "PUT" | "NO_TRADE";
  total: number;
  score_green: number;
  score_yellow: number;
  score_cross: number;
  logs_green: MqaeModelLog[];
  logs_yellow: MqaeModelLog[];
  logs_cross: MqaeModelLog[];
  green_swings: EngineSwing[];
  yellow_swings: EngineSwing[];
  green_blocks: { type: "DEMAND" | "SUPPLY"; top: number; bot: number; origin_index: number }[];
  yellow_blocks: { type: "DEMAND" | "SUPPLY"; top: number; bot: number; origin_index: number }[];
  green_rider: { val: number; trend: number }[];
  green_velocity: number[];
  yellow_velocity: number[];
  history: { bar: number; ts: string | null; signal: "CALL" | "PUT" | "NO_TRADE"; total: number }[];
  params: Record<string, unknown>;
}

export interface OiStructureEvalResponse {
  day: Weekday;
  zone: ZoneId;
  symbol: string;
  expiry: string;
  strike_min: number;
  strike_max: number;
  spot: number | null;
  timestamps: string[];
  call_series_cr: number[];
  put_series_cr: number[];
  call_swings: EngineSwing[];
  put_swings: EngineSwing[];
  call_zone: { hi: number; lo: number } | null;
  put_zone: { hi: number; lo: number } | null;
  call_breakout: "up" | "down" | null;
  put_breakout: "up" | "down" | null;
  call_red_5m: boolean;
  put_red_5m: boolean;
  call_pts: number;
  put_pts: number;
  signal: "CALL" | "PUT" | "NO_TRADE";
  confidence: number;
  contributions: EngineContribution[];
  payload: Record<string, unknown>;
  params: Record<string, unknown>;
  /** Per-strike Δ since open at `as_of` (the last closed bucket); optional on older backends / null on failure. */
  by_strike?: { strike: number; call_oi: number; put_oi: number; call_oi_change: number; put_oi_change: number }[] | null;
  as_of?: string | null;
  strike_step?: number | null;
}

// ── Backtesting ─────────────────────────────────────────────────────────────

export interface BacktestRunRequest {
  label: string;
  from_date: string;
  to_date: string;
  config:
    | { source: "live" }
    | { source: "version"; version: number }
    | { source: "inline"; document: AlgoConfigDoc };
  settings: {
    balance_mode: "compounding" | "fixed_per_day";
    starting_balance?: number | null;
    symbols?: string[];
    excluded_days?: string[];
    use_default_exclusions?: boolean;
    min_minutes_per_day?: number;
    carry_pause?: boolean;
    allow_suspect_tz?: boolean;
    /** Per-run simulation knobs applied to the frozen config (null = the base doc's). */
    sim?: { slippage_pct?: number | null; brokerage_per_order?: number | null } | null;
    /** Persist the per-minute decision trace (algo_backtest_decisions). */
    record_decisions?: boolean;
  };
  /** Line up behind an active run instead of 409ing (scenario suites). */
  queue?: boolean;
  /** Provenance of per-run overrides (display only). */
  overrides?: { preset?: string; list: string[] } | null;
}

export interface BacktestPreflightDay {
  trade_date: string;
  symbol: string;
  expiry: string | null;
  minutes: number;
  spotless: boolean;
  planned: boolean;
  skip_reason: string;
  expected_minutes?: number;
  coverage_pct?: number;
}

export interface BacktestPreflight {
  from_date: string;
  to_date: string;
  tz_suspect_days: Record<string, string[]>;
  live_days_dup_risk: Record<string, string[]>;
  default_exclusions: Record<string, string[]>;
  days: BacktestPreflightDay[];
  planned: number;
  skipped: number;
  warnings: string[];
}

export type BacktestStatus = "queued" | "running" | "done" | "error" | "cancelled";

export interface BacktestRunBase {
  id: number;
  label: string;
  created_by: string;
  created_at: string;
  from_date: string;
  to_date: string;
  config_version: number | null;
  settings: Record<string, unknown>;
  status: BacktestStatus;
  days_total: number;
  days_done: number;
  cursor_date: string | null;
  started_at: string | null;
  finished_at: string | null;
  error: string;
}

export interface BacktestRunListItem extends BacktestRunBase {
  net_pnl: number | null;
  trades: number | null;
}

export interface BacktestSummary {
  trades: number;
  wins: number;
  losses: number;
  win_rate: number | null;
  gross_win: number;
  gross_loss: number;
  net_pnl: number;
  fees_total: number;
  avg_win: number | null;
  avg_loss: number | null;
  profit_factor: number | null;
  by_date: PnlGroupRow[];
  by_day: PnlGroupRow[];
  by_zone: PnlGroupRow[];
  starting_balance: number;
  final_equity: number;
  max_drawdown: number;
  max_drawdown_pct: number;
  max_drawdown_from: string | null;
  max_drawdown_to: string | null;
  equity: { date: string; equity: number; day_pnl: number }[];
  days_done: number;
  days_skipped: number;
  forced_eod_closes: number;
  spotless_days: number;
  preflight?: BacktestPreflight;
  /** Entry-funnel aggregate: where direction → hunt → trigger → entry died. */
  funnel?: {
    direction_minutes: number;
    hunts_started: number;
    hunts_discarded: number;
    hold_minutes: number;
    hunt_minutes: number;
    trigger_armed_minutes: number;
    band_blocked_minutes: number;
    entries: number;
    days_counted: number;
    days_with_direction: number;
    avg_hunt_life_min: number | null;
  } | null;
}

export interface BacktestRun extends BacktestRunBase {
  summary: BacktestSummary | null;
}

export interface BacktestDayRow {
  trade_date: string;
  status: "planned" | "skipped" | "done" | "error";
  skip_reason: string;
  detail: {
    symbol?: string;
    expiry?: string | null;
    minutes?: number;
    spotless?: boolean;
    forced_eod_close?: boolean;
    trades?: number;
    net?: number;
    fees?: number;
    gross?: number;
    equity_after?: number;
    zones?: Record<string, number>;
    paused_reason?: string;
    funnel?: Record<string, number>;
    gaps?: [string, string, number][];
    minutes_per_day?: number;
  } | null;
}

export interface BacktestEquity {
  mode: "compounding" | "fixed_per_day";
  starting_balance: number | null;
  points: { date: string; equity: number | null; day_pnl: number }[];
}

export interface BacktestUmpCapture {
  events: { ts: string; kind: string; price: number; text: string }[];
  levels: { price: number; type: number; name: string }[];
  engine_trades: {
    entry_ts: string;
    entry_price: number;
    sub_scenario: string;
    base_level: number;
    exit_ts: string | null;
    exit_price: number | null;
    exit_reason: string;
  }[];
}

export interface BacktestTradeRow extends TradeRow {
  ump?: BacktestUmpCapture | null;
}

export interface BacktestDayBundle {
  run_id: number;
  trade_date: string;
  day: string;
  symbol: string | null;
  expiry: string | null;
  status: string;
  skip_reason: string;
  spotless: boolean;
  forced_eod_close: boolean;
  minutes: number | null;
  config_version: number | null;
  zones: {
    zone_id: ZoneId;
    start: string;
    end: string;
    premium_min: number;
    premium_max: number;
    max_trades: number;
    reeval_cadence: string;
    enabled_indicators: IndicatorKey[];
    zone_kill: boolean;
  }[];
  /** The date's own session length (385 from 2026-08-03, 375 before). */
  minutes_per_day?: number;
  /** ≥3-minute holes in the basket ticks, named before execution. */
  gaps?: [string, string, number][];
  /** Compact per-minute decision rows (full rows via backtestDecisionAt). */
  decisions_index?: DecisionIndexRow[];
  status_timeline: { ts: string; state: string; zone: string; direction: string }[];
  signals: {
    ts: string;
    zone_id: string;
    indicator: string;
    reading: string;
    payload: Record<string, unknown> | null;
  }[];
  alerts: { ts: string; key: string; text: string }[];
  trades: BacktestTradeRow[];
  equity: {
    day_start: number | null;
    day_end: number | null;
    points: { ts: string; trade_seq: number; equity: number }[];
  };
}

export interface SandboxEnvelope {
  config: AlgoConfigDoc;
  updated_by: string;
  updated_at: string;
}

export interface BrokerSnapshot {
  payload: {
    balance: unknown;
    positions: unknown[];
    orders: unknown[];
  };
  fetched_at: string;
}

/** True stored-data bounds, from GET /api/algo/backtest/coverage. */
export interface BacktestSymbolCoverage {
  first_day: string;
  last_day: string;
  usable_days: number;
  thin_days: number;
  weekend_days: number;
}

export interface BacktestCoverage {
  /** Computed server-side in IST — never derive "today" in the browser. */
  server_today_ist: string;
  last_trading_day: string | null;
  stats_refreshed_at: string | null;
  /** True when the newest stored day is behind the last completed session. */
  stale: boolean;
  bounds: { min: string | null; max: string | null };
  default_from: string | null;
  default_to: string | null;
  symbols: Record<string, BacktestSymbolCoverage>;
  error?: string;
}
