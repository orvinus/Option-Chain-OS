"""The Algo Config document — Pydantic models, Appendix-A seed, validation.

One document holds EVERYTHING the trading engine may consult at runtime
(spec §16 scopes): global settings (1×), per-day settings (5×, Mon–Fri) and
per-zone settings (15×, day × Z1/Z2/Z3) including each zone's parameters for
all three entry-filter indicators and the full 5-group NIFTY Ultra Master Pro
input set. The document is saved atomically as one JSONB row per version
(``algo_config_versions``) — there is no way to persist half a zone.

Two validation layers share the same functions here:

- ``validate_document()``  — §15 pre-save checklist. Hard errors block the save
  (zone overlap, start ≥ end, premium min ≥ max, non-positive risk numbers);
  soft warnings are returned but do not block (e.g. a zone with zero enabled
  indicators — legal to SAVE, but that zone can never produce a direction).
- ``zone_completeness()``  — §14 runtime safety gate. Called by the orchestrator
  at EVERY evaluation; any returned problem means the zone is treated as fully
  disabled for the session (no filter evaluation, no UMP computation, no orders
  live or paper) and the block is alerted, never silent.

Deliberate non-validation (§5.2): NIFTY Ultra Master Pro numeric fields accept
any value from 0 upward — no ceilings/floors are baked in beyond "a number".
An extreme value is a trading decision, not a config error.

Kill-switch polarity: every ``*_kill`` flag is True = KILLED (matches the spec's
language "manually disables"). ``strategy_active`` is the opposite sense — True
means UMP is permitted to execute in that zone (§2.1).
"""
from __future__ import annotations

import re
from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

Weekday = Literal["monday", "tuesday", "wednesday", "thursday", "friday"]
ZoneId = Literal["Z1", "Z2", "Z3"]
IndicatorKey = Literal["oi_change", "multi_tf", "ratio"]
Side3 = Literal["Call", "Put", "Ignore"]
# Signal Console F1 §1.3–§1.5 — how the enabled indicators' readings become one
# decision, and what an enabled-but-undecided indicator means while doing it.
CombineRule = Literal["unanimous", "majority"]
NeutralMode = Literal["block", "abstain"]
# Signal Console F5 §5.3 — which candle the red-candle rule reads.
CandleRead = Literal["closed", "forming"]

WEEKDAYS: tuple[Weekday, ...] = ("monday", "tuesday", "wednesday", "thursday", "friday")
ZONE_IDS: tuple[ZoneId, ...] = ("Z1", "Z2", "Z3")

# The exchange window every zone must fall inside (§2.2 — 09:15 with the small
# reference-dashboard buffer to 15:45).
EXCHANGE_OPEN_MIN = 9 * 60 + 15
EXCHANGE_CLOSE_MIN = 15 * 60 + 45


def _hhmm_to_min(hhmm: str) -> int:
    """'HH:MM' → minutes since midnight. Raises ValueError on junk (callers treat
    that as a validation error, not a crash)."""
    parts = hhmm.split(":")
    if len(parts) != 2:
        raise ValueError(f"bad time {hhmm!r}")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"bad time {hhmm!r}")
    return h * 60 + m


# ══════════════════════════════════════════════════════════════════════════
# Entry-filter indicator №1 — OI Structure Engine ("OI Change" slot)
# Defaults from oi_structure_engine_spec.pdf §13 (Full Settings Reference).
# ══════════════════════════════════════════════════════════════════════════

class OIStructureMatrix(BaseModel):
    """Interpretation Matrix — the ONLY place a 1-minute event acquires a
    trading side (spec §7). 6 events × 2 charts = 12 independent routes."""
    call: dict[str, Side3] = Field(
        default_factory=lambda: {
            "HH": "Call", "HL": "Call", "LH": "Put", "LL": "Put",
            "BREAKOUT_UP": "Call", "BREAKOUT_DOWN": "Put",
        }
    )
    put: dict[str, Side3] = Field(
        default_factory=lambda: {
            "HH": "Put", "HL": "Put", "LH": "Call", "LL": "Call",
            "BREAKOUT_UP": "Put", "BREAKOUT_DOWN": "Call",
        }
    )


class OIStructureParams(BaseModel):
    # Swing detection & range zone (1-minute streams only)
    left_bars: int = 3
    right_bars: int = 3
    min_swing_move_cr: float = 0.03
    eq_tolerance_cr: float = 0.015
    breakout_buffer_cr: float = 0.02
    # Scoring weights
    swing_weight: float = 15.0
    breakout_weight: float = 30.0
    sweep_weight: float = 10.0
    red_candle_weight: float = 20.0
    agreement_bonus: float = 10.0
    confidence_threshold: float = 55.0
    # Engine master switches (scoring-level kills, spec §9)
    engine_1m_on: bool = True
    engine_5m_on: bool = True
    # ── Signal Console F4 (Swing Scoring Lookback) ──────────────────────────
    # Only swings confirmed at or after (current bar − N) contribute POINTS.
    # 0 = the whole session, which is the original engine's behaviour and is
    # therefore the model default: re-parsing any older stored document, or a
    # frozen backtest run, can never silently change what it did.
    # §4.8 is load-bearing — the range zone and the breakout test keep reading
    # the COMPLETE structure. Lookback filters scoring inputs, nothing else.
    swing_scoring_lookback: int = Field(default=0, ge=0, le=375)
    # ── Signal Console F5 (Candle Read) ─────────────────────────────────────
    # How many 1-minute bars fold into one candle for the red-candle rule.
    candle_size_bars: int = Field(default=5, ge=2, le=60)
    # "closed"  = the most recent candle whose period has completely finished
    # "forming" = the candle still being built (its close can still move)
    candle_read: CandleRead = "closed"
    # Input series scope: strikes summed into the Call/Put OI-change streams.
    # ATM ± N; -1 = the full stored chain (matches the reference header's
    # "STRIKES ATM ± 5/10/All" selector).
    strikes_atm_window: int = 10
    matrix: OIStructureMatrix = Field(default_factory=OIStructureMatrix)


