"""Historical replay: returns enriched OI frames between two timestamps.

Strategy: ONE TimescaleDB ``time_bucket_gapfill + locf`` query builds the "book
as of each bucket, carried forward" for every frame at once (replacing the old
one-query-per-frame loop, which issued up to 5000 sequential round-trips). Frames
are assembled in Python and enriched with totals, ATM, ratio/PCR and change-since-
window-start — everything the frontend replay player and CSV export need.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from ..core.config import settings
from ..core.db import AsyncSessionLocal
from ..core.time_utils import IST
from .greeks_history import fetch_greeks_series
from .oi_change import _atm_strike, _safe_ratio


@dataclass
class ReplayRow:
    strike: int
    call_oi: int
    put_oi: int
    call_oi_change: int
    put_oi_change: int
    # Greeks/IV from greeks_snapshots (None until the populator has run for this
    # session — greeks are persisted going forward, not backfilled).
    call_iv: float | None = None
    put_iv: float | None = None
    call_delta: float | None = None
    put_delta: float | None = None
    call_gamma: float | None = None
    put_gamma: float | None = None
    call_theta: float | None = None
    put_theta: float | None = None
    call_vega: float | None = None
    put_vega: float | None = None


@dataclass
class ReplayFrame:
    ts: str  # ISO-8601 IST
    spot: float | None
    atm: int | None
    total_call_oi: int
    total_put_oi: int
    total_call_oi_change: int
    total_put_oi_change: int
    ratio: float | None
    pcr: float | None
    rows: list[ReplayRow]


# All frames in one pass. ``locf`` carries each strike's last-known OI forward into
# every bucket (so the book is complete at each instant, mirroring the old
# per-cursor ``DISTINCT ON`` snapshot). Buckets before a strike's first tick stay
# NULL and are dropped; a bucket before ANY data has no rows and is absent.
_REPLAY_SERIES_SQL = text(
    """
    WITH per_strike AS (
        SELECT
            time_bucket_gapfill((:step_iv)::interval, ts, :start, :end) AS bucket,
            strike,
            option_type,
            locf(last(oi, ts)) AS oi,
            locf(last(underlying, ts)) AS underlying
        FROM option_oi_snapshots
        WHERE symbol = :symbol
          AND expiry = :expiry
          AND ts >= :start
          AND ts <= :end
        GROUP BY 1, 2, 3
    )
    SELECT bucket, strike, option_type, oi, underlying
    FROM per_strike
    WHERE oi IS NOT NULL
    ORDER BY bucket, strike, option_type
    """
)


async def fetch_replay(
    expiry,
    start: datetime,
    end: datetime,
    step: timedelta,
    symbol: str | None = None,
    summary: bool = False,
    with_greeks: bool = False,
) -> list[ReplayFrame]:
    """Enriched replay frames from ``start`` to ``end`` at ``step`` resolution.

    ``summary=True`` omits the per-strike ``rows`` (totals/spot/atm/ratio/pcr only)
    for a lean scrubber payload. ``with_greeks=True`` joins persisted greeks/IV per
    strike (from ``greeks_snapshots``; null until the populator has run). Changes
    are computed vs the first frame in the window (change since replay start).
    """
    symbol = (symbol or settings.underlying_symbol).upper()
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    step_iv = f"{int(step.total_seconds())} seconds"

    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(
                _REPLAY_SERIES_SQL,
                {"step_iv": step_iv, "symbol": symbol, "expiry": expiry, "start": start, "end": end},
            )
        ).mappings().all()

    greeks_by_bucket: dict = {}
    if with_greeks and not summary:
        greeks_by_bucket = await fetch_greeks_series(symbol, expiry, start, end, step_iv)

    # Group rows by bucket, preserving order (query is ORDER BY bucket).
    buckets: list[datetime] = []
    by_bucket: dict[datetime, dict[int, dict[str, int]]] = {}
    spot_by_bucket: dict[datetime, float | None] = {}
    for r in rows:
        b = r["bucket"]
        if b not in by_bucket:
            by_bucket[b] = {}
            spot_by_bucket[b] = None
            buckets.append(b)
        by_bucket[b].setdefault(r["strike"], {"CE": 0, "PE": 0})[r["option_type"]] = int(r["oi"])
        if spot_by_bucket[b] is None and r.get("underlying") is not None:
            spot_by_bucket[b] = float(r["underlying"])

    # Baseline = the first frame's book (per strike + totals) for change-since-start.
    base_book: dict[int, dict[str, int]] = by_bucket[buckets[0]] if buckets else {}
    base_ce = sum(v.get("CE", 0) for v in base_book.values())
    base_pe = sum(v.get("PE", 0) for v in base_book.values())

    frames: list[ReplayFrame] = []
    for b in buckets:
        book = by_bucket[b]
        spot = spot_by_bucket[b]
        total_ce = sum(v.get("CE", 0) for v in book.values())
        total_pe = sum(v.get("PE", 0) for v in book.values())
        replay_rows: list[ReplayRow] = []
        if not summary:
            g_map = greeks_by_bucket.get(b, {})
            for strike in sorted(book.keys()):
                v = book[strike]
                bv = base_book.get(strike, {})
                cg = g_map.get((strike, "CE"))
                pg = g_map.get((strike, "PE"))
                replay_rows.append(
                    ReplayRow(
                        strike=strike,
                        call_oi=v.get("CE", 0),
                        put_oi=v.get("PE", 0),
                        call_oi_change=v.get("CE", 0) - bv.get("CE", 0),
                        put_oi_change=v.get("PE", 0) - bv.get("PE", 0),
                        call_iv=cg["iv"] if cg else None,
                        put_iv=pg["iv"] if pg else None,
                        call_delta=cg["delta"] if cg else None,
                        put_delta=pg["delta"] if pg else None,
                        call_gamma=cg["gamma"] if cg else None,
                        put_gamma=pg["gamma"] if pg else None,
                        call_theta=cg["theta"] if cg else None,
                        put_theta=pg["theta"] if pg else None,
                        call_vega=cg["vega"] if cg else None,
                        put_vega=pg["vega"] if pg else None,
                    )
                )
        frames.append(
            ReplayFrame(
                ts=b.astimezone(IST).isoformat(),
                spot=spot,
                atm=_atm_strike(symbol, spot),
                total_call_oi=total_ce,
                total_put_oi=total_pe,
                total_call_oi_change=total_ce - base_ce,
                total_put_oi_change=total_pe - base_pe,
                ratio=_safe_ratio(total_ce, total_pe),
                pcr=_safe_ratio(total_pe, total_ce),
                rows=replay_rows,
            )
        )
    return frames
