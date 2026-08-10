"""Aggregate OI time-series for the Charts page.

Given a symbol, expiry and a contiguous strike range ``[strike_min, strike_max]``
(the dashboard's "ATM ± N" window resolved on the client), this returns the
**total** Call and Put OI per time bucket across the trading session.

Strategy — a single TimescaleDB ``time_bucket`` aggregate (mirrors the ``oi_1h``
continuous aggregate in ``alembic/0001_init.py``):

  1. inner: ``last(oi, ts)`` per (bucket, strike, option_type) — the OI of each
     strike at the close of every bucket.
  2. outer: ``sum`` those per-strike values into one CE total and one PE total
     per bucket.

The frontend turns this 1-minute series into "change since session open" and
buckets it into 5/10/15/30-minute OHLC candles (or a 1-minute line). Returning
the raw 1-minute totals keeps the candle math in one place and the SQL cheap.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text

from ..core.db import AsyncSessionLocal
from ..core.time_utils import IST, market_open_today

# Postgres ``interval`` literals for the buckets we support. The Charts page only
# requests "1m" (it builds higher intervals client-side), but the endpoint stays
# generic.
BUCKET_TO_INTERVAL: dict[str, str] = {
    "1m": "1 minute",
    "5m": "5 minutes",
    "10m": "10 minutes",
    "15m": "15 minutes",
    "30m": "30 minutes",
}


def _safe_ratio(num: float, den: float) -> float | None:
    """Divide, returning ``None`` on a zero denominator (so charts gap the line)."""
    if den == 0:
        return None
    return num / den


@dataclass
class OITimeseriesPoint:
    ts: str  # ISO-8601 IST (bucket start)
    total_call_oi: int
    total_put_oi: int
    # Call/Put ratio and PCR of the bucketed totals. ``ratio`` is None when there
    # is no put OI; ``pcr`` is None when there is no call OI.
    ratio: float | None = None
    pcr: float | None = None


@dataclass
class OITimeseriesResponse:
    symbol: str
    expiry: str
    bucket: str
    points: list[OITimeseriesPoint]


_MAX_TS_SQL = text(
    """
    SELECT MAX(ts) AS max_ts
    FROM oi_snapshots_unified
    WHERE symbol = :symbol AND expiry = :expiry
    """
)

_STRIKE_BOUNDS_SQL = text(
    """
    SELECT MIN(strike) AS lo, MAX(strike) AS hi
    FROM oi_snapshots_unified
    WHERE symbol = :symbol AND expiry = :expiry
    """
)


async def fetch_strike_bounds(symbol: str, expiry: date) -> tuple[int, int] | None:
    """Return the (min, max) stored strike for a symbol+expiry, or None if empty."""
    async with AsyncSessionLocal() as s:
        row = (
            await s.execute(_STRIKE_BOUNDS_SQL, {"symbol": symbol, "expiry": expiry})
        ).mappings().first()
    if not row or row["lo"] is None or row["hi"] is None:
        return None
    return int(row["lo"]), int(row["hi"])

# Carry-forward (locf) matters: a strike contributes its last-known OI to every
# bucket, not just buckets it happened to tick in. Without it, buckets missing a
# strike's tick (the in-progress minute, or any quiet minute on less-liquid
# chains like SENSEX) sum only the strikes that ticked — the "total" dipped by
# whatever fraction of the chain stayed silent.
#
# The strike SET must also stay constant across buckets. Leading buckets (before a
# strike's first tick) used to be dropped, so a strike that started ticking mid-session
# ENTERED the sum partway through and its whole OI appeared as a step up — which the
# client's change-since-open then reported as a large fake buildup. Seeding those
# leading buckets with the strike's first observed OI keeps membership constant, so a
# late-arriving strike contributes 0 *change* instead of its entire OI.
_MAX_TS_IN_WINDOW_SQL = text(
    """
    SELECT MAX(ts) FROM oi_snapshots_unified
    WHERE symbol = :symbol
      AND expiry = :expiry
      AND strike BETWEEN :strike_min AND :strike_max
      AND ts >= :from_ts
      AND ts <= :to_ts
    """
)

_TIMESERIES_SQL = text(
    """
    WITH per_strike AS (
        SELECT
            time_bucket_gapfill((:bucket_iv)::interval, ts, :from_ts, :to_ts) AS bucket,
            strike,
            option_type,
            locf(last(oi, ts)) AS oi
        FROM oi_snapshots_unified
        WHERE symbol = :symbol
          AND expiry = :expiry
          AND strike BETWEEN :strike_min AND :strike_max
          AND ts >= :from_ts
          AND ts <= :to_ts
        GROUP BY 1, 2, 3
    ),
    -- First OI each strike is ever observed with inside the window. Used to seed its
    -- LEADING buckets (before its first tick), which locf cannot fill because there is
    -- nothing earlier to carry forward.
    first_seen AS (
        SELECT
            strike,
            option_type,
            (array_agg(oi ORDER BY bucket) FILTER (WHERE oi IS NOT NULL))[1] AS oi0
        FROM per_strike
        GROUP BY strike, option_type
    )
    SELECT
        p.bucket,
        COALESCE(SUM(COALESCE(p.oi, f.oi0)) FILTER (WHERE p.option_type = 'CE'), 0) AS total_call_oi,
        COALESCE(SUM(COALESCE(p.oi, f.oi0)) FILTER (WHERE p.option_type = 'PE'), 0) AS total_put_oi
    FROM per_strike p
    JOIN first_seen f
      ON f.strike = p.strike AND f.option_type = p.option_type
    WHERE COALESCE(p.oi, f.oi0) IS NOT NULL
    GROUP BY p.bucket
    ORDER BY p.bucket
    """
)


async def fetch_oi_timeseries(
    symbol: str,
    expiry: date,
    strike_min: int,
    strike_max: int,
    bucket: str = "1m",
    from_ts: datetime | None = None,
    to_ts: datetime | None = None,
) -> OITimeseriesResponse:
    """Total CE/PE OI per ``bucket`` over the session for the strike range.

    ``from_ts``/``to_ts`` default to the regular session window of the latest
    stored data date (so an unparameterised call returns the current day).
    """
    interval = BUCKET_TO_INTERVAL.get(bucket, BUCKET_TO_INTERVAL["1m"])

    async with AsyncSessionLocal() as s:
        # Anchor to the freshest stored tick so defaults track the active session.
        anchor_row = (
            await s.execute(_MAX_TS_SQL, {"symbol": symbol, "expiry": expiry})
        ).mappings().first()
        anchor = anchor_row["max_ts"] if anchor_row else None
        if anchor is not None and getattr(anchor, "tzinfo", None) is None:
            anchor = anchor.replace(tzinfo=timezone.utc)

        if to_ts is None:
            to_ts = anchor or datetime.now(timezone.utc)
        if from_ts is None:
            ref = (anchor or to_ts).astimezone(IST)
            from_ts = market_open_today(ref)

        from_utc = from_ts.astimezone(timezone.utc)
        to_utc = to_ts.astimezone(timezone.utc)

        # Clamp the window end to the last tick that actually exists inside it.
        # gapfill+locf happily manufacture buckets all the way to :to_ts, carrying the
        # last known OI forward — so a feed outage (or a client asking for the full
        # session while data stopped at 11:40) rendered as a complete, flat, healthy
        # session instead of a series that visibly stops.
        real_end = (
            await s.execute(
                _MAX_TS_IN_WINDOW_SQL,
                {
                    "symbol": symbol,
                    "expiry": expiry,
                    "strike_min": strike_min,
                    "strike_max": strike_max,
                    "from_ts": from_utc,
                    "to_ts": to_utc,
                },
            )
        ).scalar()
        if real_end is not None:
            if getattr(real_end, "tzinfo", None) is None:
                real_end = real_end.replace(tzinfo=timezone.utc)
            to_utc = min(to_utc, real_end.astimezone(timezone.utc))

        rows = (
            await s.execute(
                _TIMESERIES_SQL,
                {
                    "bucket_iv": interval,
                    "symbol": symbol,
                    "expiry": expiry,
                    "strike_min": strike_min,
                    "strike_max": strike_max,
                    "from_ts": from_utc,
                    "to_ts": to_utc,
                },
            )
        ).mappings().all()

    points = []
    for r in rows:
        ce = int(r["total_call_oi"])
        pe = int(r["total_put_oi"])
        points.append(
            OITimeseriesPoint(
                ts=r["bucket"].astimezone(IST).isoformat(),
                total_call_oi=ce,
                total_put_oi=pe,
                ratio=_safe_ratio(ce, pe),
                pcr=_safe_ratio(pe, ce),
            )
        )
    return OITimeseriesResponse(
        symbol=symbol,
        expiry=expiry.isoformat(),
        bucket=bucket,
        points=points,
    )
