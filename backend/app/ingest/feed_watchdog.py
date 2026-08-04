"""Escalating recovery watchdog: the feed must never sit dead through a session.

Every other recovery mechanism in this codebase is *reactive* — it needs some
specific event to fire (a rejected subscribe, a socket error, a 12h timer). The
2026-08-03/04 outage happened in a gap where none of them could fire at all: the
option universe went empty, so nothing was ever subscribed, so no rejection ever
came back, so nothing re-logged in. The feed spun quietly for a whole session
while ``/api/health`` reported ``authenticated: true``.

This watchdog is *proactive* and outcome-based. It does not care WHY the feed is
down; it only asks the one question that matters — "are rows still landing?" — and
escalates until the answer is yes:

    1. nudge a reconnect                (cheap; fixes a wedged socket)
    2. force a fresh token + reconnect  (fixes a dead/stolen XTS session)
    3. force a scripmaster refresh      (fixes an empty/stale option universe)

then cycles 2/3 for as long as it stays broken. Each step is gated by
``ESCALATE_MIN_INTERVAL_S`` because XTS is single-session per appKey: every login
invalidates the previous token, so a recovery storm is itself an outage.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from ..auth import get_session_manager
from ..core.config import settings
from ..core.logging import get_logger
from ..core.time_utils import IST, is_nse_regular_session_open, market_open_today, now_ist
from ..runtime import get_runtime

if TYPE_CHECKING:
    from .ws_client import OptionFeedClient

log = get_logger("feed_watchdog")

CHECK_INTERVAL_S = 30.0
# Rows are expected every PERSIST_BUCKET; three missed minutes during an open
# session means the numbers on screen have stopped moving.
STALE_AFTER_S = 180.0
# Never escalate faster than this — a login storm competes with itself for the
# single XTS session and makes the outage permanent instead of fixing it.
ESCALATE_MIN_INTERVAL_S = 120.0
# Start watching this long before the open so the feed is warm at 09:15 rather than
# taking its first escalation cycle during live trading.
PRE_OPEN_WARMUP = timedelta(minutes=15)


def _within_watch_window(ts: datetime | None = None) -> bool:
    """Session hours, plus a warm-up run-up to the open."""
    ts_ist = (ts or now_ist()).astimezone(IST)
    if ts_ist.weekday() >= 5:
        return False
    if is_nse_regular_session_open(ts_ist):
        return True
    open_at = market_open_today(ts_ist)
    return open_at - PRE_OPEN_WARMUP <= ts_ist < open_at


def _data_age_seconds() -> float | None:
    """Seconds since the last persisted flush, or None if nothing has flushed yet."""
    last = get_runtime().last_flush_at
    if last is None:
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last).total_seconds()


async def run_feed_watchdog(feed: OptionFeedClient) -> None:
    """Poll feed health; escalate recovery until rows land again."""
    if settings.run_mode != "live":
        return
    log.info(
        "feed_watchdog.started",
        check_interval_s=CHECK_INTERVAL_S,
        stale_after_s=STALE_AFTER_S,
    )
    failures = 0
    last_escalation = 0.0

    while True:
        try:
            await asyncio.sleep(CHECK_INTERVAL_S)
            if not _within_watch_window():
                failures = 0
                continue

            age = _data_age_seconds()
            connected = feed.is_feed_connected
            tokens = len(get_runtime().tokens)
            # "Healthy" is deliberately about OUTCOME, not connection state: a socket
            # can be open and subscribed while every instrument was rejected.
            healthy = connected and tokens > 0 and age is not None and age <= STALE_AFTER_S
            if healthy:
                if failures:
                    log.info("feed_watchdog.recovered", after_failures=failures, data_age_s=round(age or 0))
                failures = 0
                continue

            failures += 1
            log.warning(
                "feed_watchdog.unhealthy",
                failures=failures,
                feed_connected=connected,
                tokens_subscribed=tokens,
                data_age_s=None if age is None else round(age),
            )

            loop_now = asyncio.get_running_loop().time()
            if loop_now - last_escalation < ESCALATE_MIN_INTERVAL_S:
                continue
            last_escalation = loop_now

            # Step 1 nudges; after that alternate "fresh token" and "fresh universe",
            # which are the two states a nudge alone can never fix.
            if failures == 1:
                log.warning("feed_watchdog.escalate", step="nudge_reconnect")
                feed.nudge_reconnect()
            elif failures % 2 == 0:
                log.error("feed_watchdog.escalate", step="force_relogin")
                try:
                    await get_session_manager().login(force=True)
                    feed.nudge_reconnect()
                    log.info("feed_watchdog.relogin_ok")
                except Exception as e:
                    log.error("feed_watchdog.relogin_failed", error=str(e))
            else:
                log.error("feed_watchdog.escalate", step="force_universe_refresh")
                try:
                    from ..market.scripmaster import get_scripmaster

                    rt = get_runtime()
                    rows = await get_scripmaster(rt.active_symbol, force_refresh=True)
                    log.info(
                        "feed_watchdog.universe_refreshed",
                        symbol=rt.active_symbol, master_rows=len(rows),
                    )
                    feed.nudge_reconnect()
                except Exception as e:
                    log.error("feed_watchdog.universe_refresh_failed", error=str(e))
        except asyncio.CancelledError:
            log.info("feed_watchdog.stopped")
            return
        except Exception as e:  # pragma: no cover - the watchdog must outlive anything
            log.warning("feed_watchdog.error", error=str(e))
