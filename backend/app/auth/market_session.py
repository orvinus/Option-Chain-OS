"""Shrilakshmi Fintech / Symphony XTS market-data session manager.

This module is the *only* place in the codebase that performs the XTS
``/auth/login`` (and ``/auth/logout``) round-trip and holds the resulting token.

Lifecycle:
    1.  On FastAPI startup, ``try_restore_session_from_db()`` may load the latest
        row from ``auth_sessions`` and reuse a still-valid token so the feed can
        run without a fresh login.
    2.  Otherwise ``MarketDataSession.login()`` posts ``appKey`` + ``secretKey``
        to ``/auth/login`` and stores the token (valid ~24h) in DB and memory.
    3.  A background task re-logs in every ``REFRESH_EVERY`` to renew the token
        (the market-data API has no separate refresh endpoint — renewal == login).
    4.  On shutdown we call ``/auth/logout``.

The legacy ``auth_sessions`` table columns are reused without a schema change:
``jwt_token`` holds the XTS token, ``client_code`` holds the userID, and
``refresh_token`` / ``feed_token`` mirror the token (unused by XTS market data).
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..core.config import settings
from ..core.db import session_scope
from ..core.logging import get_logger
from ..market_data import xts_client

log = get_logger("auth")

# XTS market-data tokens are valid ~24h. Renew (re-login) every 12h for margin.
REFRESH_EVERY = timedelta(hours=12)
TOKEN_TTL = timedelta(hours=24)
# A failed renewal must NOT cost a full REFRESH_EVERY of silence. The renewal is what
# keeps the feed alive, and the timer is anchored to process start, so one failure at
# the wrong moment used to burn an entire trading session.
REFRESH_RETRY_AFTER_FAILURE = timedelta(minutes=5)

# Debounce window for non-forced logins. XTS is single-session per appKey — every
# login invalidates the prior token — so a manual login + post-login feed bootstrap
# + a self-heal firing within a few seconds of each other must NOT each mint a new
# token (they would invalidate one another). Within this window a non-forced login
# reuses the just-minted token instead. The manual endpoint passes force=True.
LOGIN_DEBOUNCE_S = 20.0


@dataclass
class SessionTokens:
    """Holds the XTS market-data token.

    The ``auth_sessions`` table columns predate this integration and are reused
    as-is (renaming them would need a migration for no functional gain):
      * ``jwt_token``     -> the XTS market-data token
      * ``client_code``   -> the XTS userID
      * ``refresh_token`` / ``feed_token`` -> mirror the token (XTS has neither)
    """

    jwt_token: str
    refresh_token: str
    feed_token: str
    issued_at: datetime
    client_code: str

    @property
    def expires_at(self) -> datetime:
        return self.issued_at + TOKEN_TTL


class MarketDataSession:
    """Singleton-style holder for the XTS market-data token."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tokens: Optional[SessionTokens] = None
        self._refresh_task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()
        # Serialises concurrent login() calls (manual + bootstrap + self-heal +
        # refresh) so only one /auth/login round-trip runs at a time.
        self._login_lock = asyncio.Lock()
        self._last_login_mono: float = 0.0
        # Set when XTS itself rejected our token (see mark_broker_rejected). Kept
        # separate from the TTL so `authenticated` can reflect broker reality.
        self._broker_rejected_at: Optional[datetime] = None

    # ---------------------------------------------------------- properties

    @property
    def tokens(self) -> SessionTokens:
        with self._lock:
            if not self._tokens:
                raise RuntimeError("No token; call login() first.")
            return self._tokens

    @property
    def token(self) -> str:
        """The raw XTS market-data token (used in REST headers + socket query)."""
        return self.tokens.jwt_token

    @property
    def user_id(self) -> str:
        return self.tokens.client_code

    @property
    def authenticated(self) -> bool:
        """TTL-valid AND not known-rejected by the broker.

        The TTL alone is pure local arithmetic (``issued_at + 24h``) that never asks
        XTS, so a token the broker killed overnight still reported True — which
        silently disabled BOTH unattended recovery paths: the dashboard's 60s
        auto-reconnect (gated on ``!authenticated``) and ``_fixed_credential_gate``'s
        "Already connected." short-circuit. Production ran a full session on a dead
        token that way (2026-08-03/04).
        """
        with self._lock:
            if self._tokens is None or self._tokens.expires_at <= datetime.now(timezone.utc):
                return False
            return self._broker_rejected_at is None

    def mark_broker_ok(self) -> None:
        """Record that XTS accepted this token (clears a prior rejection)."""
        with self._lock:
            self._broker_rejected_at = None

    def mark_broker_rejected(self, reason: str = "") -> None:
        """Record that XTS rejected this token and auto-recovery is not winning.

        Call this only once the feed's own self-heal has failed or exhausted its
        budget — a transient blip should not flip the dashboard to "disconnected".
        """
        with self._lock:
            if self._broker_rejected_at is not None:
                return
            self._broker_rejected_at = datetime.now(timezone.utc)
        log.warning("xts.session.broker_rejected", reason=reason)

    # ---------------------------------------------------------- public API

    async def login(self, force: bool = False) -> SessionTokens:
        """Perform an XTS market-data login using env appKey/secretKey.

        Single-flight + debounced. ``force=True`` (used by the manual dashboard
        login) always mints a fresh token; ``force=False`` (self-heal / refresh /
        bootstrap) reuses a token minted within ``LOGIN_DEBOUNCE_S`` to avoid the
        single-session token thrash described on ``LOGIN_DEBOUNCE_S``.
        """
        async with self._login_lock:
            if not force:
                with self._lock:
                    have = self._tokens
                age = time.monotonic() - self._last_login_mono
                if have is not None and age < LOGIN_DEBOUNCE_S:
                    log.info("xts.login.debounced", age_s=round(age, 1))
                    return have
            result = await xts_client.login()
            tokens = SessionTokens(
                jwt_token=result["token"],
                refresh_token=result["token"],
                feed_token=result["token"],
                issued_at=datetime.now(timezone.utc),
                client_code=result.get("userID") or settings.xts_md_app_key,
            )
            await self._persist(tokens)
            with self._lock:
                self._tokens = tokens
                # A freshly minted token is by definition not yet rejected.
                self._broker_rejected_at = None
            self._last_login_mono = time.monotonic()
            log.info("xts.login.success", user_id=tokens.client_code, forced=force)
            return tokens

    async def set_tokens(
        self,
        jwt_token: str,
        refresh_token: str,
        feed_token: str,
        client_code: str | None = None,
        issued_at: datetime | None = None,
    ) -> SessionTokens:
        """Inject an externally-acquired token (used by DB restore)."""
        if not jwt_token:
            raise RuntimeError("Cannot set session without a token.")
        tokens = SessionTokens(
            jwt_token=jwt_token,
            refresh_token=refresh_token or jwt_token,
            feed_token=feed_token or jwt_token,
            issued_at=issued_at or datetime.now(timezone.utc),
            client_code=client_code or settings.xts_md_app_key,
        )
        with self._lock:
            self._tokens = tokens
        return tokens

    async def refresh(self) -> SessionTokens:
        """XTS has no refresh endpoint — renewal is a fresh login."""
        return await self.login()

    async def start_refresh_loop(self) -> None:
        """Spawn the background renewal task. Idempotent."""
        if self._refresh_task and not self._refresh_task.done():
            return
        self._stopping.clear()
        self._refresh_task = asyncio.create_task(self._refresh_loop(), name="xts-refresh")

    async def stop(self) -> None:
        self._stopping.set()
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except (asyncio.CancelledError, Exception):
                pass
        with self._lock:
            tok = self._tokens.jwt_token if self._tokens else None
        if tok:
            try:
                await xts_client.logout(tok)
            except Exception as e:  # pragma: no cover - best effort
                log.warning("xts.logout.error", error=str(e))

    async def try_restore_session_from_db(self) -> bool:
        """Load the latest ``auth_sessions`` row and reuse it if still valid.

        Returns True when a non-expired token was restored. On failure clears the
        in-memory token so ``login()`` can run clean.
        """
        try:
            async with session_scope() as s:
                result = await s.execute(
                    text(
                        """
                        SELECT client_code, jwt_token, refresh_token, feed_token, issued_at
                        FROM auth_sessions
                        ORDER BY issued_at DESC
                        LIMIT 1
                        """
                    )
                )
                row = result.mappings().first()
        except Exception as e:
            log.warning("xts.restore_from_db.query_failed", error=str(e))
            return False
        if not row or not row.get("jwt_token"):
            return False
        issued = row.get("issued_at")
        if issued is not None and issued.tzinfo is None:
            issued = issued.replace(tzinfo=timezone.utc)
        try:
            await self.set_tokens(
                row["jwt_token"],
                row["refresh_token"],
                row["feed_token"],
                row["client_code"],
                issued_at=issued,
            )
        except Exception as e:
            log.warning("xts.restore_from_db.failed", error=str(e))
            with self._lock:
                self._tokens = None
            return False
        if self.authenticated:
            log.info("xts.restore_from_db.success", user_id=row["client_code"])
            return True
        # Stored token is past its TTL — drop it so a fresh login runs.
        with self._lock:
            self._tokens = None
        return False

    # ---------------------------------------------------------- internals

    async def _refresh_loop(self) -> None:
        log.info("xts.refresh_loop.started", interval_seconds=REFRESH_EVERY.total_seconds())
        wait_s = REFRESH_EVERY.total_seconds()
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=wait_s)
                if self._stopping.is_set():
                    break
            except asyncio.TimeoutError:
                pass
            try:
                async for attempt in AsyncRetrying(
                    stop=stop_after_attempt(3),
                    wait=wait_exponential(multiplier=2, min=2, max=30),
                    retry=retry_if_exception_type(Exception),
                    reraise=True,
                ):
                    with attempt:
                        await self.login()
                # The new token invalidated the feed socket's previous token (XTS
                # allows one valid token per appKey), so the live socket is now
                # bound to a dead token. Drop it so the feed reconnects + re-subscribes
                # under the fresh token — otherwise the 12h refresh silently stops
                # all ticks until the next manual login.
                self._nudge_feed_reconnect()
                wait_s = REFRESH_EVERY.total_seconds()
            except Exception as e:
                # Come back in minutes, not another REFRESH_EVERY.
                wait_s = REFRESH_RETRY_AFTER_FAILURE.total_seconds()
                log.error("xts.refresh.exhausted", error=str(e), retry_in_s=wait_s)

    def _nudge_feed_reconnect(self) -> None:
        """Ask the live feed (if any) to reconnect so it re-handshakes with the
        current token. Lazy import avoids an auth -> ingest import cycle."""
        try:
            from ..runtime import get_runtime

            feed = get_runtime().feed_client
            if feed is not None:
                feed.nudge_reconnect()
                log.info("xts.refresh.feed_reconnect_nudged")
        except Exception as e:
            log.warning("xts.refresh.feed_nudge_failed", error=str(e))

    async def _persist(self, tokens: SessionTokens) -> None:
        async with session_scope() as s:
            await s.execute(
                text(
                    """
                    INSERT INTO auth_sessions
                        (client_code, jwt_token, refresh_token, feed_token, issued_at, expires_at)
                    VALUES
                        (:client_code, :jwt, :refresh, :feed, :issued, :expires)
                    """
                ),
                {
                    "client_code": tokens.client_code,
                    "jwt": tokens.jwt_token,
                    "refresh": tokens.refresh_token,
                    "feed": tokens.feed_token,
                    "issued": tokens.issued_at,
                    "expires": tokens.expires_at,
                },
            )


_singleton: MarketDataSession | None = None


def get_session_manager() -> MarketDataSession:
    """Return process-wide singleton."""
    global _singleton
    if _singleton is None:
        _singleton = MarketDataSession()
    return _singleton
