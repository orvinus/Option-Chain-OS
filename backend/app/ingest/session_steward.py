"""SessionSteward — the single recovery authority for the broker session + feed.

Replaces ``feed_watchdog.py`` (and the 12h token-refresh loop). Lessons carried
in from two production outages:

2026-08-04: the option universe went empty, the socket never opened, and every
reactive recovery path was starved of its trigger. Recovery must be OUTCOME-based
("are WS rows landing?"), not event-based.

2026-08-06: five independent actors each rotated the single-session XTS token
with no coordinator — a ~6s login-storm livelock for 14.6 hours. The watchdog ran
the whole time but its relogin rung was mathematically unreachable (escalations
gated to every 4th 30s check landed only on odd counts), its universe refresh
logged ``master_rows=0`` as success, and its nudges were no-ops against an
already-disconnected feed. Every one of those defects is addressed structurally:

* ONE component rotates the token (this one), through the rate-floor/circuit
  policy inside ``MarketDataSession``; everything else calls
  ``request_recovery()`` and waits.
* The ladder uses an explicit step index — kick → rotate → universe rebuild →
  full client rebuild — with each step's OUTCOME validated, and a final
  ``os._exit(1)`` backstop so Docker's restart policy resurrects a clean process
  (which restores the still-valid token: a ZERO-login restart).
* Rotation verification is tier-aware by the clock: in-session success = WS rows
  landing; off-session success = the socket handshake + subscribe completing
  (rows cannot exist before the open — demanding them made the old design a
  scheduled morning self-storm).
* Token lifecycle runs on the wall clock: one rotation daily at a pre-open
  instant (default 08:35 IST) plus a >22h TTL backstop — never an interval timer
  drifting into the evening, which is what killed the feed at 19:55 and 19:09.
* A second-client detector: rotations that verify and then die within ~2 minutes,
  repeatedly, mean something else keeps stealing the session (a local
  RUN_MODE=live stack, an old cached dashboard tab) — park and page instead of
  burning the login budget.
"""
from __future__ import annotations

import asyncio
import os
import time as time_mod
from datetime import datetime, time, timedelta, timezone
from typing import Optional

from ..auth import get_session_manager
from ..auth.market_session import LoginCircuitOpen, LoginRateLimited
from ..core.config import settings
from ..core.logging import get_logger
from ..core.notify import notify
from ..core.time_utils import (
    IST,
    is_nse_regular_session_open,
    market_close_today,
    market_open_today,
    now_ist,
)
from ..runtime import get_runtime

log = get_logger("steward")

CHECK_INTERVAL_S = 30.0
# Rows are expected every PERSIST_BUCKET; three missed minutes during an open
# session means the numbers on screen have stopped moving.
STALE_AFTER_S = 180.0
# Min seconds between ladder actions — recovery must never become its own storm.
ESCALATE_MIN_INTERVAL_S = 90.0
# Start watching this long before the open so the machinery is proven before
# 09:15 rather than taking its first escalation during live trading.
PRE_OPEN_WARMUP = timedelta(minutes=45)
# The first minutes after the open get warmup semantics too — rows can lag the
# bell without anything being wrong.
POST_OPEN_GRACE = timedelta(minutes=2)
# Rotation verification windows (see _verify_rotation).
VERIFY_IN_SESSION_S = 180.0
VERIFY_OFF_SESSION_S = 120.0
VERIFY_POLL_S = 5.0
# os._exit backstop: only after this long unhealthy AND a full ladder pass.
SUICIDE_AFTER_S = 600.0
# Second-client signature: this many consecutive verified-then-died-quickly
# rotations opens the circuit with a "second client suspected" reason.
SECOND_CLIENT_DEATHS = 3
POST_VERIFY_DEATH_WINDOW_S = 120.0
# TTL backstop: rotate whenever the token is older than this, any hour — a token
# dying mid-session is strictly worse than one bounded rotation gap.
TOKEN_ROTATE_AFTER_H = 22.0


def _parse_hhmm(raw: str, fallback: time) -> time:
    try:
        hh, mm = (int(p) for p in raw.strip().split(":", 1))
        return time(hh, mm)
    except (ValueError, AttributeError):
        return fallback