# ══════════════════════════════════════════════════════════════════════════
# Entry-filter indicator №2 — MTF Ratio rule engine ("Multi-TF" slot)
# Semantics from MTF_Ratio_Master_Guide.pdf + the reference dashboard JS.
# ══════════════════════════════════════════════════════════════════════════

class MtfCondition(BaseModel):
    """One AND-ed row of a rule: Timeframe | Lowest-OI Side | Operator |
    Threshold | Call Sign | Put Sign. 'Any' disables that facet of the check.

    ``side`` is matched against the row's LOWEST-OI side (the signed dominant-
    side rule) since 2026-09-13; it previously matched the magnitude-based Ratio
    Side. The field name stays generic on purpose, so every stored config and
    every frozen backtest run keeps loading unchanged."""
    timeframe: str = "1m"                    # one of MtfRatioParams.timeframes
    side: Literal["Any", "Call", "Put"] = "Any"
    operator: Literal["Any", "Above", "Below"] = "Any"
    threshold: str = "Any"                   # "Any" or "1:N" e.g. "1:2"
    call_sign: Literal["Any", "Positive", "Negative"] = "Any"
    put_sign: Literal["Any", "Positive", "Negative"] = "Any"


class MtfRule(BaseModel):
    name: str = "New Rule"
    on: bool = True
    out: Literal["Call", "Put", "Neutral"] = "Call"
    conditions: list[MtfCondition] = Field(default_factory=list)


def _default_mtf_rules() -> list[MtfRule]:
    """The three seeded interpretations from the reference dashboard."""
    return [
        MtfRule(
            name="Massive Call Capitulation (Above 1:2)", on=True, out="Call",
            conditions=[MtfCondition(
                timeframe="1m", side="Call", operator="Above", threshold="1:2",
                call_sign="Negative", put_sign="Negative",
            )],
        ),
        MtfRule(
            name="Tight Consolidation (Below 1:2)", on=True, out="Put",
            conditions=[MtfCondition(
                timeframe="5m", side="Any", operator="Below", threshold="1:2",
            )],
        ),
        MtfRule(
            name="Pure Direction Bias (Any Threshold)", on=True, out="Call",
            conditions=[MtfCondition(
                timeframe="15m", side="Call", operator="Any", threshold="Any",
                call_sign="Positive",
            )],
        ),
    ]


class MtfRatioParams(BaseModel):
    # Which timeframe rows the engine computes (each = Call/Put OI Δ over that
    # trailing window; "full_day" = since session open).
    # Every timeframe the trailing-Δ engine can compute (user request: "add
    # all timeframes"). Rows compute in this order; the rule builder offers
    # the same set.
    timeframes: list[str] = Field(
        default_factory=lambda: [
            "1m", "3m", "5m", "10m", "15m", "30m", "1h", "2h", "3h", "full_day",
        ]
    )
    # ATM ± N basket for the OI Δ sums; -1 = full chain.
    strikes_atm_window: int = -1
    rules: list[MtfRule] = Field(default_factory=_default_mtf_rules)


# ══════════════════════════════════════════════════════════════════════════
# Entry-filter indicator №3 — Master Quantitative Action Engine ("Ratio" slot)
# Defaults from the MQAE PDS §6–§10 / the reference HTML source.
# ══════════════════════════════════════════════════════════════════════════

class MqaeParams(BaseModel):
    risk_mode: Literal["aggressive", "conservative"] = "aggressive"
    # ATM ± N basket for the PCR/Ratio lines; -1 = the full stored chain
    # (matches the Ratio tab's default scope).
    strikes_atm_window: int = -1
    # Auto-adjusts on mode switch (3 aggressive / 6 conservative) but remains
    # directly editable, exactly like the reference dashboard.
    master_threshold: int = 3
    # Model 1 — Kinetic Velocity
    velocity_on: bool = True
    velocity_period: int = 8
    velocity_surge_threshold: float = 0.012
    # Model 2 — Order Blocks
    order_blocks_on: bool = True
    ob_impulse_trigger: float = 0.02
    ob_zone_width: float = 0.01
    # Model 3 — SMC Structure
    smc_on: bool = True
    smc_swing_bars: int = 3
    smc_min_move: float = 0.025
    # Model 4 — Macro Trend Rider (fixed ±1 weight in both modes)
    trend_rider_on: bool = True
    trail_period: int = 15
    trail_buffer: float = 0.04
    # Model 5 — Crossover Momentum
    crossover_on: bool = True
    # The candle the five models run on (2026-09-15, user request): the Ratio
    # panel's timeframe pills write this. "1m" is the original behaviour;
    # 5m…30m = the line's value at each CLOSED session-anchored bucket (09:15
    # anchor); "full_day" = the cumulative-change lines since 09:15 at 1-minute
    # resolution (what the chart's Full Day mode draws). Every period above
    # (velocity_period, smc_swing_bars, trail_period) counts candles of THIS
    # timeframe. Applied identically by live trading, backtests, the panel and
    # the live strip via ``series.ratio_pair_for_timeframe``.
    timeframe: Literal["1m", "5m", "10m", "15m", "30m", "full_day"] = "1m"


# ══════════════════════════════════════════════════════════════════════════
# Execution engine — NIFTY Ultra Master Pro (5 input groups, per zone)
# Field-for-field mirror of the Pine v24 Inputs tab. No min/max validation
# beyond type (§5.2 — deliberately).
# ══════════════════════════════════════════════════════════════════════════

