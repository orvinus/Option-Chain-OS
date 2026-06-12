"""GET /api/spot — current spot for the active (or queried) symbol."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Query

from ..core.time_utils import IST
from ..runtime import get_runtime
from ._symbol_utils import resolve_symbol
from .schemas import SpotResponse

router = APIRouter(tags=["spot"])


@router.get("/spot", response_model=SpotResponse)
async def spot(symbol: str | None = Query(default=None)) -> SpotResponse:
    entry = resolve_symbol(symbol)
    rt = get_runtime()
    # Only the active symbol has a live ticking spot; querying another symbol
    # returns no spot (UI shows the placeholder until /active-symbol is called).
    live = rt.latest_spot if entry.symbol == rt.active_symbol else None
    return SpotResponse(
        symbol=entry.symbol,
        spot=live,
        asof=datetime.now(timezone.utc).astimezone(IST).isoformat(),
    )