def _within_watch_window(ts: datetime | None = None) -> bool:
    """Weekday, (open − warmup) → close. No evening tail: scheduled rotations are
    pre-open now, so nothing needs watching after the close — and post-close
    "staleness" is just the market being shut."""
    ts_ist = (ts or now_ist()).astimezone(IST)
    if ts_ist.weekday() >= 5:
        return False
    open_at = market_open_today(ts_ist)
    close_at = market_close_today(ts_ist)
    return open_at - PRE_OPEN_WARMUP <= ts_ist <= close_at


def _in_warmup(ts: datetime | None = None) -> bool:
    """The stretch where the machinery should be UP but rows cannot exist yet."""
    ts_ist = (ts or now_ist()).astimezone(IST)
    open_at = market_open_today(ts_ist)
    return open_at - PRE_OPEN_WARMUP <= ts_ist < open_at + POST_OPEN_GRACE


def _data_age_seconds() -> float | None:
    """Seconds since the last WS-ORIGIN flush, or None if nothing has flushed yet.

    Deliberately not ``last_flush_at``: the failover poller advances that one,
    and REST rows keeping the dashboard alive must never hide a dead socket
    from the recovery ladder.
    """
    last = get_runtime().last_ws_flush_at
    if last is None:
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last).total_seconds()


