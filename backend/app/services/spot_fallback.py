"""Last-known spot for a symbol from stored snapshots.

Used as the fallback when a live spot quote fails (throttled boot, symbol
switch mid-throttle): the last stored ``underlying`` for the symbol is
accurate to the previous tick/session, unlike a hardcoded constant or the
*previous* symbol's runtime spot — both of which mis-centre the strike
window (observed live: SENSEX universe resolved around NIFTY's 24330 →
zero contracts subscribed, feed wedged).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text

from ..core.db import AsyncSessionLocal
from ..core.logging import get_logger

log = get_logger("spot_fallback")

_LAST_UNDERLYING_SQL = text(
    "SELECT underlying, ts FROM option_oi_snapshots "
    "WHERE symbol = :symbol AND underlying IS NOT NULL "
    "ORDER BY ts DESC LIMIT 1"
)


async def db_last_underlying_with_ts(symbol: str) -> tuple[float, datetime] | None:
    """Last stored underlying for *symbol* together with the timestamp it was seen.

    Callers get the age so they can decide: centring a strike window on a day-old
    spot is fine, but presenting it as a LIVE quote is not.
    """
    try:
        async with AsyncSessionLocal() as s:
            row = (await s.execute(_LAST_UNDERLYING_SQL, {"symbol": symbol})).first()
        if not row or row[0] is None:
            return None
        ts = row[1]
        if getattr(ts, "tzinfo", None) is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return float(row[0]), ts
    except Exception as e:
        log.warning("spot_fallback.db_error", symbol=symbol, error=str(e))
        return None


async def db_last_underlying(symbol: str) -> float | None:
    """Last stored underlying, regardless of age.

    Use ONLY for strike-window centring / universe resolution. Never assign the
    result to ``rt.latest_spot`` or serve it from /api/spot without also carrying
    its timestamp — an arbitrarily old price presented with ``asof=now`` reads as a
    live quote (see ``db_last_underlying_with_ts``).
    """
    res = await db_last_underlying_with_ts(symbol)
    return res[0] if res else None
