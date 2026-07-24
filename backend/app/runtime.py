"""Process-wide runtime state shared between the ingest pipeline and the API.

This avoids passing the same set of objects through every FastAPI dependency.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Optional

from .core.config import settings
from .ingest.types import Tick
from .market.scripmaster import InstrumentToken


class Runtime:
    def __init__(self) -> None:
        self.tick_queue: "asyncio.Queue[Tick]" = asyncio.Queue(maxsize=100_000)
        self.feed_client: Optional[object] = None  # OptionFeedClient
        self.aggregator: Optional[object] = None  # MinuteAggregator
        self.universe_poller: Optional[object] = None  # UniversePoller (all-symbol REST snapshotter)
        # Poller observability (surfaced on /api/health).
        self.poller_last_sweep_at: Optional[datetime] = None
        self.poller_last_ticks: int = 0
        self.tokens: list[InstrumentToken] = []
        self.expiries: list[date] = []
        self.latest_spot: Optional[float] = None
        self.last_flush_at: Optional[datetime] = None
        self.last_flush_rows: int = 0
        # Active symbol = the underlying currently subscribed on the live WS.
        # Switched via /api/active-symbol; only one is live at a time.
        self.active_symbol: str = (settings.underlying_symbol or "NIFTY").upper()


_singleton: Runtime | None = None


def get_runtime() -> Runtime:
    global _singleton
    if _singleton is None:
        _singleton = Runtime()
    return _singleton
