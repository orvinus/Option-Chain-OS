"""GET /api/replay — historical OI replay frames."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query

from ..services import fetch_replay
from ._expiry_utils import resolve_expiry
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

    e = await resolve_expiry(expiry)
    frames = await fetch_replay(e, start, end, _STEP_MAP[step])
    return ReplayResponseOut(
        expiry=e.isoformat(),
        frames=[
            ReplayFrameOut(
                ts=f.ts,
                spot=f.spot,
                rows=[ReplayRowOut(**r.__dict__) for r in f.rows],
            )
            for f in frames
        ],
    )
