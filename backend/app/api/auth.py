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
import secrets
from datetime import datetime

from fastapi import APIRouter, HTTPException
from sqlalchemy.exc import SQLAlchemyError

from ..auth import get_session_manager
from ..core.config import settings
from ..core.logging import get_logger
from .schemas import HiddenLoginRequest, LoginRequest, LoginResponse

log = get_logger("auth.api")
router = APIRouter(prefix="/auth", tags=["auth"])


async def _bootstrap_live_ingestion_if_needed(fresh_token_minted: bool = True) -> None:
    """Start (or reconnect) live ingestion after a login — via the ONE factory path.

    The previous inline construction here dropped index_token/index_segment
    (silently defaulting a SENSEX/MCX active symbol back to NIFTY-on-NSECM) and
    nudged the feed on EVERY login response even when the rotation floor had
    merely reused the existing token — dropping a healthy socket for nothing.
    """
    try:
        from ..ingest.feed_factory import ensure_live_ingestion

        await ensure_live_ingestion(fresh_token_minted=fresh_token_minted)
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
            await asyncio.wait_for(sess.login(force=True), timeout=settings.xts_login_timeout_s)
        except asyncio.TimeoutError:
            raise HTTPException(
                504,
                "The XTS market-data gateway did not respond in time. Check your "
                f"network and XTS_MD_BASE_URL (timeout {settings.xts_login_timeout_s:.0f}s).",
            ) from None

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


async def _fixed_credential_gate(
    body: HiddenLoginRequest, cfg_user: str, cfg_password: str, gate_label: str, env_hint: str
) -> LoginResponse:
    """Shared logic for the fixed-credential dashboard gates (/hidden and the main /).

    Verifies a single username+password from .env (never shipped to the browser),
    then — since these dashboards only load data once the broker session is live —
    establishes the XTS market-data session by reusing the same login flow.
    """
    # (a) Reject misconfiguration FIRST: blank env would make compare_digest("","")
    #     return True and let empty credentials through.
    if not cfg_user or not cfg_password:
        raise HTTPException(
            500,
            f"{gate_label} login is not configured. Set {env_hint} in your .env file "
            "and restart the backend.",
        )

    # (b) Constant-time compare on bytes (str raises TypeError on non-ASCII).
    #     Use `&` (not `and`) so both comparisons always run — no per-field timing leak.
    ok = secrets.compare_digest(
        body.username.encode(), cfg_user.encode()
    ) & secrets.compare_digest(body.password.encode(), cfg_password.encode())
    if not ok:
        raise HTTPException(401, "Invalid username or password.")

    # (c0) Replay: credentials are valid but there is NO broker session to start —
    #      never contact the broker on a replay instance (single-session safety).
    #      Unlock the gated UI for historical/replay data without a broker login.
    if settings.run_mode != "live":
        return LoginResponse(
            status="ok",
            message="Replay mode — historical data only (broker feed disabled).",
            authenticated=False,
        )

    # (c) Start / confirm the broker feed. If a session already exists (e.g. admin
    #     logged in on the other dashboard, or auto-login at startup), reuse it —
    #     a fresh force=True login would invalidate the single-session token and
    #     needlessly reconnect the running feed.
    sess = get_session_manager()
    if sess.authenticated:
        return LoginResponse(status="ok", message="Already connected.", authenticated=True)

    # (d) The broker session is a BONUS, not a precondition. The user's dashboard
    #     credentials were already verified in (b), and every read endpoint serves
    #     stored history without a broker session. Previously a broker outage
    #     propagated its 401 straight out of this gate, so a correct password was
    #     rejected and months of collected data became unreachable — exactly when
    #     you most want to look at history. Degrade instead: unlock the UI and
    #     report authenticated=False so the dashboard can flag the feed as offline.
    try:
        return await login(LoginRequest())
    except HTTPException as e:
        log.warning("auth.gate.broker_unavailable", gate=gate_label, detail=str(e.detail))
        return LoginResponse(
            status="ok",
            message=(
                "Signed in. The broker feed is unavailable right now, so this is "
                f"stored historical data only ({e.detail})"
            ),
            authenticated=False,
        )


@router.post("/hidden-login", response_model=LoginResponse)
async def hidden_login(body: HiddenLoginRequest) -> LoginResponse:
    """Fixed-credential gate for the /hidden dashboard (HIDDEN_USER / HIDDEN_PASSWORD)."""
    return await _fixed_credential_gate(
        body, settings.hidden_user, settings.hidden_password,
        "Hidden dashboard", "HIDDEN_USER and HIDDEN_PASSWORD",
    )


@router.post("/main-login", response_model=LoginResponse)
async def main_login(body: HiddenLoginRequest) -> LoginResponse:
    """Fixed-credential gate for the main dashboard at "/" (MAIN_USER / MAIN_PASSWORD).

    A distinct credential from /hidden so the two dashboards can be shared separately.
    """
    return await _fixed_credential_gate(
        body, settings.main_user, settings.main_password,
        "Main dashboard", "MAIN_USER and MAIN_PASSWORD",
    )
