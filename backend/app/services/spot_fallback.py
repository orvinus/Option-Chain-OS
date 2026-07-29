"""Last-known spot for a symbol from stored snapshots.

Used as the fallback when a live spot quote fails (throttled boot, symbol
switch mid-throttle): the last stored ``underlying`` for the symbol is
accurate to the previous tick/session, unlike a hardcoded constant or the
*previous* symbol's runtime spot — both of which mis-centre the strike
window (observed live: SENSEX universe resolved around NIFTY's 24330 →
zero contracts subscribed, feed wedged).
"""
from __future__ import annotations

from sqlalchemy import text

from ..core.db import AsyncSessionLocal
from ..core.logging import get_logger

log = get_logger("spot_fallback")

_LAST_UNDERLYING_SQL = text(
    "SELECT underlying FROM option_oi_snapshots "
    "WHERE symbol = :symbol AND underlying IS NOT NULL "
    "ORDER BY ts DESC LIMIT 1"
)


async def db_last_underlying(symbol: str) -> float | None:
    try:
        async with AsyncSessionLocal() as s:
            row = (await s.execute(_LAST_UNDERLYING_SQL, {"symbol": symbol})).first()
        return float(row[0]) if row and row[0] is not None else None
    except Exception as e:
        log.warning("spot_fallback.db_error", symbol=symbol, error=str(e))
        return None
