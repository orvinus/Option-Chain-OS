"""Recovery authority for the TrueData feed.

NOT a port of ``session_steward.py``. That file's ~560 lines encode one premise —
*an XTS relogin kills the previous session, so relogin is the universal cure and
the only danger is logging in too often*. Under TrueData the premise inverts: a
relogin against a live session is REJECTED and does nothing, while after a dirty
disconnect every aggressive retry re-enters a ~60s lockout. Ported unmodified,
that machinery becomes a lockout generator.

What survives from the XTS steward, verbatim, because it is vendor-agnostic:

* the outcome-based health rule — freshness of WS-ORIGIN rows, never
  "is the socket object connected", which flaps;
* the market-hours watch window, pre-open warmup and post-open grace;
* ``ws_feed_healthy`` / ``ws_feed_stable`` (the REST failover poller's gate);
* the ``os._exit(1)`` backstop, with one mandatory addition (see ``_do_exit``).

What is deleted: the 90s login floor, the burst budget, the storm breaker, the
boot-storm heuristic, the login circuit breaker, the second-client detector, the
token-rotation rungs and the daily 08:35 rotation — roughly 380 lines whose only
purpose was surviving destructive-login semantics that no longer exist.

What is new: the wedge rung, and the rule that it fires ONLY on the wedge
signature. Burning a 75-second cool-down on a plain network blip would convert a
two-second reconnect into a minute of downtime.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime

from ..auth.td_session import SessionState, get_td_session
from ..core.config import settings
from ..core.holidays import is_nse_holiday
from ..core.logging import get_logger
from ..core.notify import notify
from ..core.time_utils import IST, market_close_today, market_open_today
from ..runtime import get_runtime

log = get_logger("td_steward")

CHECK_INTERVAL_S = 15.0        # was 30 under XTS; the heartbeat lets us halve it
STALE_AFTER_S = 150.0          # WS-origin flush age in-session (was 180)
ESCALATE_MIN_INTERVAL_S = 90.0  # MUST be >= the wedge cool-down (see below)
PRE_OPEN_WARMUP_S = 45 * 60
POST_OPEN_GRACE_S = 2 * 60
VERIFY_IN_SESSION_S = 180.0
VERIFY_OFF_SESSION_S = 120.0
VERIFY_POLL_S = 5.0
SUICIDE_AFTER_S = 900.0        # > a full wedge cycle, or we exit mid-recovery
STUCK_IN_CONNECT_S = 120.0


class TrueDataSteward:
    def __init__(self) -> None:
        self._step = 0
        self._last_action_at = 0.0
        self._unhealthy_since: float | None = None
        self._explicit_request: str | None = None
        self._wake = asyncio.Event()
        self.last_check_at: datetime | None = None
        self.ws_feed_healthy: bool | None = None
        self._stable_since: float | None = None

    # ---------------------------------------------------------- public API
    @property
    def ws_feed_stable(self) -> bool:
        """Feed has been healthy long enough for the REST failover to stand down.

        Same contract the universe poller already consumes, so its gate needs no
        change across the migration.
        """
        if not self.ws_feed_healthy or self._stable_since is None:
            return False
        return (time.monotonic() - self._stable_since) >= settings.poller_failover_linger_s

    @property
    def unhealthy_since_s(self) -> float | None:
        if self._unhealthy_since is None:
            return None
        return time.monotonic() - self._unhealthy_since

    @property
    def current_step(self) -> int:
        return self._step

    def request_recovery(self, reason: str) -> None:
        self._explicit_request = reason
        self._wake.set()

    async def request_recovery_and_wait(self, reason: str, timeout_s: float) -> bool:
        self.request_recovery(reason)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            if self._healthy_verdict(get_runtime().feed_client):
                return True
        return False

    # ------------------------------------------------------------ the loop
    async def run(self) -> None:
        if settings.run_mode != "live":
            return
        if settings.feed_vendor != "truedata":
            return
        log.info("td_steward.started", check_interval_s=CHECK_INTERVAL_S)
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("td_steward.tick_error", error=str(e))
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=CHECK_INTERVAL_S)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        rt = get_runtime()
        self.last_check_at = datetime.now(IST)
        feed = rt.feed_client

        if not self._within_watch_window():
            self.ws_feed_healthy = None
            self._unhealthy_since = None
            return

        healthy = self._healthy_verdict(feed)
        self.ws_feed_healthy = healthy

        if healthy:
            if self._stable_since is None:
                self._stable_since = time.monotonic()
            if self._unhealthy_since is not None:
                log.info("td_steward.recovered",
                         down_for_s=round(self.unhealthy_since_s or 0, 1), step=self._step)
                notify("td_steward_recovered", "✅ TrueData feed recovered.")
                # The minutes lost while the socket was down are gone from the
                # live path — ask the gap-fill task to pull them from the
                # vendor's same-day bars right away (not on its next 5-min tick).
                try:
                    from .gapfill import request_catchup

                    request_catchup("feed_recovered")
                except Exception:  # noqa: BLE001
                    pass
            self._unhealthy_since = None
            self._step = 0
            self._explicit_request = None
            return

        self._stable_since = None
        if self._unhealthy_since is None:
            self._unhealthy_since = time.monotonic()
            log.warning("td_steward.unhealthy", state=get_td_session().state.value)

        # A dead supervisor cannot self-heal — jump straight to the rebuild rung.
        if feed is not None and not feed.supervisor_alive:
            self._step = 2
            await self._escalate(force_gate=True)
            return

        # An explicit request (login endpoint, symbol switch) outranks the
        # freshness metric: the caller KNOWS something is wrong.
        if self._explicit_request:
            reason, self._explicit_request = self._explicit_request, None
            log.info("td_steward.explicit_request", reason=reason)
            await self._escalate(force_gate=True)
            return

        if self._should_exit():
            await self._do_exit()
            return
        await self._escalate()

    # ---------------------------------------------------------- the verdict
    def _healthy_verdict(self, feed) -> bool:
        sess = get_td_session()
        if self._in_warmup():
            return feed is not None and feed.supervisor_alive and \
                sess.state in (SessionState.LIVE, SessionState.CONNECTING)

        # The heartbeat check is the migration's biggest monitoring win. Under
        # XTS a dead socket was invisible for up to STALE_AFTER_S, because rows
        # only appear per persist bucket and tick silence is normal off-session.
        # TrueData's 5-6s heartbeat separates "no trades" from "no socket", so
        # detection drops from ~150s to ~25s and works outside market hours.
        if feed is not None:
            hb = getattr(feed, "heartbeat_age_s", None)
            if hb is not None and hb > 25.0:
                return False
            if getattr(feed, "stuck_in_connect_s", 0.0) > STUCK_IN_CONNECT_S:
                return False

        age = self._data_age_s()
        return age is not None and age <= STALE_AFTER_S

    @staticmethod
    def _data_age_s() -> float | None:
        """Age of the last WS-ORIGIN flush.

        Deliberately not last_flush_at: REST failover rows keep the dashboard
        alive and must never be able to hide a dead socket from this ladder.
        """
        rt = get_runtime()
        if rt.last_ws_flush_at is None:
            return None
        return (datetime.now(rt.last_ws_flush_at.tzinfo) - rt.last_ws_flush_at).total_seconds()

    @staticmethod
    def _within_watch_window() -> bool:
        now = datetime.now(IST)
        if now.weekday() >= 5 or is_nse_holiday(now.date()):
            return False
        return market_open_today().timestamp() - PRE_OPEN_WARMUP_S <= now.timestamp() \
            <= market_close_today().timestamp()

    @staticmethod
    def _in_warmup() -> bool:
        now = datetime.now(IST).timestamp()
        return now < market_open_today().timestamp() + POST_OPEN_GRACE_S

    # ----------------------------------------------------------- escalation
    @staticmethod
    def _wedge_signature(feed) -> bool:
        """Does the evidence justify burning the ~75s cool-down?

        Only these three. A plain network failure, a proxy hiccup or a closed
        socket must NOT qualify: logoutRequest would then turn a two-second
        reconnect into a minute of enforced downtime, every time.
        """
        sess = get_td_session()
        return (
            sess.state is SessionState.WEDGED
            or sess.consecutive_rejections >= 1
            or (feed is not None and getattr(feed, "stuck_in_connect_s", 0.0) > STUCK_IN_CONNECT_S)
        )

    async def _escalate(self, force_gate: bool = False) -> None:
        now = time.monotonic()
        if not force_gate and (now - self._last_action_at) < ESCALATE_MIN_INTERVAL_S:
            return
        sess = get_td_session()
        rt = get_runtime()
        feed = rt.feed_client

        # PARKED conditions: do nothing, do NOT advance the step, do not exit.
        # Acting here cannot help and actively harms.
        if sess.state is SessionState.COOLDOWN:
            log.info("td_steward.parked", why="wedge_cooldown",
                     remaining_s=round(sess.cooldown_remaining_s, 1))
            return
        from ..core import proxy_health
        if not proxy_health.is_up():
            # The vendor is unreachable at the network layer. Reconnecting,
            # rebuilding or exiting all fail identically; only the host can fix
            # this, so alert and wait rather than thrash.
            log.error("td_steward.parked", why="proxy_down")
            notify("td_proxy_down",
                   "⚠️ TrueData proxy (WARP) unreachable — feed cannot connect. "
                   "Check warp-svc and warp-socks-bridge on the host.")
            return

        self._last_action_at = now
        step = self._step

        if step == 0:
            if feed is not None:
                feed.nudge_reconnect()
            log.warning("td_steward.escalate", step=0, action="nudge_reconnect")
            self._step = 1 if self._wedge_signature(feed) else 2
            return

        if step == 1:
            log.warning("td_steward.escalate", step=1, action="wedge_recovery")
            await sess.logout_request("steward ladder")
            self._step = 2
            return

        if step == 2:
            log.warning("td_steward.escalate", step=2, action="rebuild_client")
            from .feed_factory import rebuild_feed_client
            try:
                await rebuild_feed_client("td steward escalation")
            except Exception as e:
                log.error("td_steward.rebuild_failed", error=str(e))
            # Wrap back to the wedge rung rather than the nudge: if a full
            # rebuild did not fix it, a nudge certainly will not.
            self._step = 1
            return

    # ------------------------------------------------------------- backstop
    def _should_exit(self) -> bool:
        down = self.unhealthy_since_s
        if down is None or down < SUICIDE_AFTER_S:
            return False
        sess = get_td_session()
        # Never exit mid-recovery: a restart during the cool-down is a dirty
        # disconnect that re-wedges the session and starts the clock again.
        if sess.state is SessionState.COOLDOWN:
            return False
        return True

    async def _do_exit(self) -> None:
        """Last resort — but log out FIRST.

        The XTS steward calls os._exit(1) directly, which bypasses main.py's
        shutdown handler and therefore the feed's stop(). Under XTS that was
        harmless. Under TrueData it is a guaranteed wedge: the process dies
        dirty, Docker restarts it immediately, and the fresh process connects
        into its own "User Already Connected" window. Every steward suicide
        would cost a further ~60s of enforced downtime on top of the outage it
        was trying to end.
        """
        sess = get_td_session()
        log.error("td_steward.exiting", down_for_s=round(self.unhealthy_since_s or 0, 1))
        notify("td_steward_process_exit",
               "🔁 TrueData feed unrecoverable — exiting for a container restart "
               "(session logged out first).")
        try:
            await asyncio.wait_for(sess.logout_request("pre_exit", force=True), timeout=20.0)
        except Exception as e:
            log.error("td_steward.exit_logout_failed", error=str(e))
        await asyncio.sleep(1.0)
        os._exit(1)


_singleton: TrueDataSteward | None = None


def get_td_steward() -> TrueDataSteward:
    global _singleton
    if _singleton is None:
        _singleton = TrueDataSteward()
    return _singleton
