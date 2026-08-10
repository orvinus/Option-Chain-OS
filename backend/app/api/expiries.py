"""GET /api/expiries — currently subscribed option expiries for a symbol."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query
from sqlalchemy import text

from ..core.db import AsyncSessionLocal
from ..core.logging import get_logger
from ..core.time_utils import now_ist
from ..runtime import get_runtime
from ._symbol_utils import resolve_symbol
from .schemas import ExpiriesResponse

log = get_logger("api_expiries")
router = APIRouter(tags=["expiries"])

# Guards the background self-heal so a 30s poll storm doesn't pile up re-resolves.
_reresolve_lock = asyncio.Lock()


async def _reresolve_active_universe(symbol: str) -> None:
    """Re-run the active-symbol switch so a throttled-empty first fetch recovers.

    The initial option fetch can come back empty if the broker throttled it; the
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

    # Return the UNION of two sources, so the dropdown offers both the live
    # upcoming expiry (subscribed, fills as ticks arrive) AND any historical
    # expiries that already have stored data. Using the live runtime as an
    # exclusive override hid expiries with 965k rows behind a single empty live
    # expiry — making "No OI data" unavoidable when the current expiry hasn't
    # ingested yet.
    expiry_set: set = set()

    # Source 1: live runtime list (authoritative only for the active symbol).
    if entry.symbol == rt.active_symbol and rt.expiries:
        expiry_set.update(rt.expiries)

    # Source 2: distinct expiries that actually have stored data — live table ∪
    # vendor archive (oi_snapshots_unified, migration 0005), so backfilled
    # expired weeklies are selectable in the Replay date/expiry pickers.
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT DISTINCT expiry FROM oi_snapshots_unified "
                    "WHERE symbol = :symbol ORDER BY expiry"
                ),
                {"symbol": entry.symbol},
            )
        ).all()
    expiry_set.update(r[0] for r in rows)

    if expiry_set:
        # Live (non-expired) expiries first, ascending, then past expiries most
        # recent first. The frontend defaults to expiries[0]
        # (useMarketContext.ts) — a plain ascending sort put months-old expired
        # contracts first, so the dashboard opened on frozen historical data.
        today = now_ist().date()
        upcoming = sorted(e for e in expiry_set if e >= today)
        past = sorted((e for e in expiry_set if e < today), reverse=True)
        return ExpiriesResponse(expiries=[e.isoformat() for e in upcoming + past])

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