class UmpInstitutionalLaws(BaseModel):
    expansion_law_pct: float = 20.0
    bridge_law_pct: float = 50.0
    median_law_pct: float = 50.0
    price_floor_inr: float = 50.0


class UmpStructuralDetection(BaseModel):
    body_match_pts: float = 2.2
    scan_depth_bars: int = 250


class UmpVisual(BaseModel):
    line_extension_bars: int = 500
    label_right_offset_bars: int = 30
    show_struct: bool = True
    show_discovery: bool = True
    show_bridge: bool = True
    show_median: bool = True
    median_color: str = "#FFD700"
    median_line_width: int = 1
    zone_width_pct: float = 3.0


# Every UMP entry kind (Pine's sub-scenario labels). Order = display order.
UMP_SCENARIOS: tuple[str, ...] = (
    "S1A", "S1B", "S1C", "S2A", "S2B", "S3A", "S3B", "S3C", "R1", "R2",
)


class UmpEntryModel(BaseModel):
    show_entry_signals: bool = True
    enable_long: bool = True
    enable_retest: bool = True
    show_sl_lines: bool = True
    show_trail_labels: bool = True
    max_sl_pct: float = 10.0
    trigger_timeout_bars: int = 6
    # Trade-entry candle timeframe (2026-09-09). The key-level ladder
    # (daily/weekly/1H structure) is untouched; only the candle the trigger /
    # test-candle / retest logic evaluates changes. 15 is the only other
    # value because NSE 15-minute bars align on the 09:15 clock exactly like
    # 5-minute ones (``minute // tf * tf``), so no session-anchoring is
    # needed. Older stored documents lack the key and re-validate to 5 — the
    # behaviour they were saved under. ``trigger_timeout_bars`` counts bars
    # OF THIS timeframe (Pine counts chart bars).
    entry_timeframe_min: Literal[5, 15] = 5
    # Per-entry-kind switches under the two master switches (``enable_long``
    # gates every S*, ``enable_retest`` every R*). A disabled kind is refused
    # at the single commit point (``UmpEngine._enter``): the elif ladder that
    # picked it does NOT fall through to the next kind on that tick — the
    # trigger stays armed for a later tick, exactly as if the entry guard had
    # declined. Missing keys (older documents) mean ON.
    scenarios: dict[str, bool] = Field(
        default_factory=lambda: {k: True for k in UMP_SCENARIOS}
    )

    @model_validator(mode="after")
    def _fill_scenarios(self) -> "UmpEntryModel":
        unknown = sorted(k for k in self.scenarios if k not in UMP_SCENARIOS)
        if unknown:
            raise ValueError(f"unknown UMP entry scenario(s): {', '.join(unknown)}")
        for k in UMP_SCENARIOS:
            self.scenarios.setdefault(k, True)
        return self

    def scenario_enabled(self, sub: str) -> bool:
        return bool(self.scenarios.get(sub, True))


class UmpDashboard(BaseModel):
    show_dashboard: bool = True
    show_key_levels: bool = True
    # Exactly Pine's option lists (dashboard offers Middle Right, the levels
    # table Middle Left) — an off-list value must fail the save, as TV would.
    dashboard_position: Literal[
        "Top Right", "Top Left", "Bottom Right", "Bottom Left", "Middle Right"
    ] = "Top Right"
    levels_position: Literal[
        "Top Right", "Top Left", "Bottom Right", "Bottom Left", "Middle Left"
    ] = "Bottom Right"


class UmpParams(BaseModel):
    institutional: UmpInstitutionalLaws = Field(default_factory=UmpInstitutionalLaws)
    structural: UmpStructuralDetection = Field(default_factory=UmpStructuralDetection)
    visual: UmpVisual = Field(default_factory=UmpVisual)
    entry: UmpEntryModel = Field(default_factory=UmpEntryModel)
    dashboard: UmpDashboard = Field(default_factory=UmpDashboard)


# ══════════════════════════════════════════════════════════════════════════
# Zone / day / global scopes
# ══════════════════════════════════════════════════════════════════════════

