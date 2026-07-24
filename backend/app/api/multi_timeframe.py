"""GET /api/multi-timeframe — one OI-change row per timeframe + shared level ratio/pcr.

Rows are 1m,3m,5m,10m,15m,30m,1h,2h,3h,full_day by default. Each row's OI-change
is an independent snapshot diff; the header ``ratio``/``pcr``/``spot``/``atm`` are
point-in-time levels shared by every row. ``as_of`` (ISO-8601) computes the grid
as of a historical instant (a picked date or the replay clock); omit for live.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

from ..core.time_utils import IST, TIMEFRAME_TO_DELTA
from ..runtime import get_runtime
from ..services import get_oi_engine
from ._expiry_utils import resolve_expiry
from ._symbol_utils import resolve_fno_symbol
from .schemas import MultiTFResponseOut, MultiTFRowOut

router = APIRouter(tags=["multi-timeframe"])

DEFAULT_TIMEFRAMES = ["1m", "3m", "5m", "10m", "15m", "30m", "1h", "2h", "3h", "full_day"]


def _parse_ts(raw: str, field: str) -> datetime:
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        raise HTTPException(400, f"Invalid '{field}' datetime '{raw}' (expected ISO-8601).")
    if dt.tzinfo is None:
        dt = IST.localize(dt)
    return dt


@router.get("/multi-timeframe", response_model=MultiTFResponseOut)
async def multi_timeframe(
    symbol: str | None = Query(default=None),
    expiry: str | None = Query(default=None, description="ISO date (YYYY-MM-DD)"),
    timeframes: str | None = Query(default=None, description="CSV of timeframes; default is the standard 10."),
    as_of: str | None = Query(default=None, description="Historical instant (ISO-8601). Omit for live."),
    atm_window: int | None = Query(default=None, description="Restrict sums to strikes within ATM ± N (omit / <0 = full chain)."),
) -> MultiTFResponseOut:
    tfs = [t.strip() for t in timeframes.split(",")] if timeframes else list(DEFAULT_TIMEFRAMES)
    unknown = [t for t in tfs if t not in TIMEFRAME_TO_DELTA]
    if unknown:
        raise HTTPException(400, f"Unsupported timeframe(s): {', '.join(unknown)}.")

    entry = resolve_fno_symbol(symbol)
    e = await resolve_expiry(expiry, symbol=entry.symbol)
    rt = get_runtime()
    as_of_dt = _parse_ts(as_of, "as_of") if as_of is not None else None
    # Live spot only applies to the live (non-historical) active-symbol view.
    live_spot = (
        rt.latest_spot
        if as_of_dt is None and entry.symbol == rt.active_symbol
        else None
    )

    res = await get_oi_engine().get_multi(
        tfs, e, symbol=entry.symbol, live_spot=live_spot, as_of=as_of_dt, atm_window=atm_window
    )
    return MultiTFResponseOut(
        symbol=res.symbol,
        expiry=res.expiry,
        asof=res.asof,
        computed_at=res.computed_at,
        spot=res.spot,
        atm_strike=res.atm_strike,
        total_call_oi=res.total_call_oi,
        total_put_oi=res.total_put_oi,
        ratio=res.ratio,
        pcr=res.pcr,
        rows=[MultiTFRowOut(**r.__dict__) for r in res.rows],
    )
