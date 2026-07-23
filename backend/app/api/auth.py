"""Authentication routes.

Endpoints:
  POST /api/auth/login   — establish the XTS market-data session (appKey/secretKey from .env)

The XTS market-data API authenticates with only an appKey + secretKey (configured
in .env), so the login form needs no per-user credentials. The ``LoginRequest``
body is accepted for backward compatibility with the existing dashboard form but
its fields are ignored.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from fastapi import APIRouter, HTTPException
from sqlalchemy.exc import SQLAlchemyError

from ..auth import get_session_manager
from ..core.config import settings
from ..core.logging import get_logger
from .schemas import LoginRequest, LoginResponse

log = get_logger("auth.api")
router = APIRouter(prefix="/auth", tags=["auth"])


async def _bootstrap_live_ingestion_if_needed() -> None:
    """Start aggregator + feed if startup did not already (non-blocking)."""
    # Replay instances NEVER contact the broker (single-session-per-appKey safety):
    # a live feed here would compete with the one production session on the same key.
    if settings.run_mode != "live":
        log.info("auth.login.ingestion_skipped_replay")
        return
    try:
        from ..main import _resubscribe_provider, _spot_refresher
        from ..runtime import get_runtime
        from ..ingest.aggregator import MinuteAggregator
        from ..ingest.ws_client import OptionFeedClient
        from ..services import get_oi_engine
        from ..ws import get_hub

        rt = get_runtime()
        if rt.feed_client is not None:
            # A feed is already running, but its live Socket.IO connection was
            # opened with the PREVIOUS token. XTS market-data allows only ONE
            # valid token per appKey — every fresh login invalidates the prior
            # one — so the existing socket (and its subscriptions) are now bound
            # to a dead token and every subscribe returns 'Invalid Token' with no
            # ticks flowing. Drop the socket so the supervisor reconnects via
            # _connect_once(), which re-reads the FRESH token from the session
            # manager and re-subscribes under it. Without this nudge, a "successful"
            # login leaves the feed silently dead.
            log.info("auth.login.reconnecting_feed_with_fresh_token")
            rt.feed_client.nudge_reconnect()
            return

        engine = get_oi_engine()
        hub = get_hub()

        async def _on_flush(bucket: datetime, rows: int) -> None:
            rt.last_flush_at = bucket
            rt.last_flush_rows = rows
            engine.on_aggregator_flush(bucket)
            await hub.publish_flush(bucket, rows)

        aggregator = MinuteAggregator(rt.tick_queue, on_flush=_on_flush)
        await aggregator.start()
        rt.aggregator = aggregator

        feed = OptionFeedClient(rt.tick_queue, _resubscribe_provider)
        await feed.start()
        rt.feed_client = feed
        asyncio.create_task(_spot_refresher(feed), name="spot-refresher-after-login")
        log.info("auth.login.ingestion_started")
    except Exception as e:
        log.error("auth.login.ingestion_bootstrap.failed", error=str(e))


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest) -> LoginResponse:
    """Establish the XTS market-data session using env appKey/secretKey.

    Responds as soon as login + DB persist succeed; live ingestion starts in the
    background so the browser does not hang on network latency.
    """
    # Hard gate: a replay instance must NEVER log into the broker. The broker allows
    # ONE market-data session per appKey, so a login here would steal the single
    # production session (the recurring "data goes wrong every few days" bug). The
    # frontend also skips auto-connect in replay, but this is the authoritative
    # backstop — no caller can coax a replay backend into a broker session.
    if settings.run_mode != "live":
        raise HTTPException(
            409,
            "Broker login is disabled in replay mode (RUN_MODE=replay). This instance "
            "serves historical data only and never contacts the broker. To run a live "
            "feed locally, set RUN_MODE=live with a DEDICATED appKey — never the "
            "production key (they cannot share one broker session).",
        )

    sess = get_session_manager()

    if not (settings.xts_md_app_key or "").strip() or not (settings.xts_md_secret_key or "").strip():
        raise HTTPException(
            400,
            "Missing XTS market-data credentials. Set XTS_MD_APP_KEY and "
            "XTS_MD_SECRET_KEY in your .env file.",
        )

    try:
        try:
            # force=True: an explicit dashboard login always mints a fresh token,
            # bypassing the debounce (the user clicked because they want a new session).
            await asyncio.wait_for(sess.login(force=True), timeout=settings.smartapi_login_timeout_s)
        except asyncio.TimeoutError:
            raise HTTPException(
                504,
                "The XTS market-data gateway did not respond in time. Check your "
                f"network and XTS_MD_BASE_URL (timeout {settings.smartapi_login_timeout_s:.0f}s).",
            ) from None

        await sess.start_refresh_loop()
        log.info("auth.login.success", user_id=sess.user_id)
        asyncio.create_task(_bootstrap_live_ingestion_if_needed(), name="bootstrap-ingestion")

    except HTTPException:
        raise
    except SQLAlchemyError as e:
        log.error("auth.login.db_error", error=str(e))
        raise HTTPException(
            503,
            "Could not save your session to PostgreSQL. Start TimescaleDB "
            "(Docker Desktop + scripts/start-timescale.ps1) and confirm DB_URL in .env. "
            f"Database error: {e}",
        ) from e
    except Exception as e:
        log.warning("auth.login.failed", error=str(e))
        raise HTTPException(401, f"XTS market-data login failed: {e}") from e

    return LoginResponse(
        status="ok",
        message="Logged in. Live ingestion is starting in the background.",
        authenticated=True,
    )