class ZoneConfig(BaseModel):
    start: str = "09:20"          # HH:MM IST
    end: str = "10:30"
    premium_min: float = 75.0
    premium_max: float = 125.0
    # Multi-strike hunting (user decision 2026-08-18): the N in-band strikes
    # nearest the band midpoint EACH get their own Ultra Master Pro engine
    # hunting in parallel; the first valid entry wins and the rest are
    # discarded (still one position at a time).
    #
    # MODEL default = 1 (the original single-strike behaviour, byte-identical)
    # so that re-parsing any OLDER stored document — the live config before it
    # is explicitly upgraded, or a frozen backtest run's config — can never
    # silently change what it did. The Appendix-A seed ships 3 (the intended
    # multi-strike default) and the live config gets 3 via a normal AUDITED
    # save, so the change is versioned, visible and reversible.
    # Upper bound = the most strikes one side of a chain can ever be COLLECTED
    # with (data_strike_window ±60 → 121). It was a hard 10 (raised
    # 2026-09-23 at the user's request); any value above what the day
    # actually collects simply means "hunt every in-band strike", and
    # validate_document warns about it.
    strike_scan_count: int = Field(default=1, ge=1, le=121)
    max_trades: int = 2
    zone_kill: bool = False       # True = manually disabled for the day (§2.1)
    strategy_active: bool = True  # UMP permitted to execute here (§2.1)
    # §4.2 — re-check the combination every candle close, or once at zone start.
    reeval_cadence: Literal["every_candle", "zone_start"] = "every_candle"
    # EXPERIMENTAL relaxation of §5.3 (user-approved 2026-08-15): once a hunt
    # starts, keep it alive for this many minutes even while the combined
    # signal flickers to NO_TRADE — an OPPOSITE side still discards instantly.
    # 0 (default) = exact spec behavior. With every_candle cadence the
    # unanimous signal flickers every few minutes while the UMP engine needs
    # a hunt to survive several 5-minute candles; this knob makes that
    # dynamic testable without abandoning the unanimity rule for entries.
    direction_hold_min: int = Field(default=0, ge=0, le=375)
    enabled_indicators: list[IndicatorKey] = Field(
        default_factory=lambda: ["oi_change", "multi_tf", "ratio"]
    )
    # ── Signal Console F1 (Combination Rule and Neutral Handling) ───────────
    # A DISABLED indicator is simply absent from enabled_indicators above — it
    # is never evaluated and never votes (spec §1.6, "OFF before NEUTRAL").
    # These two settings govern only the ENABLED ones.
    #   unanimous — every enabled indicator with a direction must agree (§1.4)
    #   majority  — the side with more votes wins; a tie is NO_TRADE (§1.3)
    # BOTH model defaults reproduce today's hard-coded behaviour exactly, so
    # no saved config and no frozen backtest changes meaning on deploy.
    combine_rule: CombineRule = "unanimous"
    #   block   — an enabled indicator reading NO_TRADE forces NO_TRADE (§1.5)
    #   abstain — it is ignored and the remaining directional readings decide
    neutral_mode: NeutralMode = "block"
    oi_structure: OIStructureParams = Field(default_factory=OIStructureParams)
    mtf_ratio: MtfRatioParams = Field(default_factory=MtfRatioParams)
    mqae: MqaeParams = Field(default_factory=MqaeParams)
    ump: UmpParams = Field(default_factory=UmpParams)


class DayAlertToggles(BaseModel):
    """Telegram is the platform's only alert transport (2026-09-08: the
    email/SMS/push toggles were removed — they never had a transport). The
    single toggle gates trade entry/exit, kill and daily-summary messages.
    Max-loss breach stays always-on. Old documents carrying the removed keys
    still load (Pydantic ignores extras)."""
    telegram_trade_entry_exit: bool = True


class DayConfig(BaseModel):
    index_symbol: str = "NIFTY"
    day_kill: bool = False
    # ATM ± N strikes of live DATA the feed collects for this day's index
    # (drives the TrueData subscription window; applied on the next
    # re-subscribe / ATM re-centre, checked every minute). The vendor's trial
    # cap is ~50 instruments, so beyond ±12 the feed symmetrically truncates
    # the wings — §15 warns.
    data_strike_window: int = Field(default=11, ge=1, le=60)
    # Capital & risk (§7.2) — percentages of that day's ALLOCATED capital.
    all_in: bool = False
    allocation_pct: float = 50.0
    max_loss_pct: float = 20.0
    max_profit_lock_pct: float = 30.0
    max_consec_losses: int = 3
    max_consec_loss_pct: float = 25.0
    # §2.3 — applies ONLY to whichever zone is last for this day.
    end_exit_enabled: bool = True
    alerts: DayAlertToggles = Field(default_factory=DayAlertToggles)
    zones: dict[ZoneId, ZoneConfig] = Field(default_factory=dict)


class FeeConfig(BaseModel):
    """Editable fee model, seeded with Lakshmishree Broking's real F&O rate
    (₹17 flat per executed order) + the statutory NSE options charges. Used by
    the paper simulator AND live P&L, so both speak the same costs."""
    brokerage_per_order: float = 17.0
    stt_sell_premium_pct: float = 0.1        # STT on sell-side premium
    exchange_txn_pct: float = 0.03503        # NSE options transaction charge
    sebi_turnover_pct: float = 0.0001        # ₹10 / crore
    ipft_pct: float = 0.0005                 # NSE IPFT ₹0.50 / lakh premium
    gst_pct: float = 18.0                    # on brokerage + txn + SEBI
    stamp_duty_buy_pct: float = 0.003        # buy-side premium


class PaperConfig(BaseModel):
    """§8 — Paper Trading. Identical zone/indicator/risk configuration as live
    by construction (it reads the same document); only routing differs."""
    paper_mode: bool = True                   # True = orders go to the simulator
    virtual_balance: float = 30000.0
    session_started: str = ""                 # ISO date; set on seed/reset
    slippage_pct: float = 0.5                 # applied unfavourably to every fill
    latency_ms: int = Field(default=250, ge=0)   # live paper fills wait this long
    # The Literal keeps old documents loading; the after-validator folds the
    # dead 'bid_ask_mid' option into 'ltp' (the feed stores LTP only) and
    # validate_document reports the normalisation as a warning.
    fill_source: Literal["ltp", "bid_ask_mid"] = "ltp"
    shadow_mode: bool = False                 # mirror live signals into paper
    fill_source_normalised: bool = Field(default=False, exclude=True)

    @model_validator(mode="after")
    def _normalise_fill_source(self) -> "PaperConfig":
        if self.fill_source == "bid_ask_mid":
            self.fill_source = "ltp"
            self.fill_source_normalised = True
        return self


# Runtime cap on the simulated paper latency (orchestrator._latency_wait).
PAPER_LATENCY_CAP_MS = 2000


class HolidayEntry(BaseModel):
    date: str                                  # YYYY-MM-DD
    occasion: str = ""


class ConfigLock(BaseModel):
    """Accidental-edit protection for the config document itself.

    ``lock_market_hours``: while ON, saves are refused during the NSE session
    (holidays excluded) — EXCEPT saves that touch only this section, so the
    lock can always be turned off.
    ``require_save_confirm``: while ON (default) the UI shows the field diff
    and asks for confirmation before any save goes live; OFF posts directly.
    """
    lock_market_hours: bool = False
    require_save_confirm: bool = True


