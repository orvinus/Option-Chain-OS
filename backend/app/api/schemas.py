"""Pydantic response schemas for the REST API."""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str
    authenticated: bool
    latest_spot: float | None
    tokens_subscribed: int
    last_flush_at: str | None
    expiries: list[str]
    run_mode: str = Field(description="live | replay")
    now_ist: str = Field(description="Current server time in Asia/Kolkata (ISO-8601)")
    nse_session_open: bool = Field(
        description="True on a weekday between session_open_ist and session_close_ist "
        "(holidays not checked)"
    )
    session_open_ist: str = Field(
        default="09:15", description="Regular-session open, IST 'HH:MM' (configurable)",
    )
    session_close_ist: str = Field(
        default="15:40",
        description="Regular-session close, IST 'HH:MM' (configurable). The frontend "
        "reads this instead of hardcoding a close, so the two can never drift.",
    )
    feed_connected: bool = Field(
        default=False,
        description="XTS market-data WebSocket is connected (live mode only)",
    )
    active_symbol: str = Field(
        default="NIFTY",
        description="Underlying currently subscribed on the live feed; controlled via /api/active-symbol",
    )
    poller_enabled: bool = Field(
        default=False,
        description="Universe poller (all-symbol REST snapshotter) is running",
    )
    poller_last_sweep_at: str | None = Field(
        default=None, description="ISO-8601 time of the last completed poller sweep",
    )
    poller_last_ticks: int = Field(
        default=0, description="Ticks enqueued by the most recent poller sweep",
    )


class SpotResponse(BaseModel):
    symbol: str
    spot: float | None
    asof: str | None


class NiftyCrossCheckResponse(BaseModel):
    """Compare runtime spot (XTS index feed) to a public NIFTY 50 quote."""

    our_spot: float | None
    reference_last: float | None
    reference_source: str = Field(
        description="nseindia_allIndices | yahoo_nsei | none — where reference_last came from"
    )
    diff_points: float | None = Field(default=None, description="our_spot minus reference_last")
    aligned: bool | None = Field(
        default=None,
        description="True if both present and |diff| <= tolerance_points",
    )
    tolerance_points: float = 30.0
    pipeline_note: str = Field(
        default=(
            "Our spot is streamed from the Shrilakshmi/Symphony XTS market-data feed using "
            "the NSE NIFTY 50 index token (exchange feed via broker). It is not scraped from "
            "nseindia.com. This endpoint compares to NSE India’s public allIndices API when "
            "reachable, otherwise Yahoo ^NSEI (same underlying index)."
        )
    )
    fetch_detail: str | None = Field(
        default=None,
        description="Diagnostics when NSE is skipped or fallback is used",
    )


class ExpiriesResponse(BaseModel):
    expiries: list[str]


class OIChangeRowOut(BaseModel):
    strike: int
    call_oi: int
    put_oi: int
    call_oi_change: int
    put_oi_change: int
    call_ltp: float | None = None
    put_ltp: float | None = None
    call_ltp_change: float | None = None
    put_ltp_change: float | None = None


class OIChangeResponseOut(BaseModel):
    timeframe: str
    expiry: str
    spot: float | None
    asof: str = Field(
        description="Latest exchange timestamp on stored option rows (from feed). May lag wall clock after hours.",
    )
    computed_at: str = Field(
        description="IST timestamp when this snapshot was computed — advances on every push even if asof is stale.",
    )
    total_call_oi_change: int
    total_put_oi_change: int
    rows: list[OIChangeRowOut]


class OptionChainStrike(BaseModel):
    strike: int
    call_oi: int = 0
    put_oi: int = 0
    call_ltp: float | None = None
    put_ltp: float | None = None
    call_volume: int = 0
    put_volume: int = 0


class OptionChainResponse(BaseModel):
    expiry: str
    spot: float | None
    asof: str | None
    rows: list[OptionChainStrike]


