"""Centralized typed configuration loaded from environment / .env file.

All other modules MUST import settings from here, never read os.getenv directly.
"""
from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
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
    # If false (default), the XTS market-data login happens only via the dashboard —
    # backend starts immediately. Set true to attempt env-based login on startup.
    xts_login_at_startup: bool = Field(default=False, validation_alias="XTS_LOGIN_AT_STARTUP")

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
    # session's token and, on a 401, waits for the feed's own self-heal to refresh it.
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

    # ---------------- Authentication mode ----------------
    # Retained for backward compatibility with the boot/restore branches. The XTS
    # market-data API uses a single appKey/secretKey login; "totp" simply selects
    # that env-credential flow (no MPIN/TOTP/OAuth is involved).
    auth_mode: Literal["totp", "publisher"] = Field(default="totp", validation_alias="AUTH_MODE")

    # ---------------- Behavior ----------------
    run_mode: Literal["live", "replay"] = Field(default="live", validation_alias="RUN_MODE")
    debug_ticks: bool = Field(default=False, validation_alias="DEBUG_TICKS")
    # Dashboard login: cap blocking waits so the UI never spins forever.
    smartapi_login_timeout_s: float = Field(default=45.0, validation_alias="SMARTAPI_LOGIN_TIMEOUT_S")
    db_persist_timeout_s: float = Field(default=20.0, validation_alias="DB_PERSIST_TIMEOUT_S")

    # ---------------- Hidden dashboard gate ----------------
    # Fixed username + password protecting the /hidden dashboard (verified server-side,
    # never shipped to the browser). Blank => the /hidden-login endpoint returns 500 until set.
    hidden_user: str = Field(default="", validation_alias="HIDDEN_USER")
    hidden_password: str = Field(default="", validation_alias="HIDDEN_PASSWORD")

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