class SessionSteward:
    """Owns: outcome-based feed recovery, token rotation schedule, both alerts."""

    def __init__(self) -> None:
        self._recovery_requested = asyncio.Event()
        self._recovery_reasons: list[str] = []
        self._last_check_at: Optional[datetime] = None
        # Episode = a contiguous stretch of unhealthy verdicts.
        self._unhealthy_since: Optional[float] = None
        self._step = 0
        self._last_action_at = 0.0
        self._episode_had_rebuild = False
        # Poller-facing WS health (True outside the watch window so failover idles).
        self._ws_healthy = True
        self._ws_healthy_since = 0.0
        # Scheduled-rotation bookkeeping.
        self._last_rotation_ist_date = None
        # Second-client detection.
        self._last_verify_ok_at: Optional[float] = None
        self._post_verify_deaths = 0

    # ------------------------------------------------ public surface

    def request_recovery(self, reason: str) -> None:
        """Any actor may ASK for recovery; only the steward acts. Wakes the loop."""
        self._recovery_reasons.append(reason)
        del self._recovery_reasons[:-20]
        log.info("steward.recovery_requested", reason=reason)
        self._recovery_requested.set()

    async def request_recovery_and_wait(self, reason: str, timeout_s: float = 15.0) -> bool:
        """For the login endpoint: request, then wait briefly for a healthy verdict."""
        self.request_recovery(reason)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            await asyncio.sleep(1.0)
            if self.ws_feed_healthy and get_session_manager().authenticated:
                return True
        return self.ws_feed_healthy and get_session_manager().authenticated

    @property
    def ws_feed_healthy(self) -> bool:
        """Raw current verdict (True outside the watch window)."""
        return self._ws_healthy

    @property
    def ws_feed_stable(self) -> bool:
        """Healthy AND has been for the failover linger — the poller's gate, so
        failover keeps covering the first minutes after a recovery instead of
        thrashing on the ~83s gateway socket cycle."""
        if not self._ws_healthy:
            return False
        return (
            time_mod.monotonic() - self._ws_healthy_since
            >= settings.poller_failover_linger_s
        )

    @property
    def last_check_at(self) -> Optional[datetime]:
        return self._last_check_at

    @property
    def unhealthy_since_s(self) -> Optional[float]:
        if self._unhealthy_since is None:
            return None
        return asyncio.get_event_loop().time() - self._unhealthy_since

    @property
    def current_step(self) -> int:
        return self._step

    # ------------------------------------------------ the loop

    async def run(self) -> None:
        if settings.run_mode != "live":
            return
        log.info(
            "steward.started",
            check_interval_s=CHECK_INTERVAL_S,
            stale_after_s=STALE_AFTER_S,
            daily_rotation_ist=settings.daily_token_refresh_ist,
        )
        while True:
            try:
                requested = await self._sleep_or_request(CHECK_INTERVAL_S)
                self._last_check_at = datetime.now(timezone.utc)
                await self._maybe_scheduled_rotation()

                rt = get_runtime()
                feed = rt.feed_client
                # A dead supervisor task cannot be kicked, rotated at, or given a
                # fresh universe — only a rebuild helps. Jump the ladder.
                if feed is not None and not feed.supervisor_alive:
                    log.error("steward.supervisor_dead")
                    self._open_episode()
                    self._step = 3
                    await self._escalate(force_gate=True)
                    continue

                if not _within_watch_window():
                    self._set_healthy(True)
                    self._reset_episode()
                    continue

                healthy = self._healthy_verdict(feed)
                # An explicit recovery request outranks the freshness metric.
                # MEASURED 2026-08-11: with the options broadcast dead after a
                # gateway socket cycle, the spot row alone keeps
                # _data_age_seconds() green — so requests from the feed's rearm
                # path were evaluated as "healthy, nothing to do" and ignored
                # all morning. A requester states a concrete broker-visible
                # problem; believe it. Kick cannot fix a dead broadcast, so
                # enter the ladder at the rotate rung (jump applied after
                # _open_episode, which resets the step) — the login floor and
                # burst budget still bound the actual rotation rate.
                jump_to_rotate = requested and not _in_warmup()
                if jump_to_rotate:
                    healthy = False
                if healthy:
                    self._set_healthy(True)
                    if self._unhealthy_since is not None:
                        dur = asyncio.get_running_loop().time() - self._unhealthy_since
                        log.info("steward.recovered", after_s=round(dur))
                        notify("steward_recovered", f"✅ Feed recovered after {round(dur / 60)} min.")
                    self._reset_episode()
                    # Proof the current token delivers — closes the circuit and
                    # resets the storm counter.
                    get_session_manager().mark_feed_healthy()
                    continue

                self._note_possible_post_verify_death()
                self._set_healthy(False)
                self._open_episode()
                if jump_to_rotate:
                    # Skip the kick rung; force_gate bypasses the 90 s
                    # escalate spacing so the rotation keeps pace with the
                    # ~67 s gateway cycles — LOGIN_FLOOR_S still applies.
                    self._step = max(self._step, 1)
                await self._escalate(force_gate=jump_to_rotate)
                if self._should_exit():
                    await self._do_exit()
            except asyncio.CancelledError:
                log.info("steward.stopped")
                return
            except Exception as e:  # pragma: no cover — must outlive anything
                log.warning("steward.error", error=str(e))

    async def _sleep_or_request(self, timeout: float) -> bool:
        """Sleep until the next check; True when woken by an explicit request."""
        requested = True
        try:
            await asyncio.wait_for(self._recovery_requested.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            requested = False
        self._recovery_requested.clear()
        return requested

    # ------------------------------------------------ health

    def _healthy_verdict(self, feed) -> bool:
        if _in_warmup():
            # Rows CANNOT exist yet — judging by data age here made the old
            # design escalate every single morning. Machinery-up is the bar:
            # session valid + supervisor alive. Deep verification pre-open is
            # the daily rotation's job.
            return (
                feed is not None
                and feed.supervisor_alive
                and get_session_manager().authenticated
            )
        age = _data_age_seconds()
        # Deliberately NOT feed_connected (the ~83s gateway cycle flaps it while
        # perfectly healthy) and NOT len(rt.tokens) (kept last-known-good, so it
        # reads 46 with zero live subscriptions).
        return age is not None and age <= STALE_AFTER_S

    def _set_healthy(self, healthy: bool) -> None:
        if healthy and not self._ws_healthy:
            self._ws_healthy_since = time_mod.monotonic()
        if healthy and self._ws_healthy_since == 0.0:
            self._ws_healthy_since = time_mod.monotonic()
        self._ws_healthy = healthy

    def _open_episode(self) -> None:
        if self._unhealthy_since is None:
            self._unhealthy_since = asyncio.get_event_loop().time()
            self._step = 0
            self._episode_had_rebuild = False
            reasons = ", ".join(self._recovery_reasons[-3:]) or "stale data"
            log.warning("steward.unhealthy_start", reasons=reasons)
            notify(
                "steward_unhealthy",
                f"⚠️ Feed unhealthy ({reasons}) — steward escalating: "
                "kick → rotate → universe → rebuild.",
            )

    def _reset_episode(self) -> None:
        self._unhealthy_since = None
        self._step = 0
        self._episode_had_rebuild = False

    def _note_possible_post_verify_death(self) -> None:
        """Verified-then-died-quickly, repeatedly = a second client on this appKey."""
        if self._last_verify_ok_at is None:
            return
        since = asyncio.get_event_loop().time() - self._last_verify_ok_at
        self._last_verify_ok_at = None
        if since <= POST_VERIFY_DEATH_WINDOW_S:
            self._post_verify_deaths += 1
            log.warning(
                "steward.post_verify_death",
                seconds_after_verify=round(since), count=self._post_verify_deaths,
            )
            if self._post_verify_deaths >= SECOND_CLIENT_DEATHS:
                self._post_verify_deaths = 0
                get_session_manager().park_rotations(
                    "second client suspected: the feed verifies healthy after "
                    "every rotation and then dies within 2 minutes — something "
                    "else keeps stealing this appKey's single session (a local "
                    "RUN_MODE=live stack? an old cached dashboard tab?)"
                )
        else:
            self._post_verify_deaths = 0

    # ------------------------------------------------ ladder

    async def _escalate(self, force_gate: bool = False) -> None:
        loop_now = asyncio.get_running_loop().time()
        if not force_gate and loop_now - self._last_action_at < ESCALATE_MIN_INTERVAL_S:
            return
        self._last_action_at = loop_now
        rt = get_runtime()
        feed = rt.feed_client
        sess = get_session_manager()

        step = self._step
        if step == 0 and not sess.authenticated:
            # Kicking a feed whose token is dead is pointless — skip to rotation.
            step = 1

        if step == 0:
            log.warning("steward.escalate", step="kick")
            if feed is not None:
                feed.nudge_reconnect()
            self._step = 1
        elif step == 1:
            log.error("steward.escalate", step="rotate_token")
            parked = await self._rotate("ladder")
            if parked == "parked":
                # Circuit open: stay on this rung; probes go through at the
                # session manager's own cadence. No suicide while parked (a
                # restart cannot fix what the circuit is protecting against).
                return
            self._step = 2
        elif step == 2:
            log.error("steward.escalate", step="rebuild_universe")
            try:
                from ..market.scripmaster import get_scripmaster

                rows = await asyncio.wait_for(
                    get_scripmaster(rt.active_symbol, force_refresh=True), timeout=90.0
                )
                if rows:
                    log.info("steward.universe_refreshed", symbol=rt.active_symbol, master_rows=len(rows))
                else:
                    # The old watchdog logged master_rows=0 as success. Zero rows
                    # is a FAILURE — say so and keep climbing.
                    log.error("steward.universe_refresh_empty", symbol=rt.active_symbol)
            except Exception as e:
                log.error("steward.universe_refresh_failed", error=str(e))
            finally:
                # The old code skipped the nudge when the refresh raised —
                # guaranteeing the fresh universe (or retry) was never used.
                if feed is not None:
                    feed.nudge_reconnect()
            self._step = 3
        else:
            log.error("steward.escalate", step="rebuild_client")
            try:
                from .feed_factory import rebuild_feed_client

                await rebuild_feed_client("steward escalation")
                self._episode_had_rebuild = True
            except Exception as e:
                log.error("steward.rebuild_failed", error=str(e))
            # Wrap back to rotation — never to the kick (if a rebuild didn't fix
            # it, a kick certainly won't).
            self._step = 1

    # ------------------------------------------------ rotation + verification

    async def _rotate(self, reason: str) -> str:
        """Rotate the token through the policy, reconnect, verify the outcome.

        Returns "ok", "failed", or "parked" (circuit open — do not advance)."""
        sess = get_session_manager()
        try:
            before = sess.last_login_at
            tokens = await sess.login(force=True, actor=f"steward:{reason}")
            minted = before is None or tokens.issued_at != before
        except (LoginCircuitOpen, LoginRateLimited) as e:
            log.warning("steward.rotation_parked", reason=str(e))
            notify("steward_parked", f"⏸ Rotation parked (circuit open): {e}")
            return "parked"
        except Exception as e:
            log.error("steward.rotation_failed", error=str(e))
            return "failed"
        feed = get_runtime().feed_client
        if feed is not None and minted:
            # The fresh token invalidated the socket's old one — reconnect NOW.
            feed.nudge_reconnect()
        ok = await self._verify_rotation()
        if ok:
            self._last_rotation_ist_date = now_ist().date()
            self._last_verify_ok_at = asyncio.get_running_loop().time()
            sess.mark_feed_healthy()
            log.info("steward.rotation_verified", reason=reason)
            return "ok"
        log.warning("steward.rotation_unverified", reason=reason)
        return "failed"

    async def _verify_rotation(self) -> bool:
        """Tier-aware, hour-aware verification.

        In-session: success = a WS-origin flush newer than the rotation instant
        (rows actually landing). Off-session / pre-open: success = the socket
        handshaking AND completing its subscribe (``is_feed_connected`` is set
        only after ``_subscribe_all`` returns) — rows cannot exist before the
        open, and demanding them would fail every 08:35 rotation by design.
        A connected-but-rowless feed in-session extends the window once: progress
        must reset the clock, or slow subscribes get killed by their own rescuer.
        """
        rt = get_runtime()
        in_session = is_nse_regular_session_open() and not _in_warmup()
        budget = VERIFY_IN_SESSION_S if in_session else VERIFY_OFF_SESSION_S
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget
        baseline = rt.last_ws_flush_at
        extended = False
        while loop.time() < deadline:
            await asyncio.sleep(VERIFY_POLL_S)
            feed = rt.feed_client
            if not in_session:
                if feed is not None and feed.is_feed_connected:
                    return True
                continue
            newest = rt.last_ws_flush_at
            if newest is not None and (baseline is None or newest > baseline):
                return True
            if (
                not extended
                and feed is not None
                and feed.is_feed_connected
                and deadline - loop.time() < 30.0
            ):
                extended = True
                deadline += 60.0
        return False

    # ------------------------------------------------ scheduled rotation

    async def _maybe_scheduled_rotation(self) -> None:
        sess = get_session_manager()
        t = now_ist()
        # Daily pre-open rotation: a fresh token minted before the bell, verified
        # (tier-3) before it matters, so no rotation ever needs to happen in the
        # evening again — the trigger of both outages.
        if t.weekday() < 5:
            rotate_at = _parse_hhmm(settings.daily_token_refresh_ist, time(8, 35))
            target = IST.localize(datetime.combine(t.date(), rotate_at))
            open_at = market_open_today(t)
            if target <= t < open_at and self._last_rotation_ist_date != t.date():
                log.info("steward.daily_rotation_due", at=settings.daily_token_refresh_ist)
                await self._rotate("daily_preopen")
                return
        # TTL backstop: the 08:35 rotation failed or the process slept through it.
        # Rotate as soon as the token is old, ANY hour — a token dying mid-session
        # is strictly worse than one bounded rotation gap.
        try:
            issued = sess.tokens.issued_at
        except RuntimeError:
            return
        age_h = (datetime.now(timezone.utc) - issued).total_seconds() / 3600.0
        if age_h >= TOKEN_ROTATE_AFTER_H:
            log.warning("steward.ttl_backstop_rotation", token_age_h=round(age_h, 1))
            await self._rotate("ttl_backstop")

    # ------------------------------------------------ the backstop

    def _should_exit(self) -> bool:
        """All four must hold — extracted so tests can drive the predicate:
        long-unhealthy AND a rebuild was tried AND the circuit is closed (a
        restart cannot fix what the circuit is parked FOR) AND we are in the
        watch window past warmup (an off-hours restart fixes nothing urgent)."""
        if self._unhealthy_since is None or self._episode_had_rebuild is False:
            return False
        loop_now = asyncio.get_event_loop().time()
        if loop_now - self._unhealthy_since < SUICIDE_AFTER_S:
            return False
        if get_session_manager().circuit_state != "closed":
            return False
        return _within_watch_window() and not _in_warmup()

    async def _do_exit(self) -> None:
        log.error(
            "steward.process_exit",
            hint="in-process recovery exhausted (kick/rotate/universe/rebuild all "
            "failed for 10+ min); exiting so Docker restarts a clean process. The "
            "restore path reuses the still-valid token — a zero-login restart.",
        )
        notify(
            "steward_process_exit",
            "🔄 Backend self-restarting: in-process recovery exhausted after 10+ min. "
            "Docker will bring it back; token is reused (no login).",
        )
        await asyncio.sleep(2.0)  # let the alert POST leave the building
        os._exit(1)


_steward: SessionSteward | None = None


def get_steward() -> SessionSteward | None:
    return _steward


def set_steward(steward: SessionSteward) -> None:
    global _steward
    _steward = steward