class GlobalConfig(BaseModel):
    # Master kill (engine-wide): True = no trade is evaluated or executed
    # regardless of any other setting.
    master_kill: bool = False
    # Locked user decision 2026-08-19: UMP positions carry OVERNIGHT exactly
    # like the Pine indicator — no end-of-day close; the exit ladder alone
    # closes them. The ONLY automatic day-close is the unconditional
    # expiry-day 15:25 IST force-close (the contract ceases to exist).
    # GLOBAL, not per-day: a carried position spans weekday configs, so a
    # per-day flag would have no unambiguous owner. While True, every
    # end_exit_enabled toggle is IGNORED (§2.3 deliberately overridden).
    # Default True flips live behavior on deploy BY DESIGN — the §15 warning
    # makes it loud; frozen backtest configs are guarded by raw-JSON key
    # absence in the runner, so old runs stay byte-identical.
    overnight_carry: bool = True
    # §7.1 — manually-entered placeholder until the broker API supplies it live.
    demat_balance: float = 30000.0
    paper: PaperConfig = Field(default_factory=PaperConfig)
    fees: FeeConfig = Field(default_factory=FeeConfig)
    holidays: list[HolidayEntry] = Field(default_factory=list)
    config_lock: ConfigLock = Field(default_factory=ConfigLock)


class AlgoConfig(BaseModel):
    """The complete document. ``schema_rev`` guards future shape migrations."""
    schema_rev: int = 1
    global_: GlobalConfig = Field(default_factory=GlobalConfig, alias="global")
    days: dict[Weekday, DayConfig] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}

    def zone(self, day: Weekday, zone_id: ZoneId) -> Optional[ZoneConfig]:
        d = self.days.get(day)
        return d.zones.get(zone_id) if d else None

    def last_zone_id(self, day: Weekday) -> Optional[ZoneId]:
        """Whichever zone is last by End time — the only zone End-Exit can fire
        on (§2.3). Tracks 'whichever zone is last', not a fixed label."""
        d = self.days.get(day)
        if not d or not d.zones:
            return None
        try:
            return max(d.zones, key=lambda z: _hhmm_to_min(d.zones[z].end))
        except ValueError:
            return None


# ══════════════════════════════════════════════════════════════════════════
# Appendix-A seed — the exact reviewed reference configuration (§18)
# ══════════════════════════════════════════════════════════════════════════

# (start, end, prem_min, prem_max, max_trades, zone_kill) per §18.1
_A_ZONES: dict[Weekday, dict[ZoneId, tuple[str, str, float, float, int, bool]]] = {
    "monday": {
        "Z1": ("09:20", "10:30", 75, 125, 2, False),
        "Z2": ("11:45", "13:00", 60, 100, 2, False),
        "Z3": ("14:15", "15:25", 55, 90, 1, False),
    },
    "tuesday": {
        "Z1": ("09:20", "10:45", 80, 135, 2, False),
        "Z2": ("12:00", "13:15", 58, 98, 2, False),
        "Z3": ("14:10", "15:30", 52, 88, 1, False),
    },
    "wednesday": {
        "Z1": ("09:20", "10:15", 80, 130, 2, False),
        "Z2": ("11:30", "13:00", 55, 95, 2, True),   # "Killed (manual)" in §18.1
        "Z3": ("14:00", "15:20", 60, 105, 1, False),
    },
    "thursday": {
        "Z1": ("09:20", "10:20", 78, 128, 2, False),
        "Z2": ("11:40", "13:05", 53, 92, 2, False),
        "Z3": ("14:05", "15:15", 58, 100, 1, False),
    },
    "friday": {
        "Z1": ("09:20", "10:30", 80, 130, 2, False),
        "Z2": ("11:45", "13:00", 58, 98, 2, False),
        "Z3": ("14:10", "15:20", 55, 90, 1, False),
    },
}

# (all_in, alloc_pct, loss_pct, profit_pct, consec_losses, consec_loss_pct) per §18.2
_A_RISK: dict[Weekday, tuple[bool, float, float, float, int, float]] = {
    "monday": (False, 50, 20, 30, 3, 25),
    "tuesday": (True, 100, 20, 30, 3, 25),
    "wednesday": (False, 40, 15, 25, 2, 20),
    "thursday": (False, 50, 20, 30, 3, 25),
    "friday": (False, 25, 20, 30, 2, 20),
}

# UMP per-zone tier (§18.3 base + the reviewed ultra-dashboard per-zone data):
# Z1/Z2/Z3 escalate expansion/width/SL/timeout together.
_A_UMP_TIER: dict[ZoneId, dict[str, float | int | str]] = {
    "Z1": {"explaw": 18, "bridgelaw": 45, "medianlaw": 45, "floor": 40,
           "bodymatch": 1.8, "scandepth": 200, "lineext": 400, "labeloff": 20,
           "zonewidth": 2, "maxsl": 8, "timeout": 4,
           "mediancolor": "#eab308", "medianwidth": 1},
    "Z2": {"explaw": 20, "bridgelaw": 50, "medianlaw": 50, "floor": 50,
           "bodymatch": 2.2, "scandepth": 250, "lineext": 500, "labeloff": 30,
           "zonewidth": 3, "maxsl": 10, "timeout": 6,
           # Z2 is the byte-exact Pine-default tier — keep Pine's gold.
           "mediancolor": "#FFD700", "medianwidth": 1},
    "Z3": {"explaw": 24, "bridgelaw": 55, "medianlaw": 55, "floor": 60,
           "bodymatch": 2.6, "scandepth": 300, "lineext": 600, "labeloff": 40,
           "zonewidth": 4, "maxsl": 12, "timeout": 8,
           "mediancolor": "#facc15", "medianwidth": 2},
}

