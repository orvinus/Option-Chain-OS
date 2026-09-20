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

    # ---------------- TrueData (historical REST backfill only) ----------------
    # Used exclusively by scripts/truedata_backfill.py + market_data/truedata_rest.py.
    # No TrueData WebSocket is opened anywhere — the live feed stays XTS. Bearer
    # tokens die at a fixed wall clock (~04:00 IST), not a rolling TTL.
    truedata_user: str = Field(default="", validation_alias="TRUEDATA_USER")
    truedata_password: str = Field(default="", validation_alias="TRUEDATA_PASSWORD")
    # OI unit normalization: multiply vendor OI by this per exchange. 1 = vendor
    # already reports units (contracts × lot). If probe P-B finds lots, set the
    # symbol's lot size here (e.g. 65 / 20) — a config flip, never a code change.
    truedata_oi_scale_nse: int = Field(default=1, validation_alias="TRUEDATA_OI_SCALE_NSE")
    truedata_oi_scale_bse: int = Field(default=1, validation_alias="TRUEDATA_OI_SCALE_BSE")
    # REST request governor (requests/second). Docs contradict (1–10/s); start
    # conservative, raise after the probe measures the enforced ceiling.
    truedata_rate_limit_rps: float = Field(default=4.0, validation_alias="TRUEDATA_RATE_LIMIT_RPS")
    # Proxy for ALL TrueData traffic — REST today, the realtime WS once
    # FEED_VENDOR=truedata (e.g. socks5://warp-proxy:40000 = Cloudflare WARP proxy
    # mode on the VPS, whose direct IP the vendor's edge drops for being a hosting
    # ASN). Never affects the XTS feed. Empty = direct connection (correct on a dev PC).
    #
    # On the VPS this MUST be a host-resolvable name, not 127.0.0.1: the backend
    # runs on a Docker bridge where 127.0.0.1 is the container's own loopback,
    # while WARP binds the host's. `warp-proxy` resolves to the pinned bridge
    # gateway inside the container (docker-compose extra_hosts) and to 127.0.0.1
    # on the host (/etc/hosts), so one value serves both consumers.
    truedata_proxy: str = Field(default="", validation_alias="TRUEDATA_PROXY")

    # ---------------- Live feed vendor switch ----------------
    # Selects the ENTIRE live stack: REST client, feed adapter, session manager,
    # steward variant and health sub-checks. This is the migration's rollback
    # switch — flipping it back to "xts" plus a restart is the whole procedure,
    # and it is data-clean because every read is session-floored (the two vendors'
    # rows are separated by timestamp, never joined).
    # "td_relay" = follower mode: no vendor login at all; ticks arrive from another
    # backend's /ws/td-relay (see ws/td_relay.py). This is how two backends share
    # TrueData's single realtime login.
    feed_vendor: Literal["xts", "truedata", "td_relay"] = Field(
        default="xts", validation_alias="FEED_VENDOR"
    )
    # Runs the TrueData feed ALONGSIDE the live one, writing to td_shadow_snapshots
    # instead of option_oi_snapshots. Independent of feed_vendor: this is the
    # parallel-validation mode, not a vendor selection.
    truedata_shadow_enabled: bool = Field(
        default=False, validation_alias="TRUEDATA_SHADOW_ENABLED"
    )

    # ---------------- TrueData realtime WebSocket ----------------
    truedata_ws_host: str = Field(
        default="push.truedata.in", validation_alias="TRUEDATA_WS_HOST"
    )
    # 8084 production, 8086 sandbox/trial. The trial account answers ONLY on 8086
    # (8082/8084 return "User Subscription Expired"), so this must be re-pointed
    # when the paid tier goes live — and every sandbox measurement re-confirmed.
    truedata_ws_port: int = Field(default=8086, validation_alias="TRUEDATA_WS_PORT")
    # 0 = trust the maxsymbols the login response reports. Only set this to pin a
    # lower ceiling than the plan allows (e.g. to leave headroom for a second
    # consumer on the same account).
    truedata_maxsymbols_override: int = Field(
        default=0, validation_alias="TRUEDATA_MAXSYMBOLS_OVERRIDE"
    )
    truedata_budget_safety_margin: int = Field(
        default=2, validation_alias="TRUEDATA_BUDGET_SAFETY_MARGIN"
    )
    truedata_subscribe_batch: int = Field(
        default=100, validation_alias="TRUEDATA_SUBSCRIBE_BATCH"
    )
    # Option chains streamed ALONGSIDE the dashboard's symbol (comma list), so
    # e.g. a SENSEX trading day has live data while the dashboard shows NIFTY.
    # Planned at ELASTIC priority: they get whatever the plan's maxsymbols
    # leaves after the viewed chain — on the 50-symbol trial that is only the
    # pinned indices' spot; on a 250+ plan every chain streams in full.
    truedata_pinned_symbols: str = Field(
        default="NIFTY,SENSEX", validation_alias="TRUEDATA_PINNED_SYMBOLS"
    )
    # How often pinned chains are checked for an ATM re-centre (seconds).
    truedata_pinned_recentre_s: float = Field(
        default=60.0, validation_alias="TRUEDATA_PINNED_RECENTRE_S"
    )
    # Re-emit the last known state for every subscribed contract once per persist
    # bucket. NOT optional in practice: TrueData sends a frame only when a trade
    # occurs, whereas XTS pushed OI ~1/min regardless — without this, an untraded
    # deep-OTM strike produces NO rows at all between trades, and wing analytics
    # read holes. Gated on the vendor heartbeat so it can never mask a dead socket.
    # ---------------- TrueData relay (one vendor socket, many backends) ----------
    # OWNER side (the process that holds the vendor login): TD_RELAY_ENABLED=true
    # exposes /ws/td-relay, gated by TD_RELAY_KEY. FOLLOWER side: FEED_VENDOR=
    # td_relay + TD_RELAY_URL=wss://<owner>/ws/td-relay + the same TD_RELAY_KEY.
    td_relay_enabled: bool = Field(default=False, validation_alias="TD_RELAY_ENABLED")
    td_relay_key: str = Field(default="", validation_alias="TD_RELAY_KEY")
    td_relay_url: str = Field(default="", validation_alias="TD_RELAY_URL")

    # ---------------- Boot-time historical gap fill ----------------
    # After GAPFILL_DELAY_S, trading days in the last GAPFILL_LOOKBACK_DAYS with
    # fewer than GAPFILL_MIN_MINUTES usable minutes are pulled from the TrueData
    # REST archive via scripts/truedata_backfill.py (see ingest/gapfill.py).
    gapfill_on_boot: bool = Field(default=True, validation_alias="GAPFILL_ON_BOOT")
    gapfill_lookback_days: int = Field(default=30, validation_alias="GAPFILL_LOOKBACK_DAYS")
    gapfill_min_minutes: int = Field(default=300, validation_alias="GAPFILL_MIN_MINUTES")
    # NSE UDiFF settlement bars (scripts/nse_settlement_backfill.py). Public
    # archive, no credentials. Fills the days a contract was listed but never
    # traded, which is what TradingView's D/W bars carry and what the UMP
    # DISCOVERY/BRIDGE levels are built from on a young contract.
    settlement_backfill_on_boot: bool = Field(
        default=True, validation_alias="SETTLEMENT_BACKFILL_ON_BOOT"
    )
    settlement_backfill_days: int = Field(
        default=75, validation_alias="SETTLEMENT_BACKFILL_DAYS"
    )
    gapfill_symbols: str = Field(default="NIFTY,SENSEX", validation_alias="GAPFILL_SYMBOLS")
    gapfill_delay_s: float = Field(default=120.0, validation_alias="GAPFILL_DELAY_S")
    gapfill_recheck_h: float = Field(default=24.0, validation_alias="GAPFILL_RECHECK_H")
    gapfill_timeout_s: float = Field(default=7200.0, validation_alias="GAPFILL_TIMEOUT_S")
    # Same-day SESSION catch-up (2026-09-09): restores 09:15→boot (and any
    # reconnect hole) from the vendor's same-day 1-minute bars, promoted into
    # option_oi_snapshots with src=1 wherever the live feed has no row for that
    # minute. Checked every GAPFILL_CATCHUP_CHECK_S while the session is open,
    # at most once per GAPFILL_CATCHUP_MIN_INTERVAL_S per symbol, and only when
    # at least GAPFILL_CATCHUP_MIN_MISSING minutes are absent.
    gapfill_catchup_check_s: float = Field(default=300.0, validation_alias="GAPFILL_CATCHUP_CHECK_S")
    gapfill_catchup_min_interval_s: float = Field(
        default=300.0, validation_alias="GAPFILL_CATCHUP_MIN_INTERVAL_S"
    )
    gapfill_catchup_min_missing: int = Field(default=2, validation_alias="GAPFILL_CATCHUP_MIN_MISSING")
    gapfill_catchup_max_expiries: int = Field(default=2, validation_alias="GAPFILL_CATCHUP_MAX_EXPIRIES")
    # The months-long historical pull is deferred to close + this many minutes
    # on a trading day (it competes with the live socket for the vendor path),
    # unless the lookback has NO usable day at all (fresh install).
    gapfill_post_close_delay_min: int = Field(default=20, validation_alias="GAPFILL_POST_CLOSE_DELAY_MIN")
    # Data-integrity report: OI on an ATM±3 strike unchanged for this many
    # consecutive session minutes is reported as stale.
    integrity_stale_oi_minutes: int = Field(default=30, validation_alias="INTEGRITY_STALE_OI_MINUTES")

    truedata_hold_last_enabled: bool = Field(
        default=True, validation_alias="TRUEDATA_HOLD_LAST_ENABLED"
    )
    # OI scale for MCX, which the NSE/BSE probes do not cover (the vendor may
    # report commodity OI in lots even where index OI is in units).
    truedata_oi_scale_mcx: int = Field(default=1, validation_alias="TRUEDATA_OI_SCALE_MCX")

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
    # "segment_sweep" is TrueData-only and requires the getAllBars add-on: one
    # request per minute returns a whole segment's bars, which is the only
    # arithmetically viable way to cover the 212 long-tail stocks (per-contract
    # polling is ~10,580 requests per sweep — hours, and a certain quota ban).
    poller_mode: Literal["off", "failover", "full", "segment_sweep"] = Field(
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

    # ---------------- Dashboard gate ----------------
    # Fixed username + password protecting the main dashboard at "/" (verified
    # server-side, never shipped to the browser). Blank => the /main-login
    # endpoint returns 500 until set.
    main_user: str = Field(default="", validation_alias="MAIN_USER")
    main_password: str = Field(default="", validation_alias="MAIN_PASSWORD")

    # ---------------- Algo Config admin (real server-side auth) ----------------
    # Unlike the display-only gates above, /api/algo/* is protected by a hashed
    # user row + signed httpOnly session cookie. These two seed the admin row on
    # first login against an empty algo_users table; blank => every algo
    # endpoint returns 503 until configured (misconfiguration is loud, never an
    # open door).
    algo_admin_user: str = Field(default="", validation_alias="ALGO_ADMIN_USER")
    algo_admin_password: str = Field(default="", validation_alias="ALGO_ADMIN_PASSWORD")
    # HMAC key for session tokens. Blank (dev default) => an ephemeral per-boot
    # secret, so sessions simply expire on restart instead of sharing a key.
    algo_session_secret: str = Field(default="", validation_alias="ALGO_SESSION_SECRET")
    algo_session_ttl_h: float = Field(default=12.0, validation_alias="ALGO_SESSION_TTL_H")

    # ---------------- Lakshmishree Interactive API (order execution) ----------------
    # Symphony XTS *Interactive* API credentials — a SEPARATE key pair from the
    # market-data appKey above (same vendor stack, different product). All blank
    # (the default) => the live broker is "Not connected": the engine refuses
    # live entries and only the paper simulator routes orders. Live routing
    # additionally requires PAPER_MODE off in the Algo Config document.
    lakshmishree_interactive_url: str = Field(
        default="", validation_alias="LAKSHMISHREE_INTERACTIVE_URL"
    )
    lakshmishree_interactive_app_key: str = Field(
        default="", validation_alias="LAKSHMISHREE_INTERACTIVE_APP_KEY"
    )
    lakshmishree_interactive_secret_key: str = Field(
        default="", validation_alias="LAKSHMISHREE_INTERACTIVE_SECRET_KEY"
    )
    lakshmishree_interactive_source: str = Field(
        default="WEBAPI", validation_alias="LAKSHMISHREE_INTERACTIVE_SOURCE"
    )

    # ---------------- Algo Config live stream (/ws/algo-stream) ----------------
    # The Algo Config page's push feed. Clock-driven and deliberately NOT hung
    # off the ingest flush hook: the flush path writes rt.last_ws_flush_at,
    # which is the session steward's dead-socket detector, and must never be
    # made to wait on how many dashboard tabs are open.
    #
    # ENABLED=false is the market-day rollback lever: the socket then closes
    # with an error frame and the panels fall back to their REST polling.
    algo_stream_enabled: bool = Field(default=True, validation_alias="ALGO_STREAM_ENABLED")
    # Price / forming-candle cadence. 1 s matches PERSIST_BUCKET, which is the
    # finest granularity the DB actually carries.
    algo_stream_tick_ms: int = Field(default=1000, validation_alias="ALGO_STREAM_TICK_MS")
    # OI-derived readings (OI-Structure / MTF-Ratio / MQAE) refresh cadence.
    # Deliberately slower than the price tick: the exchange disseminates OI
    # roughly ONCE PER MINUTE, so recomputing these per second would burn union
    # -view queries to redraw an identical number. 10 s still beats the old
    # 30 s poll and, unlike it, includes the forming bucket.
    # 30 s matches the polling cadence the panels already used, so the load on
    # the union view is no worse than today — but unlike the old poll this one
    # returns the forming bucket AND the closed basis from a single fetch.
    algo_stream_oi_refresh_s: float = Field(
        default=30.0, validation_alias="ALGO_STREAM_OI_REFRESH_S"
    )
    # Slow lane: broker account / paper session / P&L aggregates.
    algo_stream_slow_refresh_s: float = Field(
        default=20.0, validation_alias="ALGO_STREAM_SLOW_REFRESH_S"
    )
    # Concurrent subscriber ceiling. The thing being protected is the 30-slot
    # DB pool shared with ingest and the orchestrator.
    algo_stream_max_subs: int = Field(default=8, validation_alias="ALGO_STREAM_MAX_SUBS")

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
    def truedata_pinned_symbol_list(self) -> list[str]:
        out: list[str] = []
        for s in self.truedata_pinned_symbols.split(","):
            s = s.strip().upper()
            if s and s not in out:
                out.append(s)
        return out

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
