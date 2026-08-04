"""Liveness / introspection endpoint."""
from __future__ import annotations

from fastapi import APIRouter

from ..auth import get_session_manager
from ..core.config import settings
from ..core.time_utils import MARKET_CLOSE, MARKET_OPEN, is_nse_regular_session_open, now_ist
from ..runtime import get_runtime
from .schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    rt = get_runtime()
    sess = get_session_manager()
    feed = rt.feed_client
    feed_connected = (
        bool(getattr(feed, "is_feed_connected", False)) if feed is not None else False
    )
    return HealthResponse(
        status="ok",
        authenticated=sess.authenticated,
        latest_spot=rt.latest_spot,
        tokens_subscribed=len(rt.tokens),
        last_flush_at=rt.last_flush_at.isoformat() if rt.last_flush_at else None,
        expiries=[e.isoformat() for e in rt.expiries],
        run_mode=settings.run_mode,
        now_ist=now_ist().isoformat(),
        nse_session_open=is_nse_regular_session_open(),
        session_open_ist=MARKET_OPEN.strftime("%H:%M"),
        session_close_ist=MARKET_CLOSE.strftime("%H:%M"),
        feed_connected=feed_connected,
        active_symbol=rt.active_symbol,
        poller_enabled=rt.universe_poller is not None,
        poller_last_sweep_at=rt.poller_last_sweep_at.isoformat() if rt.poller_last_sweep_at else None,
        poller_last_ticks=rt.poller_last_ticks,
    )