# RETEST toggled off on alternating zones (§18.3) — exact per-zone pattern from
# the reviewed dashboard's zone data.
_A_RETEST: dict[Weekday, dict[ZoneId, bool]] = {
    "monday": {"Z1": True, "Z2": False, "Z3": True},
    "tuesday": {"Z1": False, "Z2": True, "Z3": False},
    "wednesday": {"Z1": True, "Z2": False, "Z3": True},
    "thursday": {"Z1": False, "Z2": True, "Z3": False},
    "friday": {"Z1": True, "Z2": False, "Z3": True},
}

_A_DAY_KILL: dict[Weekday, bool] = {
    "monday": False, "tuesday": False, "wednesday": False, "thursday": False,
    "friday": True,   # "Day Off" in §18.1
}

# Friday's End-Exit shows "Off (last zone)" in §18.1; all other days On.
_A_END_EXIT: dict[Weekday, bool] = {
    "monday": True, "tuesday": True, "wednesday": True, "thursday": True,
    "friday": False,
}


def default_config(today: Optional[date] = None) -> AlgoConfig:
    """Build the Appendix-A reference document — the engine's out-of-the-box
    behaviour must match what was already reviewed in the dashboard mockups."""
    days: dict[Weekday, DayConfig] = {}
    for day in WEEKDAYS:
        all_in, alloc, loss, profit, cl, clp = _A_RISK[day]
        zones: dict[ZoneId, ZoneConfig] = {}
        for zid in ZONE_IDS:
            start, end, pmin, pmax, mtr, zkill = _A_ZONES[day][zid]
            tier = _A_UMP_TIER[zid]
            ump = UmpParams(
                institutional=UmpInstitutionalLaws(
                    expansion_law_pct=float(tier["explaw"]),
                    bridge_law_pct=float(tier["bridgelaw"]),
                    median_law_pct=float(tier["medianlaw"]),
                    price_floor_inr=float(tier["floor"]),
                ),
                structural=UmpStructuralDetection(
                    body_match_pts=float(tier["bodymatch"]),
                    scan_depth_bars=int(tier["scandepth"]),
                ),
                visual=UmpVisual(
                    line_extension_bars=int(tier["lineext"]),
                    label_right_offset_bars=int(tier["labeloff"]),
                    median_color=str(tier["mediancolor"]),
                    median_line_width=int(tier["medianwidth"]),
                    zone_width_pct=float(tier["zonewidth"]),
                ),
                entry=UmpEntryModel(
                    enable_retest=_A_RETEST[day][zid],
                    max_sl_pct=float(tier["maxsl"]),
                    trigger_timeout_bars=int(tier["timeout"]),
                ),
            )
            zones[zid] = ZoneConfig(
                start=start, end=end,
                premium_min=float(pmin), premium_max=float(pmax),
                max_trades=mtr, zone_kill=zkill,
                # The seed ships multi-strike hunting ON (top 3) — the model
                # default stays 1 so OLD stored documents keep their original
                # single-strike behaviour until explicitly upgraded.
                strike_scan_count=3,
                ump=ump,
            )
        days[day] = DayConfig(
            day_kill=_A_DAY_KILL[day],
            all_in=all_in, allocation_pct=float(alloc),
            max_loss_pct=float(loss), max_profit_lock_pct=float(profit),
            max_consec_losses=cl, max_consec_loss_pct=float(clp),
            end_exit_enabled=_A_END_EXIT[day],
            zones=zones,
        )

    paper = PaperConfig(session_started=(today or date.today()).isoformat())
    # Seed the trading holiday list from the platform's NSE holiday file so a
    # fresh config never trades a holiday the platform already knows about
    # (the runtime ALSO unions with the live file — this seed just makes the
    # list visible/editable in the Holidays panel from day one).
    holidays: list[HolidayEntry] = []
    try:
        from ..core.holidays import known_holidays

        for d in sorted(known_holidays()):
            try:
                date.fromisoformat(str(d))   # a malformed file entry must not
            except ValueError:               # seed an unsaveable document
                continue
            holidays.append(
                HolidayEntry(date=str(d), occasion="NSE holiday (platform calendar)")
            )
    except Exception:
        holidays = []
    return AlgoConfig(
        global_=GlobalConfig(paper=paper, holidays=holidays), days=days
    )


# ══════════════════════════════════════════════════════════════════════════
# Validation — §15 pre-save checklist / §14 runtime safety gate
# ══════════════════════════════════════════════════════════════════════════

def _zone_time_errors(day: Weekday, zones: dict[ZoneId, ZoneConfig]) -> list[str]:
    """start<end per zone, pairwise non-overlap, all inside 09:15–15:45 (§2.2)."""
    errors: list[str] = []
    spans: list[tuple[int, int, str]] = []
    for zid, z in zones.items():
        try:
            s, e = _hhmm_to_min(z.start), _hhmm_to_min(z.end)
        except ValueError:
            errors.append(f"{day} {zid}: invalid time format ({z.start!r}–{z.end!r})")
            continue
        if s >= e:
            errors.append(f"{day} {zid}: start {z.start} must be before end {z.end}")
            continue
        if s < EXCHANGE_OPEN_MIN or e > EXCHANGE_CLOSE_MIN:
            errors.append(f"{day} {zid}: {z.start}–{z.end} falls outside 09:15–15:45")
        spans.append((s, e, zid))
    spans.sort()
    for (s1, e1, z1), (s2, e2, z2) in zip(spans, spans[1:]):
        if s2 < e1:
            errors.append(f"{day}: zones {z1} and {z2} overlap")
    return errors


