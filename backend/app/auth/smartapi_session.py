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


@dataclass
class SessionTokens:
    """Holds the XTS market-data token.

    Field names are kept from the previous AngelOne integration so the
    ``auth_sessions`` persistence and any external callers keep working:
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
        with self._lock:
            return self._tokens is not None and self._tokens.expires_at > datetime.now(timezone.utc)

    # ---------------------------------------------------------- public API

    async def login(self) -> SessionTokens:
        """Perform an XTS market-data login using env appKey/secretKey."""
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
        log.info("xts.login.success", user_id=tokens.client_code)
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
        if settings.auth_mode != "totp":
            return False
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
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=REFRESH_EVERY.total_seconds())
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
            except Exception as e:
                log.error("xts.refresh.exhausted", error=str(e))

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


# Backwards-compatible alias (old name referenced by ``auth/__init__.py``).
SmartApiSession = MarketDataSession

_singleton: MarketDataSession | None = None


def get_session_manager() -> MarketDataSession:
    """Return process-wide singleton."""
    global _singleton
    if _singleton is None:
        _singleton = MarketDataSession()
    return _singleton
