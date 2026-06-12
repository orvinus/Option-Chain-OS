"""GET /api/interpretation — per-strike CE/PE classification."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..core.time_utils import TIMEFRAME_TO_DELTA
from ..services import classify, get_oi_engine
from ..services.interpretation import classify_rows
from ._expiry_utils import resolve_expiry
from ._symbol_utils import resolve_fno_symbol
from .schemas import InterpretationResponseOut, InterpretationRowOut

router = APIRouter(tags=["interpretation"])


@router.get("/interpretation", response_model=InterpretationResponseOut)
async def interpretation(
    timeframe: str = Query(default="5m"),
    expiry: str | None = Query(default=None),
    symbol: str | None = Query(default=None),
) -> InterpretationResponseOut:
    if timeframe not in TIMEFRAME_TO_DELTA:
        raise HTTPException(400, f"Unsupported timeframe '{timeframe}'.")
    entry = resolve_fno_symbol(symbol)
    e = await resolve_expiry(expiry, symbol=entry.symbol)
    res = await get_oi_engine().get(timeframe, e, symbol=entry.symbol)
    interpretations = classify_rows(res.rows)
    _ = classify(0, 0)  # keep public re-export referenced
    return InterpretationResponseOut(
        timeframe=timeframe,
        expiry=e.isoformat(),
        rows=[
            InterpretationRowOut(strike=i.strike, call=i.call.value, put=i.put.value)
            for i in interpretations
        ],
    )