def zone_completeness(cfg: AlgoConfig, day: Weekday, zone_id: ZoneId) -> list[str]:
    """§14.1 — what must be present/valid before a zone may trade (live OR
    paper). Empty list = eligible. The orchestrator calls this at every
    evaluation; the UI shows the same output in the Validation sub-tab, so the
    two gates can never disagree."""
    problems: list[str] = []
    d = cfg.days.get(day)
    if d is None:
        return [f"{day}: day configuration missing entirely"]
    z = d.zones.get(zone_id)
    if z is None:
        return [f"{day} {zone_id}: zone configuration missing entirely"]

    # 1. Zone timing valid + no overlap with the day's other zones.
    problems.extend(_zone_time_errors(day, d.zones))

    # 2. Premium band: min strictly below max, both positive.
    if not (z.premium_min > 0 and z.premium_max > 0 and z.premium_min < z.premium_max):
        problems.append(
            f"{day} {zone_id}: premium band invalid "
            f"(min {z.premium_min} must be > 0 and < max {z.premium_max})"
        )

    # 3. Max trades: positive integer.
    if z.max_trades < 1:
        problems.append(f"{day} {zone_id}: max trades must be ≥ 1 (got {z.max_trades})")

    # 4. ≥1 entry-filter indicator enabled — a zone with none can never produce
    #    a direction (§4.1) and must be flagged, not silently No-Trade.
    if not z.enabled_indicators:
        problems.append(f"{day} {zone_id}: no entry-filter indicator enabled")

    # 5. That day's risk parameters valid (§14.1 item 8).
    if not d.all_in and not (0 < d.allocation_pct <= 100):
        problems.append(f"{day}: capital allocation % must be in (0, 100] (got {d.allocation_pct})")
    if d.max_loss_pct <= 0:
        problems.append(f"{day}: max loss per day % must be > 0")
    if d.max_profit_lock_pct <= 0:
        problems.append(f"{day}: max profit lock % must be > 0")
    if d.max_consec_losses < 1:
        problems.append(f"{day}: max consecutive losses must be ≥ 1")
    if d.max_consec_loss_pct <= 0:
        problems.append(f"{day}: max consecutive loss % must be > 0")

    # 6. UMP parameter groups are guaranteed present by the model; the only
    #    §14-relevant value checks are non-negativity (§5.2 forbids anything
    #    stricter).
    u = z.ump
    for label, val in (
        ("expansion law", u.institutional.expansion_law_pct),
        ("bridge law", u.institutional.bridge_law_pct),
        ("median law", u.institutional.median_law_pct),
        ("price floor", u.institutional.price_floor_inr),
        ("body match", u.structural.body_match_pts),
        ("zone width", u.visual.zone_width_pct),
        ("max SL %", u.entry.max_sl_pct),
    ):
        if val < 0:
            problems.append(f"{day} {zone_id}: Ultra Master Pro {label} is negative ({val})")
    if u.structural.scan_depth_bars < 1:
        problems.append(f"{day} {zone_id}: Ultra Master Pro scan depth must be ≥ 1")
    if u.entry.trigger_timeout_bars < 1:
        problems.append(f"{day} {zone_id}: Ultra Master Pro trigger timeout must be ≥ 1")

    return problems


