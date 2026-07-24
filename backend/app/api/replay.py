"""GET /api/replay — historical OI replay frames."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query

from ..services import fetch_replay
from ._expiry_utils import resolve_expiry
from ._symbol_utils import resolve_fno_symbol
from .schemas import ReplayFrameOut, ReplayResponseOut, ReplayRowOut

router = APIRouter(tags=["replay"])

_STEP_MAP = {
    "1s": timedelta(seconds=1),
    "15s": timedelta(seconds=15),
    "30s": timedelta(seconds=30),
    "45s": timedelta(seconds=45),
    "1m": timedelta(minutes=1),
    "3m": timedelta(minutes=3),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
}


@router.get("/replay", response_model=ReplayResponseOut)
async def replay(
    start: datetime = Query(description="ISO 8601, e.g. 2026-05-08T09:15:00+05:30"),
    end: datetime = Query(description="ISO 8601"),
    step: str = Query(default="5m"),
    expiry: str | None = Query(default=None),
    symbol: str | None = Query(default=None),
    summary: bool = Query(default=False, description="Omit per-strike rows (lean scrubber payload)."),
    with_greeks: bool = Query(default=False, description="Join persisted greeks/IV per strike (null until populated)."),
) -> ReplayResponseOut:
    if step not in _STEP_MAP:
        raise HTTPException(400, f"Unsupported step '{step}'.")
    if start >= end:
        raise HTTPException(400, "start must be < end")
    if (end - start).total_seconds() / _STEP_MAP[step].total_seconds() > 5000:
        raise HTTPException(400, "Replay range too large; reduce window or increase step.")

    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)

    entry = resolve_fno_symbol(symbol)
    e = await resolve_expiry(expiry, symbol=entry.symbol)
    frames = await fetch_replay(
        e, start, end, _STEP_MAP[step], symbol=entry.symbol, summary=summary, with_greeks=with_greeks,
    )
    return ReplayResponseOut(
        symbol=entry.symbol,
        expiry=e.isoformat(),
        frames=[
            ReplayFrameOut(
                ts=f.ts,
                spot=f.spot,
                atm=f.atm,
                total_call_oi=f.total_call_oi,
                total_put_oi=f.total_put_oi,
                total_call_oi_change=f.total_call_oi_change,
                total_put_oi_change=f.total_put_oi_change,
                ratio=f.ratio,
                pcr=f.pcr,
                rows=[ReplayRowOut(**r.__dict__) for r in f.rows],
            )
            for f in frames
        ],
    )
