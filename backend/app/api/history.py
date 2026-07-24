"""GET /api/history-dates — IST trading days that have stored data.

Feeds the frontend date picker so it only offers days the platform actually
recorded (weekends / holidays / pre-ingestion days are excluded). ``ts`` is
stored in UTC, so it must be converted to IST *before* truncating to a date —
a 15:31 IST tick is 10:01 UTC (same calendar day either way) but a late
after-hours tick could otherwise straddle the UTC/IST date boundary.
"""
from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from ..core.db import AsyncSessionLocal
from ._expiry_utils import resolve_expiry
from ._symbol_utils import resolve_fno_symbol
from .schemas import HistoryDatesResponse

router = APIRouter(tags=["history"])

_HISTORY_DATES_SQL = text(
    """
    SELECT DISTINCT (ts AT TIME ZONE 'Asia/Kolkata')::date AS d
    FROM option_oi_snapshots
    WHERE symbol = :symbol AND expiry = :expiry
    ORDER BY d DESC
    """
)


@router.get("/history-dates", response_model=HistoryDatesResponse)
async def history_dates(
    symbol: str | None = None,
    expiry: str | None = None,
) -> HistoryDatesResponse:
    entry = resolve_fno_symbol(symbol)
    e = await resolve_expiry(expiry, symbol=entry.symbol)
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(_HISTORY_DATES_SQL, {"symbol": entry.symbol, "expiry": e})
        ).mappings().all()
    return HistoryDatesResponse(
        symbol=entry.symbol,
        expiry=e.isoformat(),
        dates=[r["d"].isoformat() for r in rows],
    )