class OptionChainFullStrikeOut(BaseModel):
    strike: int
    call_oi: int = 0
    put_oi: int = 0
    call_oi_change: int = 0
    put_oi_change: int = 0
    call_ltp: float | None = None
    put_ltp: float | None = None
    call_ltp_change: float | None = None
    put_ltp_change: float | None = None
    call_volume: int = 0
    put_volume: int = 0
    call_iv: float | None = None
    put_iv: float | None = None
    call_trend: str = ""
    put_trend: str = ""
    pcr_oi: float | None = None
    pcr_volume: float | None = None
    pe_ce_oi: int = 0
    pe_ce_oi_change: int = 0
    call_delta: float | None = None
    call_gamma: float | None = None
    call_theta: float | None = None
    call_vega: float | None = None
    put_delta: float | None = None
    put_gamma: float | None = None
    put_theta: float | None = None
    put_vega: float | None = None


class OptionChainFullResponseOut(BaseModel):
    timeframe: str
    expiry: str
    spot: float | None
    asof: str
    computed_at: str
    lot_size: int
    rows: list[OptionChainFullStrikeOut]
    synthetic_future: float | None = None
    atm_iv: float | None = None
    ivp: float | None = None


class IvScannerRowOut(BaseModel):
    symbol: str
    display: str = ""
    price: float | None = None
    price_chg: float | None = None
    price_chg_pct: float | None = None
    total_oi: int = 0
    total_oi_chg: int = 0
    total_oi_chg_pct: float | None = None
    pcr: float | None = None
    iv: float | None = None
    iv_chg_pct: float | None = None
    iv_range_1y_low: float | None = None
    iv_range_1y_high: float | None = None
    hv_10: float | None = None
    hv_20: float | None = None
    hv_30: float | None = None
    ivr: float | None = None
    ivp: float | None = None
    iv_hv10: float | None = None
    iv_hv20: float | None = None
    iv_hv30: float | None = None
    history_ready: bool = False


class IvScannerResponseOut(BaseModel):
    mode: str = Field(description="latest | historical")
    expiry_slot: str = Field(description="near | next | far")
    expiry: str | None = None
    asof: str
    max_symbols: int = 50
    rows: list[IvScannerRowOut]
    note: str | None = None


class ReplayRowOut(BaseModel):
    strike: int
    call_oi: int
    put_oi: int
    call_oi_change: int = 0
    put_oi_change: int = 0
    call_iv: float | None = None
    put_iv: float | None = None
    call_delta: float | None = None
    put_delta: float | None = None
    call_gamma: float | None = None
    put_gamma: float | None = None
    call_theta: float | None = None
    put_theta: float | None = None
    call_vega: float | None = None
    put_vega: float | None = None


class ReplayFrameOut(BaseModel):
    ts: str
    spot: float | None
    atm: int | None = None
    total_call_oi: int = 0
    total_put_oi: int = 0
    total_call_oi_change: int = 0
    total_put_oi_change: int = 0
    ratio: float | None = None
    pcr: float | None = None
    rows: list[ReplayRowOut]


class ReplayResponseOut(BaseModel):
    symbol: str
    expiry: str
    frames: list[ReplayFrameOut]


class OITimeseriesPointOut(BaseModel):
    ts: str
    total_call_oi: int
    total_put_oi: int
    ratio: float | None = None
    pcr: float | None = None


class OITimeseriesResponseOut(BaseModel):
    symbol: str
    expiry: str
    bucket: str
    points: list[OITimeseriesPointOut]


class RatioPointOut(BaseModel):
    ts: str
    ratio: float | None = None
    pcr: float | None = None
    total_call_oi: int
    total_put_oi: int


class RatioTimeseriesResponseOut(BaseModel):
    symbol: str
    expiry: str
    bucket: str
    points: list[RatioPointOut]


class HistoryDatesResponse(BaseModel):
    symbol: str
    expiry: str
    dates: list[str]  # IST trading days (YYYY-MM-DD), newest first


class MultiTFRowOut(BaseModel):
    timeframe: str
    call_oi_change: int
    put_oi_change: int
    oi_change_ratio: float | None = None