def validate_document(
    cfg: AlgoConfig, *, broker_configured: Optional[bool] = None
) -> tuple[list[str], list[str]]:
    """§15 pre-save validation. Returns (errors, warnings): errors block the
    save; warnings are surfaced but a save is allowed (the runtime safety gate
    still keeps a warned zone from trading where it matters).

    ``broker_configured`` is the runtime fact "the Interactive API credentials
    are wired" — pass it where known (the API layer / config store) to enforce
    §15's "at least one broker connected before live trading". ``None`` skips
    that check (pure-config callers)."""
    errors: list[str] = []
    warnings: list[str] = []

    for day in WEEKDAYS:
        d = cfg.days.get(day)
        if d is None:
            errors.append(f"{day}: day configuration missing")
            continue
        missing_zones = [z for z in ZONE_IDS if z not in d.zones]
        if missing_zones:
            errors.append(f"{day}: zones missing: {', '.join(missing_zones)}")
        errors.extend(_zone_time_errors(day, d.zones))
        for zid, z in d.zones.items():
            if not (z.premium_min > 0 and z.premium_max > 0 and z.premium_min < z.premium_max):
                errors.append(
                    f"{day} {zid}: premium min ({z.premium_min}) must be > 0 and "
                    f"< premium max ({z.premium_max})"
                )
            if z.max_trades < 1:
                errors.append(f"{day} {zid}: max trades must be ≥ 1")
            if not z.enabled_indicators and not z.zone_kill:
                warnings.append(
                    f"{day} {zid}: no entry-filter indicator enabled — this zone "
                    "can never produce a direction (§4.1)"
                )
            # Signal Console F1 §1.3 — a majority needs an odd bench to break.
            if (
                z.combine_rule == "majority"
                and len(z.enabled_indicators) % 2 == 0
                and z.enabled_indicators
                and not z.zone_kill
            ):
                warnings.append(
                    f"{day} {zid}: majority rule with an even number of enabled "
                    f"indicators ({len(z.enabled_indicators)}) — a split vote ties "
                    "and ties are NO_TRADE"
                )
            if z.combine_rule == "majority" and z.neutral_mode == "abstain":
                warnings.append(
                    f"{day} {zid}: majority + neutral-abstain is the loosest "
                    "combination — a single directional indicator can decide the "
                    "zone while the others are neutral"
                )
            if z.direction_hold_min > 0:
                warnings.append(
                    f"{day} {zid}: EXPERIMENTAL direction-hold is ON "
                    f"({z.direction_hold_min} min) — hunts survive NO_TRADE "
                    "flickers, relaxing §5.3's instant discard"
                )
            if z.strike_scan_count > 5:
                warnings.append(
                    f"{day} {zid}: strike scan count {z.strike_scan_count} means "
                    f"{z.strike_scan_count} parallel engine warmups every time a "
                    "hunt starts — expect slower minute passes"
                )
            collected = 2 * d.data_strike_window + 1
            if z.strike_scan_count > collected:
                warnings.append(
                    f"{day} {zid}: strike scan count {z.strike_scan_count} exceeds "
                    f"the {collected} strikes collected per side (Data strikes "
                    f"ATM ±{d.data_strike_window}) — at most {collected} can hunt; "
                    "raise the data window to widen it"
                )
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", z.ump.visual.median_color):
                # Rendering-only field, so a malformed value must not block the
                # save — the chart falls back to Pine's #FFD700.
                warnings.append(
                    f"{day} {zid}: UMP median color "
                    f"{z.ump.visual.median_color!r} is not a #RRGGBB hex — the "
                    "chart will fall back to Pine's default #FFD700"
                )
            # Sizing consumes the whole allocation, so ONE trade can lose up
            # to ~max_sl_pct of allocated capital — if that exceeds the day's
            # max-loss cap, a single stop-out blows straight through §7.1.
            if not z.zone_kill and z.ump.entry.max_sl_pct > d.max_loss_pct:
                warnings.append(
                    f"{day} {zid}: UMP max SL ({z.ump.entry.max_sl_pct}% of "
                    f"premium) exceeds the day max-loss cap "
                    f"({d.max_loss_pct}% of allocated) — a single trade can "
                    "lose more than the whole day is allowed to"
                )
        if d.data_strike_window > 12:
            warnings.append(
                f"{day}: data strike window ATM ±{d.data_strike_window} wants "
                f"{d.data_strike_window * 4 + 1}+ instruments — beyond the "
                "TrueData trial cap (~50) the feed symmetrically truncates the "
                "outer wings"
            )
        if (
            not cfg.global_.overnight_carry
            and not d.end_exit_enabled
            and not d.day_kill
        ):
            # §14 item 4 companion: with End-Exit off there is NO end-of-day
            # close in live trading — a position that never hits an exit rides
            # into the next session. Legal to save; loud to see. (With
            # overnight carry ON this describes INTENDED behavior — gated off.)
            warnings.append(
                f"{day}: End-Exit is OFF — a live position that reaches the "
                "last zone's End time will NOT be force-closed (overnight risk)"
            )
        if not d.all_in and not (0 < d.allocation_pct <= 100):
            errors.append(f"{day}: capital allocation % must be in (0, 100]")
        for label, val in (
            ("max loss %", d.max_loss_pct),
            ("max profit lock %", d.max_profit_lock_pct),
            ("max consecutive loss %", d.max_consec_loss_pct),
        ):
            if val <= 0:
                errors.append(f"{day}: {label} must be > 0")
        if d.max_consec_losses < 1:
            errors.append(f"{day}: max consecutive losses must be ≥ 1")

    g = cfg.global_
    if g.overnight_carry:
        warnings.append(
            "global: OVERNIGHT CARRY is ON (Pine parity) — positions ride the "
            "overnight gap un-stopped (the exit ladder only runs 09:15–15:45); "
            "broker orders are NRML (the premium stays blocked overnight) and "
            "the ONLY automatic close is the expiry-day 15:25 IST force-close"
        )
        ee_days = [
            day for day, d in cfg.days.items()
            if d.end_exit_enabled and not d.day_kill
        ]
        if ee_days:
            warnings.append(
                f"global: End-Exit is configured on {len(ee_days)} day(s) but "
                "IGNORED while overnight carry is ON (§2.3 override — the "
                "Pine exit ladder alone closes positions)"
            )
    if g.demat_balance <= 0:
        # §15: demat balance set and > 0 before a LIVE save; paper works
        # without it — warn there, block only when Paper Mode is actually off.
        if g.paper.paper_mode:
            warnings.append(
                "global: demat balance is not set (> 0 required before live trading)"
            )
        else:
            errors.append(
                "global: Paper Mode is OFF but the demat balance is not set — "
                "live sizing needs a real balance"
            )
    if not g.paper.paper_mode and broker_configured is False:
        errors.append(
            "global: Paper Mode is OFF but no broker is connected (§15) — "
            "configure the Lakshmishree Interactive API or turn Paper Mode back on"
        )
    if g.paper.virtual_balance <= 0:
        errors.append("global: paper virtual balance must be > 0")
    if g.paper.fill_source_normalised or g.paper.fill_source == "bid_ask_mid":
        warnings.append(
            "global: paper fill source normalised to LTP (the feed stores LTP "
            "only) — 'bid/ask mid' is not available"
        )
    if g.paper.latency_ms > PAPER_LATENCY_CAP_MS:
        warnings.append(
            f"global: paper latency {g.paper.latency_ms} ms exceeds the "
            f"simulator cap — runtime clamps to {PAPER_LATENCY_CAP_MS} ms"
        )
    for h in g.holidays:
        try:
            date.fromisoformat(h.date)
        except ValueError:
            errors.append(f"holiday {h.date!r}: not a valid YYYY-MM-DD date")

    for f_label, f_val in (
        ("brokerage per order", g.fees.brokerage_per_order),
        ("STT %", g.fees.stt_sell_premium_pct),
        ("exchange txn %", g.fees.exchange_txn_pct),
        ("GST %", g.fees.gst_pct),
        ("stamp duty %", g.fees.stamp_duty_buy_pct),
    ):
        if f_val < 0:
            errors.append(f"fees: {f_label} is negative")

    return errors, warnings
