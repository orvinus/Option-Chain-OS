"""GET /api/ratio-timeseries — Call/Put ratio and PCR per time bucket.

A lean projection over :func:`fetch_oi_timeseries` for the dedicated Ratio chart.
When ``strike_min``/``strike_max`` are omitted it defaults to the full stored
strike range for the symbol+expiry, so the ratio reflects the whole chain.
Each bucket is its own aggregation (timeframe-independent) — a 5m ratio is the
ratio of 5m-bucketed totals, never a resample of the 1m series.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

from ..core.time_utils import IST
from ..services import fetch_oi_timeseries
from ..services.oi_timeseries import BUCKET_TO_INTERVAL, fetch_strike_bounds
from ._expiry_utils import resolve_expiry
from ._symbol_utils import resolve_fno_symbol
from .schemas import RatioPointOut, RatioTimeseriesResponseOut

router = APIRouter(tags=["ratio-timeseries"])


def _parse_ts(raw: str, field: str) -> datetime:
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        raise HTTPException(400, f"Invalid '{field}' datetime '{raw}' (expected ISO-8601).")
    if dt.tzinfo is None:
        dt = IST.localize(dt)
    return dt


@router.get("/ratio-timeseries", response_model=RatioTimeseriesResponseOut)
async def ratio_timeseries(
    symbol: str | None = Query(default=None),
    expiry: str | None = Query(default=None, description="ISO date (YYYY-MM-DD)"),
    bucket: str = Query(default="1m", description="One of " + ",".join(BUCKET_TO_INTERVAL.keys())),
    strike_min: int | None = Query(default=None, description="Lowest strike (default: full stored range)."),
    strike_max: int | None = Query(default=None, description="Highest strike (default: full stored range)."),
    from_ts: str | None = Query(default=None, description="Window start (ISO-8601). Default: session open of latest data."),
    to_ts: str | None = Query(default=None, description="Window end (ISO-8601). Default: latest tick."),
) -> RatioTimeseriesResponseOut:
    if bucket not in BUCKET_TO_INTERVAL:
        raise HTTPException(400, f"Unsupported bucket '{bucket}'.")

    entry = resolve_fno_symbol(symbol)
    e = await resolve_expiry(expiry, symbol=entry.symbol)

    if strike_min is None or strike_max is None:
        bounds = await fetch_strike_bounds(entry.symbol, e)
        if bounds is None:
            return RatioTimeseriesResponseOut(symbol=entry.symbol, expiry=e.isoformat(), bucket=bucket, points=[])
        lo, hi = bounds
        strike_min = strike_min if strike_min is not None else lo
        strike_max = strike_max if strike_max is not None else hi
    if strike_min > strike_max:
        raise HTTPException(400, "'strike_min' must be <= 'strike_max'.")

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
    return RatioTimeseriesResponseOut(
        symbol=res.symbol,
        expiry=res.expiry,
        bucket=res.bucket,
        points=[
            RatioPointOut(
                ts=p.ts,
                ratio=p.ratio,
                pcr=p.pcr,
                total_call_oi=p.total_call_oi,
                total_put_oi=p.total_put_oi,
            )
            for p in res.points
        ],
    )
