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
    ltp: float
    oi: int
    volume: int
    underlying: float | None = None  # filled in by aggregator from latest spot
