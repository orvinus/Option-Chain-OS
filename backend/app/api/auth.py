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
from .schemas import GateLoginRequest, LoginRequest, LoginResponse

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
    """Establish (or coalesce onto) the XTS market-data session.

    COALESCING by default: this endpoint no longer rotates the token on every
    call. During the 2026-08-06 outage the frontend hit it with force=True every
    ~6 seconds, each rotation killing the socket the previous one had enabled.
    Now:

    * session healthy → 200 "reused", ZERO broker calls. This also defuses any
      old cached frontend bundle still running the removed auto-login loop —
      success flips its `authenticated` gate and ends its retry cycle.
    * session unhealthy → a recovery REQUEST to the steward + a short wait; the
      response honestly reports where things stand.
    * ``force_new_token=true`` → the one manual path that truly mints (a human
      explicitly asked for a new broker session).
    """
    # Hard gate: a replay instance must NEVER log into the broker. The broker allows
    # ONE market-data session per appKey, so a login here would steal the single
    # production session (the recurring "data goes wrong every few days" bug).
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

    from ..ingest.session_steward import get_steward

    steward = get_steward()

    if body.force_new_token:
        # The explicit human override — the only path that demands a rotation.
        try:
            try:
                await asyncio.wait_for(
                    sess.login(force=True, manual=True, actor="api-manual"),
                    timeout=settings.xts_login_timeout_s,
                )
            except asyncio.TimeoutError:
                raise HTTPException(
                    504,
                    "The XTS market-data gateway did not respond in time. Check your "
                    f"network and XTS_MD_BASE_URL (timeout {settings.xts_login_timeout_s:.0f}s).",
                ) from None
            log.info("auth.login.manual_rotation", user_id=sess.user_id)
            asyncio.create_task(
                _bootstrap_live_ingestion_if_needed(fresh_token_minted=True),
                name="bootstrap-ingestion",
            )
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
            message="Fresh broker session minted. Live ingestion is reconnecting.",
            authenticated=True,
        )

    # Default path: coalesce or request recovery — never rotate from here.
    feed_healthy = steward is not None and steward.ws_feed_healthy
    if sess.authenticated and feed_healthy:
        # Make sure ingestion exists (first login after a deferred startup).
        asyncio.create_task(
            _bootstrap_live_ingestion_if_needed(fresh_token_minted=False),
            name="bootstrap-ingestion",
        )
        return LoginResponse(
            status="ok", message="Already connected — session reused.", authenticated=True
        )
    if steward is None:
        # Live mode before the lifespan finished (or tests) — nothing to ask yet.
        return LoginResponse(
            status="ok",
            message="Backend still starting; recovery will run automatically.",
            authenticated=sess.authenticated,
        )
    recovered = await steward.request_recovery_and_wait("api-login", timeout_s=15.0)
    if recovered:
        msg = "Reconnected."
    elif sess.circuit_state == "open":
        msg = (
            "Recovery is parked (rotation circuit open: "
            f"{sess.circuit_reason or 'repeated failures'}). It retries every few "
            "minutes; to force a fresh broker session POST force_new_token=true."
        )
    else:
        msg = "Recovery requested — the steward is escalating in the background."
    log.info("auth.login.recovery_requested", recovered=recovered)
    return LoginResponse(status="ok", message=msg, authenticated=sess.authenticated)


async def _fixed_credential_gate(
    body: GateLoginRequest, cfg_user: str, cfg_password: str, gate_label: str, env_hint: str
) -> LoginResponse:
    """Shared logic for the fixed-credential dashboard gate at "/".

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

    # (d) The broker session is a BONUS, not a precondition — and a sign-in must
    #     NEVER rotate the token. This gate used to call the login flow with
    #     force=True, which meant every dashboard sign-in during an outage
    #     invalidated the socket's token and fed the login storm. Ask the steward
    #     to recover, wait briefly, and report honestly either way.
    from ..ingest.session_steward import get_steward

    steward = get_steward()
    if steward is not None:
        recovered = await steward.request_recovery_and_wait(f"gate:{gate_label}", timeout_s=10.0)
        if recovered:
            return LoginResponse(status="ok", message="Signed in — feed connected.", authenticated=True)
    log.info("auth.gate.feed_recovering", gate=gate_label)
    return LoginResponse(
        status="ok",
        message=(
            "Signed in. The broker feed is recovering in the background — "
            "stored historical data is fully available meanwhile."
        ),
        authenticated=False,
    )


@router.post("/main-login", response_model=LoginResponse)
async def main_login(body: GateLoginRequest) -> LoginResponse:
    """Fixed-credential gate for the main dashboard at "/" (MAIN_USER / MAIN_PASSWORD)."""
    return await _fixed_credential_gate(
        body, settings.main_user, settings.main_password,
        "Main dashboard", "MAIN_USER and MAIN_PASSWORD",
    )
