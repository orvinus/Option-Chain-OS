"""Historical replay: returns OI-change frames between two timestamps."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from ..core.config import settings
from ..core.db import AsyncSessionLocal
from ..core.time_utils import IST


@dataclass
class ReplayRow:
    strike: int
    call_oi: int
    put_oi: int


@dataclass
class ReplayFrame:
    ts: str  # ISO-8601 IST
    spot: float | None
    rows: list[ReplayRow]


_SNAPSHOT_AT_BUCKET_SQL = text(
    """
    SELECT DISTINCT ON (strike, option_type)
        strike, option_type, oi, underlying, ts
    FROM option_oi_snapshots
    WHERE symbol = :symbol AND expiry = :expiry AND ts <= :ts_at
    ORDER BY strike, option_type, ts DESC
    """
)


async def fetch_replay(
    expiry,
    start: datetime,
    end: datetime,
    step: timedelta,
    symbol: str | None = None,
) -> list[ReplayFrame]:
    """Walk forward in ``step`` increments, snapshotting the OI book at each tick."""
    symbol = symbol or settings.underlying_symbol
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    cursor = start
    frames: list[ReplayFrame] = []
    async with AsyncSessionLocal() as s:
        while cursor <= end:
            rows = (
                (
                    await s.execute(
                        _SNAPSHOT_AT_BUCKET_SQL,
                        {"symbol": symbol, "expiry": expiry, "ts_at": cursor},
                    )
                )
                .mappings()
                .all()
            )
            book: dict[int, dict[str, int]] = {}
            spot = None
            for r in rows:
                book.setdefault(r["strike"], {"CE": 0, "PE": 0})[r["option_type"]] = int(r["oi"])
                if spot is None and r.get("underlying") is not None:
                    spot = float(r["underlying"])
            replay_rows = [
                ReplayRow(strike=k, call_oi=v.get("CE", 0), put_oi=v.get("PE", 0))
                for k, v in sorted(book.items())
            ]
            frames.append(
                ReplayFrame(
                    ts=cursor.astimezone(IST).isoformat(),
                    spot=spot,
                    rows=replay_rows,
                )
            )
            cursor = cursor + step
    return frames
