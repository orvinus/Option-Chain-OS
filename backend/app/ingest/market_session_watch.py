"""IST regular-session boundary: nudge broker WebSocket reconnect after open."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ..core.logging import get_logger
from ..core.time_utils import is_nse_regular_session_open

if TYPE_CHECKING:
    from .ws_client import OptionFeedClient

log = get_logger("market_session_watch")


async def run_nse_session_open_watch(feed: OptionFeedClient) -> None:
    """Poll every minute; on transition to session-open, reconnect the feed."""
    prev = False
    while True:
        try:
            open_now = is_nse_regular_session_open()
            if open_now and not prev:
                log.info("market.session_open_ist_nudging_ws")
                feed.nudge_reconnect()
            prev = open_now
            await asyncio.sleep(60.0)
        except asyncio.CancelledError:
            return
        except Exception as e:  # pragma: no cover - keep task alive
            log.warning("market_session_watch.error", error=str(e))
            await asyncio.sleep(60.0)