class MultiTFResponseOut(BaseModel):
    symbol: str
    expiry: str
    asof: str
    computed_at: str
    spot: float | None = None
    atm_strike: int | None = None
    total_call_oi: int
    total_put_oi: int
    ratio: float | None = None
    pcr: float | None = None
    rows: list[MultiTFRowOut]


class InterpretationRowOut(BaseModel):
    strike: int
    call: str
    put: str


class InterpretationResponseOut(BaseModel):
    timeframe: str
    expiry: str
    rows: list[InterpretationRowOut]


class AuthStartResponse(BaseModel):
    auth_mode: str
    state: str
    login_url: str


class AuthCallbackResponse(BaseModel):
    status: str
    message: str
    authenticated: bool


class LoginRequest(BaseModel):
    """Near-empty by design.

    The XTS market-data session authenticates with the appKey/secretKey in .env —
    there is no client code, MPIN or TOTP. Extra keys are ignored, so an older
    client still posting them keeps working.

    ``force_new_token=true`` is the ONLY way a caller can demand an actual token
    rotation (a human clicking "Force new broker session"). Every other login
    request coalesces onto the existing session or becomes a recovery request to
    the steward — rotations are single-authority now.
    """

    force_new_token: bool = False


class LoginResponse(BaseModel):
    status: str
    message: str
    authenticated: bool


class HiddenLoginRequest(BaseModel):
    """Fixed username+password gate for the /hidden dashboard (verified against .env)."""
    username: str
    password: str


class SymbolEntryOut(BaseModel):
    symbol: str
    display: str
    kind: str = Field(description="index | stock")
    sector: str
    fno_eligible: bool
    lot_size: int
    strike_step: int


class SymbolSectorGroup(BaseModel):
    sector: str
    symbols: list[SymbolEntryOut]


class SymbolsResponse(BaseModel):
    active_symbol: str
    groups: list[SymbolSectorGroup]


class ActiveSymbolRequest(BaseModel):
    symbol: str


class ActiveSymbolResponse(BaseModel):
    symbol: str
    display: str
    fno_eligible: bool
    spot: float | None
    expiries: list[str]


class OptionChainCrossCheckStrikeOut(BaseModel):
    strike: int
    expiry: str
    ce_ltp_ours: float | None = None
    ce_ltp_nse: float | None = None
    ce_ltp_diff: float | None = None
    ce_ltp_aligned: bool | None = None
    ce_oi_ours: int | None = None
    ce_oi_nse: int | None = None
    ce_oi_diff_pct: float | None = None
    ce_oi_aligned: bool | None = None
    pe_ltp_ours: float | None = None
    pe_ltp_nse: float | None = None
    pe_ltp_diff: float | None = None
    pe_ltp_aligned: bool | None = None
    pe_oi_ours: int | None = None
    pe_oi_nse: int | None = None
    pe_oi_diff_pct: float | None = None
    pe_oi_aligned: bool | None = None


class OptionChainCrossCheckSummary(BaseModel):
    total_strikes: int
    ce_ltp_aligned: int
    pe_ltp_aligned: int
    ce_oi_aligned: int
    pe_oi_aligned: int
    missing_in_ours: int = Field(description="Strikes present in NSE but not in our DB")
    missing_in_nse: int = Field(description="Strikes present in our DB but not in NSE")


class OptionChainCrossCheckResponse(BaseModel):
    symbol: str
    expiry: str | None
    checked_at: str
    nse_source: str = Field(description="option-chain-indices | option-chain-equities")
    ltp_tolerance_pts: float = 5.0
    ltp_tolerance_pct: float = 10.0
    oi_tolerance_pct: float = 10.0
    summary: OptionChainCrossCheckSummary
    rows: list[OptionChainCrossCheckStrikeOut]
    nse_fetch_error: str | None = Field(
        default=None,
        description="Set when NSE option chain could not be fetched (e.g. server-IP block). "
                    "Our DB data is still returned; NSE columns will be null.",
    )
    note: str = Field(
        default=(
            "NSE option chain data is published in batches and may lag the live XTS feed by "
            "several minutes. Run this check during active market hours (09:15–15:30 IST) "
            "for meaningful results."
        )
    )
