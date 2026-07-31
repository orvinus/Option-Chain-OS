"""GET /api/oi-change — strike-wise call/put OI change for a timeframe or window."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

from ..core.time_utils import IST, TIMEFRAME_TO_DELTA
from ..runtime import get_runtime
from ..services import get_oi_engine
from ._expiry_utils import resolve_expiry
from ._symbol_utils import resolve_fno_symbol
from .schemas import OIChangeResponseOut, OIChangeRowOut

router = APIRouter(tags=["oi-change"])


def _parse_ts(raw: str, field: str) -> datetime:
    """Parse an ISO-8601 datetime; treat naive values as IST."""
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        raise HTTPException(400, f"Invalid '{field}' datetime '{raw}' (expected ISO-8601).")
    if dt.tzinfo is None:
        dt = IST.localize(dt)
    return dt


@router.get("/oi-change", response_model=OIChangeResponseOut)
async def oi_change(
    timeframe: str = Query(default="5m", description="One of " + ",".join(TIMEFRAME_TO_DELTA.keys())),
    expiry: str | None = Query(default=None, description="ISO date (YYYY-MM-DD)"),
    symbol: str | None = Query(default=None),
    as_of: str | None = Query(default=None, description="Compute the timeframe as of this ISO-8601 instant (historical, e.g. a past date's close). Omit for the live latest snapshot. Ignored when from_ts is set."),
    from_ts: str | None = Query(default=None, description="Custom window start (ISO-8601). When set, overrides timeframe."),
    to_ts: str | None = Query(default=None, description="Custom window end (ISO-8601). Omit for 'up to latest' (live)."),
) -> OIChangeResponseOut:
    entry = resolve_fno_symbol(symbol)
    e = await resolve_expiry(expiry, symbol=entry.symbol)
    engine = get_oi_engine()
    rt = get_runtime()
    live_spot = rt.latest_spot if entry.symbol == rt.active_symbol else None
    if from_ts is not None:
        f = _parse_ts(from_ts, "from_ts")
        t = _parse_ts(to_ts, "to_ts") if to_ts is not None else None
        if t is not None and t <= f:
            raise HTTPException(400, "'to_ts' must be after 'from_ts'.")
        if t is None and f.astimezone(IST).date() < datetime.now(IST).date():
            raise HTTPException(
                400,
                "Open-ended windows (no 'to_ts') are only supported for the current "
                "session. Provide 'to_ts' when 'from_ts' is on a past date.",
            )
        res = await engine.get_range(f, t, e, symbol=entry.symbol, live_spot=live_spot)
    else:
        if timeframe not in TIMEFRAME_TO_DELTA:
            raise HTTPException(400, f"Unsupported timeframe '{timeframe}'.")
        as_of_dt = _parse_ts(as_of, "as_of") if as_of is not None else None
        res = await engine.get(timeframe, e, symbol=entry.symbol, live_spot=live_spot, as_of=as_of_dt)
    return OIChangeResponseOut(
        timeframe=res.timeframe,
        expiry=res.expiry,
        spot=res.spot,
        asof=res.asof,
        computed_at=res.computed_at,
        total_call_oi_change=res.total_call_oi_change,
        total_put_oi_change=res.total_put_oi_change,
        rows=[OIChangeRowOut(**r.__dict__) for r in res.rows],
    )
