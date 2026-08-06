"""Centralized typed configuration loaded from environment / .env file.

All other modules MUST import settings from here, never read os.getenv directly.
"""
from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _resolve_project_root() -> Path:
    """Repo root in dev (parent of ``backend/``). When frozen (PyInstaller), the folder containing the ``.exe``."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[3]


PROJECT_ROOT = _resolve_project_root()


class Settings(BaseSettings):
    """Runtime configuration."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------- Shrilakshmi Fintech / Symphony XTS market-data credentials ----------------
    # Obtained from the broker's API dashboard (appKey + secretKey). Market-data
    # login takes only these two + a source tag — no client code / MPIN / TOTP.
    xts_md_base_url: str = Field(
        default="https://developers.symphonyfintech.in/apibinarymarketdata",
        validation_alias="XTS_MD_BASE_URL",
    )
    xts_md_app_key: str = Field(default="", validation_alias="XTS_MD_APP_KEY")
    xts_md_secret_key: str = Field(default="", validation_alias="XTS_MD_SECRET_KEY")
    xts_md_source: str = Field(default="WebAPI", validation_alias="XTS_MD_SOURCE")
    xts_md_broadcast_mode: str = Field(default="Full", validation_alias="XTS_MD_BROADCAST_MODE")
    xts_md_publish_format: str = Field(default="JSON", validation_alias="XTS_MD_PUBLISH_FORMAT")
    # NOTE: there is deliberately no XTS_LOGIN_AT_STARTUP and no AUTH_MODE setting.
    # Both existed for a long time and neither did what its name and docs claimed:
    # XTS_LOGIN_AT_STARTUP was read by no code at all, and AUTH_MODE was an Angel One
    # leftover whose "publisher" value silently skipped session restore, login AND the
    # token refresh loop. The only gate now is `run_mode == "live"` in main.py's
    # lifespan, which always restores-or-logs-in.

    # ---------------- Database ----------------
    db_url: str = Field(
        default="postgresql+psycopg://postgres:postgres@localhost:5432/oi",
        validation_alias="DB_URL",
    )
    db_url_sync: str = Field(
        default="postgresql+psycopg://postgres:postgres@localhost:5432/oi",
        validation_alias="DB_URL_SYNC",
    )

    # ---------------- Ingestion scope ----------------
    underlying_symbol: str = Field(default="NIFTY", validation_alias="UNDERLYING_SYMBOL")
    nifty_index_token: str = Field(default="26000", validation_alias="NIFTY_INDEX_TOKEN")
    # Default 11 = ATM±11 = 23 strikes × 2 + spot = 47 live subscriptions, safely
    # under the broker's ~50-instrument-per-session cap. Do NOT raise the default
    # to 50 — that resolves to ~202 instruments, exceeds the cap, and silently
    # truncates the option chain. A larger window needs a broker plan with a
    # higher cap (and/or the REST universe poller for the non-active strikes).
    strike_window: int = Field(default=11, validation_alias="STRIKE_WINDOW")
    strike_step: int = Field(default=50, validation_alias="STRIKE_STEP")
    expiries: str = Field(default="current_weekly", validation_alias="EXPIRIES")
    nifty_lot_size: int = Field(default=75, validation_alias="NIFTY_LOT_SIZE")

    # ---------------- Aggregation cadence ----------------
    persist_bucket: Literal["1s", "5s", "1min"] = Field(
        default="1min",
        validation_alias="PERSIST_BUCKET",
        description="DB flush bucket. Use 1s or 5s if you rely on sub-minute OI timeframes (1s/15s/30s/45s); 1min often yields flat deltas for those windows.",
    )

    # ---------------- Universe poller (all-symbol REST-quote snapshotter) ----------------
    # The live WS feed can only carry ~50 instruments for the ONE viewed symbol.
    # This background poller REST-quotes every other F&O symbol's chain on a tiered
    # cadence into the same DB (option_oi_snapshots), so all symbols accrue OI in
    # parallel. It NEVER logs in (single-session-per-appKey) — it reuses the live
    # session's token and, on a 401, waits for the session steward to refresh it.
    #
    # POLLER_MODE supersedes the legacy POLLER_ENABLED boolean:
    #   off      — no poller at all.
    #   failover — poll ONLY the active symbol, ONLY while the WS feed is unhealthy,
    #              so a dead socket degrades the dashboard to ~15s REST updates
    #              instead of going dark. Zero load while the feed is healthy.
    #   full     — the original all-symbol tiered snapshotter (plus the failover
    #              behavior for the active symbol when the feed is down).
    # Legacy POLLER_ENABLED=true maps to "full" via effective_poller_mode.
    poller_mode: Literal["off", "failover", "full"] = Field(
        default="failover", validation_alias="POLLER_MODE"
    )
    poller_failover_interval_s: float = Field(
        default=15.0, validation_alias="POLLER_FAILOVER_INTERVAL_S"
    )
    poller_failover_linger_s: float = Field(
        default=120.0, validation_alias="POLLER_FAILOVER_LINGER_S"
    )
    poller_enabled: bool = Field(default=False, validation_alias="POLLER_ENABLED")
    poller_strike_window: int = Field(default=7, validation_alias="POLLER_STRIKE_WINDOW")
    poller_expiries: str = Field(default="current_weekly", validation_alias="POLLER_EXPIRIES")
    poller_quote_chunk: int = Field(default=25, validation_alias="POLLER_QUOTE_CHUNK")
    poller_max_concurrency: int = Field(default=6, validation_alias="POLLER_MAX_CONCURRENCY")
    poller_pace_ms: int = Field(default=50, validation_alias="POLLER_PACE_MS")
    poller_max_429_retries: int = Field(default=5, validation_alias="POLLER_MAX_429_RETRIES")
    poller_skip_active_symbol: bool = Field(default=True, validation_alias="POLLER_SKIP_ACTIVE_SYMBOL")
    poller_tier_fast_interval_s: float = Field(default=20.0, validation_alias="POLLER_TIER_FAST_S")
    poller_tier_mid_interval_s: float = Field(default=60.0, validation_alias="POLLER_TIER_MID_S")
    poller_tier_slow_interval_s: float = Field(default=180.0, validation_alias="POLLER_TIER_SLOW_S")

    # ---------------- API server ----------------
    api_host: str = Field(default="0.0.0.0", validation_alias="API_HOST")
    api_port: int = Field(default=8000, validation_alias="API_PORT")
    api_cors_origins: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173",
        validation_alias="API_CORS_ORIGINS",
    )

    # ---------------- Market session (IST) ----------------
    # The exchange moved the F&O close from 15:30 to 15:40 on 2026-08-04. These drive
    # is_nse_regular_session_open(), the session floor, staleness gating AND the expiry
    # settlement instant behind every IV/greek — so they must never be hardcoded again.
    market_open_ist: str = Field(default="09:15", validation_alias="MARKET_OPEN_IST")
    market_close_ist: str = Field(default="15:40", validation_alias="MARKET_CLOSE_IST")

    # ---------------- Behavior ----------------
    run_mode: Literal["live", "replay"] = Field(default="live", validation_alias="RUN_MODE")
    debug_ticks: bool = Field(default=False, validation_alias="DEBUG_TICKS")
    # Dashboard login: cap blocking waits so the UI never spins forever.
    xts_login_timeout_s: float = Field(
        default=45.0,
        validation_alias=AliasChoices("XTS_LOGIN_TIMEOUT_S", "SMARTAPI_LOGIN_TIMEOUT_S"),
    )
    db_persist_timeout_s: float = Field(default=20.0, validation_alias="DB_PERSIST_TIMEOUT_S")

    # ---------------- Dashboard gates ----------------
    # Fixed username + password protecting the /hidden dashboard (verified server-side,
    # never shipped to the browser). Blank => the /hidden-login endpoint returns 500 until set.
    hidden_user: str = Field(default="", validation_alias="HIDDEN_USER")
    hidden_password: str = Field(default="", validation_alias="HIDDEN_PASSWORD")
    # Separate fixed credential for the MAIN dashboard at "/" (distinct from /hidden).
    # Blank => the /main-login endpoint returns 500 until set.
    main_user: str = Field(default="", validation_alias="MAIN_USER")
    main_password: str = Field(default="", validation_alias="MAIN_PASSWORD")

    # ---------------- Alerting (Telegram) ----------------
    # Both blank => alerting is a no-op everywhere (dev default). On the VPS, set
    # both so the session steward and the oi-sentinel can page the operator.
    telegram_bot_token: str = Field(default="", validation_alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", validation_alias="TELEGRAM_CHAT_ID")
    alert_min_interval_s: float = Field(default=300.0, validation_alias="ALERT_MIN_INTERVAL_S")
    # "pretty" for dev terminals, "json" for production (machine-parseable).
    log_format: Literal["pretty", "json"] = Field(default="pretty", validation_alias="LOG_FORMAT")

    # ---------------- Broker session rotation policy ----------------
    # XTS is single-session-per-appKey: EVERY login invalidates the previous token,
    # killing the socket that token carried. Two production outages (2026-08-04,
    # 2026-08-06) were login storms where independent recovery actors rotated the
    # session out from under each other every ~6 seconds. These knobs are the
    # mechanical policy enforced inside MarketDataSession.login() — no caller can
    # rotate faster than the floor, and a burst opens the recovery circuit.
    login_floor_s: float = Field(default=90.0, validation_alias="LOGIN_FLOOR_S")
    login_burst_max: int = Field(default=5, validation_alias="LOGIN_BURST_MAX")
    login_burst_window_s: float = Field(default=600.0, validation_alias="LOGIN_BURST_WINDOW_S")
    # Daily pre-open token rotation instant (IST HH:MM). Chosen pre-open so a
    # rotation can never fire in the evening and leave the socket dead overnight —
    # the trigger of both outages.
    daily_token_refresh_ist: str = Field(default="08:35", validation_alias="DAILY_TOKEN_REFRESH_IST")
    # Strict-health staleness threshold (any-origin data age during an open session).
    strict_stale_after_s: float = Field(default=300.0, validation_alias="STRICT_STALE_AFTER_S")

    # ---------------- IV scanner / HV ----------------
    iv_scanner_max_symbols: int = Field(default=50, validation_alias="IV_SCANNER_MAX_SYMBOLS")
    yahoo_hv_enabled: bool = Field(default=True, validation_alias="YAHOO_HV_ENABLED")
    iv_history_snapshot_interval_s: float = Field(
        default=900.0,
        validation_alias="IV_HISTORY_SNAPSHOT_INTERVAL_S",
        description="Seconds between background ATM-IV snapshots for the active symbol.",
    )

    # -------- Derived helpers --------

    @property
    def expiry_policies(self) -> list[str]:
        return [s.strip() for s in self.expiries.split(",") if s.strip()]

    @property
    def poller_expiry_policies(self) -> list[str]:
        return [s.strip() for s in self.poller_expiries.split(",") if s.strip()]

    @property
    def cors_origins_list(self) -> list[str]:
        return [s.strip() for s in self.api_cors_origins.split(",") if s.strip()]

    @property
    def effective_poller_mode(self) -> str:
        """POLLER_MODE, honoring the legacy POLLER_ENABLED=true as "full".

        An .env that opted into the all-symbol poller via the old boolean keeps
        its meaning when POLLER_MODE is untouched (still at its "failover"
        default). An explicit POLLER_MODE=off or =full always wins.
        """
        if self.poller_enabled and self.poller_mode == "failover":
            return "full"
        return self.poller_mode

    @field_validator("strike_window")
    @classmethod
    def _validate_strike_window(cls, v: int) -> int:
        if v <= 0 or v > 500:
            raise ValueError("STRIKE_WINDOW must be 1..500")
        return v

    @field_validator("strike_step")
    @classmethod
    def _validate_strike_step(cls, v: int) -> int:
        # Fallback only; the per-symbol step is read from the registry
        # (data/symbols.json). Stocks use steps like 1/2/5/10/20; indices 25/50/100.
        if v <= 0 or v > 1000:
            raise ValueError("STRIKE_STEP must be a positive integer (typical: 1..100)")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings: Settings = get_settings()
