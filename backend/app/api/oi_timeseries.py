"""GET /api/oi-timeseries — total CE/PE OI per time bucket for a strike range.

Powers the Charts page: total Call / Put OI summed across the ATM ± N strike
window, sampled per ``bucket`` over the session. The client turns this into a
change-since-open series and 5/10/15/30-minute OHLC candles (or a 1m line).
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

from ..core.time_utils import IST
from ..services import fetch_oi_timeseries
from ..services.oi_timeseries import BUCKET_TO_INTERVAL
from ._expiry_utils import resolve_expiry
from ._symbol_utils import resolve_fno_symbol
from .schemas import OITimeseriesPointOut, OITimeseriesResponseOut

router = APIRouter(tags=["oi-timeseries"])


def _parse_ts(raw: str, field: str) -> datetime:
    """Parse an ISO-8601 datetime; treat naive values as IST."""
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        raise HTTPException(400, f"Invalid '{field}' datetime '{raw}' (expected ISO-8601).")
    if dt.tzinfo is None:
        dt = IST.localize(dt)
    return dt


@router.get("/oi-timeseries", response_model=OITimeseriesResponseOut)
async def oi_timeseries(
    symbol: str | None = Query(default=None),
    expiry: str | None = Query(default=None, description="ISO date (YYYY-MM-DD)"),
    strike_min: int = Query(description="Lowest strike to include (inclusive)."),
    strike_max: int = Query(description="Highest strike to include (inclusive)."),
    bucket: str = Query(default="1m", description="One of " + ",".join(BUCKET_TO_INTERVAL.keys())),
    from_ts: str | None = Query(default=None, description="Window start (ISO-8601). Default: session open of latest data."),
    to_ts: str | None = Query(default=None, description="Window end (ISO-8601). Default: latest tick."),
) -> OITimeseriesResponseOut:
    if bucket not in BUCKET_TO_INTERVAL:
        raise HTTPException(400, f"Unsupported bucket '{bucket}'.")
    if strike_min > strike_max:
        raise HTTPException(400, "'strike_min' must be <= 'strike_max'.")

    entry = resolve_fno_symbol(symbol)
    e = await resolve_expiry(expiry, symbol=entry.symbol)

    f = _parse_ts(from_ts, "from_ts") if from_ts is not None else None
    t = _parse_ts(to_ts, "to_ts") if to_ts is not None else None
    if f is not None and t is not None and t <= f:
        raise HTTPException(400, "'to_ts' must be after 'from_ts'.")

    res = await fetch_oi_timeseries(
        symbol=entry.symbol,
        expiry=e,
        strike_min=strike_min,
        strike_max=strike_max,
        bucket=bucket,
        from_ts=f,
        to_ts=t,
    )
    return OITimeseriesResponseOut(
        symbol=res.symbol,
        expiry=res.expiry,
        bucket=res.bucket,
        points=[OITimeseriesPointOut(**p.__dict__) for p in res.points],
    )
