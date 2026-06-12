"""GET /api/expiries — currently subscribed option expiries for a symbol."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query
from sqlalchemy import text

from ..core.db import AsyncSessionLocal
from ..core.logging import get_logger
from ..runtime import get_runtime
from ._symbol_utils import resolve_symbol
from .schemas import ExpiriesResponse

log = get_logger("api_expiries")
router = APIRouter(tags=["expiries"])

# Guards the background self-heal so a 30s poll storm doesn't pile up re-resolves.
_reresolve_lock = asyncio.Lock()


async def _reresolve_active_universe(symbol: str) -> None:
    """Re-run the active-symbol switch so a throttled-empty first fetch recovers.

    The initial option fetch can come back empty if Angel throttled it; the
    frontend then polls ``/api/expiries`` every 30s but that only reads runtime /
    DB and never re-triggers resolution. Re-running ``switch_active_symbol``
    re-resolves the option universe *and* re-subscribes the live feed, so the
    next poll picks up a populated expiry list with OI actually flowing.
    """
    # Lazy import to avoid an import cycle (ingest layer pulls in market/auth).
    from ..ingest.symbol_controller import switch_active_symbol

    async with _reresolve_lock:
        rt = get_runtime()
        # Another in-flight switch may have already populated it.
        if symbol == rt.active_symbol and rt.expiries:
            return
        try:
            result = await switch_active_symbol(symbol)
            log.info("api_expiries.self_heal", symbol=symbol, expiries=result.expiries)
        except Exception as e:
            log.warning("api_expiries.self_heal.error", symbol=symbol, error=str(e))


@router.get("/expiries", response_model=ExpiriesResponse)
async def expiries(symbol: str | None = Query(default=None)) -> ExpiriesResponse:
    entry = resolve_symbol(symbol)
    rt = get_runtime()

    # Live runtime list is authoritative only for the active symbol.
    if entry.symbol == rt.active_symbol and rt.expiries:
        return ExpiriesResponse(expiries=[e.isoformat() for e in sorted(rt.expiries)])

    # Fallback: derive distinct expiries from the snapshot table.
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT DISTINCT expiry FROM option_oi_snapshots "
                    "WHERE symbol = :symbol ORDER BY expiry"
                ),
                {"symbol": entry.symbol},
            )
        ).all()
    if rows:
        return ExpiriesResponse(expiries=[r[0].isoformat() for r in rows])

    # Active F&O symbol with nothing resolved yet (first option fetch was likely
    # throttled to empty). Kick off a guarded background re-resolution so the
    # next poll recovers — without the user having to re-select the symbol.
    if (
        entry.symbol == rt.active_symbol
        and entry.fno_eligible
        and not _reresolve_lock.locked()
    ):
        asyncio.create_task(_reresolve_active_universe(entry.symbol))

    return ExpiriesResponse(expiries=[])
