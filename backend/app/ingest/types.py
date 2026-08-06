"""Internal data types shared between the WS client and the aggregator."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(slots=True)
class Tick:
    ts: datetime  # tz-aware UTC
    token: str
    symbol: str
    expiry: date
    strike: int
    option_type: str  # 'CE' | 'PE' | 'IDX' (for spot)
    # None when no price is known yet (e.g. an untraded strike, or the first frame
    # after a reconnect wiped the merged state). Persisted as NULL — never 0, which
    # would be indistinguishable from a real quote.
    ltp: float | None
    oi: int
    volume: int
    underlying: float | None = None  # filled in by aggregator from latest spot
    # Which pipeline produced this tick: "ws" (live socket) or "poller" (REST).
    # The session steward judges FEED health on WS-origin data only — REST
    # failover rows keep the dashboard alive but must never mask a dead socket.
    origin: str = "ws"
