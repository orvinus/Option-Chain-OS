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
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text

from ..core.config import settings
from ..core.db import session_scope
from ..core.logging import get_logger
from ..core.notify import notify
from ..market_data import xts_client

log = get_logger("auth")

# XTS market-data tokens are valid ~24h. Renewal (a fresh login) is scheduled by
# the SessionSteward: daily at a pre-open IST instant + a >22h TTL backstop.
TOKEN_TTL = timedelta(hours=24)

# While the circuit is open, one non-manual "probe" login may go through per this
# interval — enough to notice the broker coming back, slow enough to never storm.
LOGIN_PROBE_INTERVAL_S = 300.0
# Rotations that never produce a verified-healthy feed = a storm in progress
# (something keeps killing the session: a second client, revoked creds, broker
# maintenance). Park instead of feeding it.
STORM_ROTATIONS_LIMIT = 4
# Boot heuristic: this many auth_sessions rows in the last 30 min at startup means
# the PREVIOUS process died mid-storm — boot with the circuit already open so a
# restart can't reset the throttle and resume the storm.
BOOT_STORM_ROWS = 4


class LoginCircuitOpen(RuntimeError):
    """Broker session rotation is parked; only probes / manual overrides proceed."""


class LoginRateLimited(RuntimeError):
    """The rotation burst budget was exhausted; the circuit has been opened."""


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
        # ---- rotation policy state (the login circuit breaker) ----
        # Wall-clock instant of the last login by ANY process on this appKey —
        # seeded from MAX(auth_sessions.issued_at) so a restart cannot reset the
        # floor and resume a login storm.
        self._last_login_wall: Optional[datetime] = None
        self._floor_seeded = False
        # Monotonic timestamps of this process's logins (burst budget window).
        self._login_times: deque[float] = deque()
        # Rotations since the feed was last VERIFIED healthy (steward calls
        # mark_feed_healthy). Rotating without ever getting healthy is the storm
        # signature — each new token kills the socket the previous one enabled.
        self._rotations_since_healthy = 0
        self._circuit_open_at: Optional[float] = None
        self._circuit_reason = ""
        self._last_probe_mono: Optional[float] = None

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

    @property
    def has_usable_token(self) -> bool:
        """TTL-valid token exists, IGNORING the broker-rejected flag.

        The REST poller's gate: while the steward is mid-recovery the flag may be
        set even though the token still answers REST quotes — the one failover
        data path must not be disarmed by exactly the state it exists to cover.
        A genuinely dead token fails the first quote (TokenStale) cheaply.
        """
        with self._lock:
            return (
                self._tokens is not None
                and self._tokens.expires_at > datetime.now(timezone.utc)
            )

    @property
    def last_login_at(self) -> Optional[datetime]:
        return self._last_login_wall

    @property
    def logins_last_hour(self) -> int:
        now_mono = time.monotonic()
        return sum(1 for t in self._login_times if now_mono - t <= 3600.0)

    @property
    def circuit_state(self) -> str:
        return "open" if self._circuit_open_at is not None else "closed"

    @property
    def circuit_reason(self) -> str:
        return self._circuit_reason

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

    async def login(
        self, force: bool = False, manual: bool = False, actor: str = "unknown"
    ) -> SessionTokens:
        """Perform an XTS market-data login, under the ROTATION POLICY.

        XTS is single-session per appKey: every login invalidates the previous
        token, killing whatever socket that token carried. Two production outages
        (2026-08-04, 2026-08-06) were login storms in which independent recovery
        actors rotated the session out from under each other every ~6 seconds for
        a whole trading session. The policy is therefore enforced HERE, where no
        caller can bypass it:

        * **Hard floor** (``LOGIN_FLOOR_S``, default 90s): while a TTL-valid token
          exists, a non-manual login within the floor returns the existing token —
          even with ``force=True``. The floor survives restarts (seeded from
          ``MAX(auth_sessions.issued_at)``). With no usable token there is nothing
          of ours a rotation would kill, so the floor does not apply.
        * **Burst budget** (``LOGIN_BURST_MAX`` per ``LOGIN_BURST_WINDOW_S``):
          exhausting it opens the circuit and raises ``LoginRateLimited``.
        * **Storm breaker**: more than ``STORM_ROTATIONS_LIMIT`` rotations without
          the steward ever calling ``mark_feed_healthy()`` opens the circuit.
        * **Open circuit**: non-manual logins raise ``LoginCircuitOpen``, except
          one probe per ``LOGIN_PROBE_INTERVAL_S``.
        * ``manual=True`` (a human explicitly forcing a new broker session)
          bypasses floor, budget and circuit — but is still recorded.

        ``force`` retains its old meaning of "mint fresh rather than reuse" only
        beyond the floor; ``actor`` is for the audit trail.
        """
        async with self._login_lock:
            now_mono = time.monotonic()
            with self._lock:
                have = self._tokens
            usable = have is not None and have.expires_at > datetime.now(timezone.utc)
            if not manual:
                await self._seed_floor_from_db_once()
                age = self._seconds_since_last_login()
                if usable and age is not None and age < settings.login_floor_s:
                    # force=True does NOT bypass the floor — only manual does. The
                    # 2026-08-06 storm was force=True logins at a ~6s cadence.
                    log.info("xts.login.floored", actor=actor, age_s=round(age, 1), forced=force)
                    return have  # type: ignore[return-value]
                if self._circuit_open_at is not None:
                    if (
                        self._last_probe_mono is not None
                        and now_mono - self._last_probe_mono < LOGIN_PROBE_INTERVAL_S
                    ):
                        raise LoginCircuitOpen(self._circuit_reason)
                    self._last_probe_mono = now_mono
                    log.warning("xts.login.circuit_probe", actor=actor, reason=self._circuit_reason)
                self._prune_login_times(now_mono)
                if len(self._login_times) >= settings.login_burst_max:
                    self._open_circuit(
                        f"burst budget exhausted ({settings.login_burst_max} logins "
                        f"in {int(settings.login_burst_window_s)}s)"
                    )
                    raise LoginRateLimited(self._circuit_reason)
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
            self._last_login_wall = tokens.issued_at
            self._login_times.append(time.monotonic())
            self._rotations_since_healthy += 1
            log.info(
                "xts.login.success",
                user_id=tokens.client_code, forced=force, manual=manual, actor=actor,
                rotations_since_healthy=self._rotations_since_healthy,
            )
            if (
                self._rotations_since_healthy > STORM_ROTATIONS_LIMIT
                and self._circuit_open_at is None
            ):
                self._open_circuit(
                    f"{self._rotations_since_healthy} rotations without a verified-"
                    "healthy feed — each new token kills the socket the previous one "
                    "enabled (second client / revoked creds / broker maintenance?)"
                )
            return tokens

    # ------------------------------------------------ rotation policy internals

    def _seconds_since_last_login(self) -> float | None:
        """Age of the newest login by ANY process (wall clock, DB-seeded)."""
        if self._last_login_wall is None:
            return None
        return (datetime.now(timezone.utc) - self._last_login_wall).total_seconds()

    def _prune_login_times(self, now_mono: float) -> None:
        window = settings.login_burst_window_s
        while self._login_times and now_mono - self._login_times[0] > window:
            self._login_times.popleft()

    async def _seed_floor_from_db_once(self) -> None:
        """One-time DB seed of the rotation floor + the boot-storm heuristic.

        A restart used to reset every in-memory throttle, so the os._exit backstop
        or the VPS sentinel restarting a storming process simply RESUMED the storm
        with a fresh budget. auth_sessions already records every login (one INSERT
        per mint), so the newest row anchors the floor across process lives, and a
        pile of recent rows at boot means the previous process died mid-storm —
        boot with the circuit already open.
        """
        if self._floor_seeded:
            return
        self._floor_seeded = True
        try:
            async with session_scope() as s:
                result = await s.execute(
                    text(
                        """
                        SELECT MAX(issued_at) AS newest,
                               COUNT(*) FILTER (
                                   WHERE issued_at > NOW() - INTERVAL '30 minutes'
                               ) AS recent
                        FROM auth_sessions
                        """
                    )
                )
                row = result.mappings().first()
        except Exception as e:
            log.warning("xts.login.floor_seed_failed", error=str(e))
            return
        if not row:
            return
        newest = row.get("newest")
        if newest is not None:
            if newest.tzinfo is None:
                newest = newest.replace(tzinfo=timezone.utc)
            if self._last_login_wall is None or newest > self._last_login_wall:
                self._last_login_wall = newest
        recent = int(row.get("recent") or 0)
        if recent >= BOOT_STORM_ROWS and self._circuit_open_at is None:
            self._open_circuit(
                f"{recent} logins in the last 30 min found at boot — the previous "
                "process likely died mid-storm; parking rotations"
            )

    def _open_circuit(self, reason: str) -> None:
        if self._circuit_open_at is not None:
            return
        self._circuit_open_at = time.monotonic()
        self._circuit_reason = reason
        log.error("xts.login.circuit_open", reason=reason)
        notify(
            "login_circuit_open",
            f"⛔ Broker login circuit OPEN: {reason}. Rotations parked; probing "
            f"every {int(LOGIN_PROBE_INTERVAL_S / 60)} min. Manual override: "
            "POST /api/auth/login with force_new_token=true.",
        )

    def park_rotations(self, reason: str) -> None:
        """Public circuit-open for detectors outside this module (e.g. the
        steward's second-client signature)."""
        self._open_circuit(reason)

    def mark_feed_healthy(self) -> None:
        """The steward verified rows are flowing on the current token — reset the
        storm counter and close the circuit. The ONLY way the circuit closes
        besides a process restart with a quiet auth_sessions history."""
        was_open = self._circuit_open_at is not None
        self._rotations_since_healthy = 0
        self._circuit_open_at = None
        self._circuit_reason = ""
        if was_open:
            log.info("xts.login.circuit_closed")
            notify("login_circuit_closed", "✅ Broker login circuit closed — feed verified healthy.")

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

    # NOTE: there is deliberately no refresh loop here anymore. The 12h interval
    # timer — anchored to process start, blind to the clock — fired at 19:55 on
    # 2026-08-03 and 19:09 on 2026-08-05, each time rotating the token in the
    # evening and killing the feed overnight with nothing watching. Token renewal
    # now belongs to the SessionSteward: one rotation daily at a pre-open IST
    # instant plus a >22h TTL backstop, both VERIFIED after the fact.

    async def stop(self) -> None:
        """Stop background work. Deliberately does NOT log the token out.

        The old shutdown called /auth/logout, which killed the token at the broker
        while leaving its row as the NEWEST in auth_sessions — so every restart
        restored a corpse the broker had already rejected, `authenticated` lied
        True on pure TTL arithmetic, and startup skipped the fresh login. Leaving
        the token valid is safe under single-session-per-appKey: it cannot enable
        a competing session (any new login invalidates it), and the successor
        process restores it for a ZERO-login restart.
        """
        self._stopping.set()
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except (asyncio.CancelledError, Exception):
                pass

    async def try_restore_session_from_db(self) -> bool:
        """Load the latest ``auth_sessions`` row and reuse it if still valid.

        Returns True when a non-expired token was restored AND the broker did not
        reject it on a cheap validation probe. TTL arithmetic alone is not trust:
        a <24h-old stored token can be broker-dead (daily expiry, single-session
        invalidation), and running a session on one is how /api/health said
        ``authenticated: true`` over a corpse for 13 hours. On failure clears the
        in-memory token so ``login()`` can run clean.
        """
        await self._seed_floor_from_db_once()
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
        if not self.authenticated:
            # Stored token is past its TTL — drop it so a fresh login runs.
            with self._lock:
                self._tokens = None
            return False
        # TTL-valid — now ask the BROKER before trusting it.
        verdict = await xts_client.validate_token(row["jwt_token"])
        if verdict is False:
            log.info(
                "xts.restore_from_db.rejected_by_broker",
                hint="stored token is broker-dead (daily expiry / invalidated by a "
                "newer login elsewhere) — falling through to a fresh login.",
            )
            with self._lock:
                self._tokens = None
            return False
        if verdict is None:
            # Network trouble — inconclusive. Keep the token: a fresh login would
            # fail on the same network, and the steward catches a dead token via
            # data-freshness within minutes anyway.
            log.warning("xts.restore_from_db.unvalidated")
        else:
            self.mark_broker_ok()
        log.info("xts.restore_from_db.success", user_id=row["client_code"], validated=verdict is True)
        return True

    # ---------------------------------------------------------- internals

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
