"""Historical replay: returns enriched OI frames between two timestamps.

Strategy: ONE TimescaleDB ``time_bucket_gapfill + locf`` query builds the "book
as of each bucket, carried forward" for every frame at once (replacing the old
one-query-per-frame loop, which issued up to 5000 sequential round-trips). Frames
are assembled in Python and enriched with totals, ATM, ratio/PCR and change-since-
session-open — everything the frontend replay player and CSV export need.

A frame labelled ``T`` contains ONLY observations with ``ts <= T``. That is not a
detail: ``time_bucket`` labels a bucket with its START, so the bucket labelled
11:00 holds the last tick in [11:00, 11:01) and the player used to display an
11:00 clock over data observed up to 11:00:59 — one step of look-ahead into the
future, in the one tool whose whole job is replaying a session honestly. Labels
are therefore emitted as ``bucket + step`` (see ``fetch_replay``).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from ..core.config import settings
from ..core.db import AsyncSessionLocal
from ..core.time_utils import IST, session_floor_for
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
#
# Reads ``oi_snapshots_unified`` (live table UNION vendor-backfilled
# ``oi_archive_bars``, migration 0005) so replay serves both live-recorded days
# and the imported 6-month TrueData history through one query. The importer
# guarantees no (symbol, day) overlap between the two stores.
_REPLAY_SERIES_SQL = text(
    """
    WITH per_strike AS (
        SELECT
            time_bucket_gapfill((:step_iv)::interval, ts, :start, :end) AS bucket,
            strike,
            option_type,
            locf(last(oi, ts)) AS oi,
            locf(last(ltp, ts)) AS ltp,
            locf(last(underlying, ts)) AS underlying,
            -- Age of the carried-forward values, so the frame's spot can be taken from
            -- the FRESHEST leg rather than whichever strike happens to sort first.
            locf(last(ts, ts)) AS last_ts
        FROM oi_snapshots_unified
        WHERE symbol = :symbol
          AND expiry = :expiry
          AND ts >= :start
          AND ts <= :end
        GROUP BY 1, 2, 3
    )
    SELECT bucket, strike, option_type, oi, ltp, underlying, last_ts
    FROM per_strike
    WHERE oi IS NOT NULL
    ORDER BY bucket, strike, option_type
    """
)


def _min_straddle_atm(ltp_book: dict[int, dict[str, float]]) -> int | None:
    """ATM fallback when no spot is stored for a frame: the strike whose CE+PE
    premium sum is smallest (the classic straddle-minimum ATM detector). Used
    for vendor-backfilled days that predate the vendor's own index-bar depth —
    without it the replay ATM (and the frontend's ATM±N strike filter) dies on
    exactly those days."""
    best: tuple[float, int] | None = None
    for strike, legs in ltp_book.items():
        ce, pe = legs.get("CE"), legs.get("PE")
        if ce is None or pe is None or (ce <= 0 and pe <= 0):
            continue
        s = ce + pe
        if best is None or s < best[0]:
            best = (s, strike)
    return best[1] if best else None

# Session-open baseline over the SAME unified source. A local copy of
# ``oi_change._SNAPSHOT_AT_OR_AFTER_BOUNDED_SQL`` on purpose: the live tabs keep
# reading the live table only, while replay must baseline archive days too —
# sharing the constant would silently widen every live tab's scan.
_BASELINE_AT_OR_AFTER_SQL = text(
    """
    SELECT DISTINCT ON (strike, option_type)
        strike, option_type, oi, ltp, underlying, ts
    FROM oi_snapshots_unified
    WHERE symbol = :symbol AND expiry = :expiry AND ts >= :cutoff AND ts <= :upper
    ORDER BY strike, option_type, ts ASC
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
    strike (from ``greeks_snapshots``; null until the populator has run).

    Changes are computed vs the session-open baseline, the same anchor every other
    tab uses, so a replay frame's "change today" equals what the OI Change and
    Multi-TF tabs report for the same instant.

    Frames are labelled with the END of their bucket (``bucket + step``), so a frame
    labelled ``T`` never contains an observation later than ``T``. The first label is
    therefore ``start + step`` and the last is ``end + step``.
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
        # Session-open baseline — ONE query for the whole replay, not one per frame.
        # Every other tab anchors "change today" on the FIRST tick at or after the
        # session open. Deriving it from the first gapfill bucket instead took that
        # bucket's LAST tick, which read ~3% low on put OI (puts ramp hardest in the
        # opening minute) and made replay disagree with the Multi-TF tab. Bounded by
        # `end` so a strike with no data this session cannot borrow a later day's
        # first row as its baseline.
        base_rows = (
            await s.execute(
                _BASELINE_AT_OR_AFTER_SQL,
                {
                    "symbol": symbol,
                    "expiry": expiry,
                    "cutoff": session_floor_for(start),
                    "upper": end,
                },
            )
        ).mappings().all()

    greeks_by_bucket: dict = {}
    if with_greeks and not summary:
        greeks_by_bucket = await fetch_greeks_series(symbol, expiry, start, end, step_iv)

    # Group rows by bucket, preserving order (query is ORDER BY bucket).
    buckets: list[datetime] = []
    by_bucket: dict[datetime, dict[int, dict[str, int]]] = {}
    ltp_by_bucket: dict[datetime, dict[int, dict[str, float]]] = {}
    spot_by_bucket: dict[datetime, float | None] = {}
    spot_ts_by_bucket: dict[datetime, datetime | None] = {}
    for r in rows:
        b = r["bucket"]
        if b not in by_bucket:
            by_bucket[b] = {}
            ltp_by_bucket[b] = {}
            spot_by_bucket[b] = None
            spot_ts_by_bucket[b] = None
            buckets.append(b)
        by_bucket[b].setdefault(r["strike"], {"CE": 0, "PE": 0})[r["option_type"]] = int(r["oi"])
        if r.get("ltp") is not None:
            ltp_by_bucket[b].setdefault(r["strike"], {})[r["option_type"]] = float(r["ltp"])
        # Take the frame's spot from the FRESHEST leg. Rows arrive in strike order, so
        # the old "first non-null wins" locked onto the LOWEST strike — typically a
        # quiet deep-ITM contract whose locf-carried underlying stops updating, which
        # froze the replay spot (and therefore the ATM) for the rest of the session.
        u = r.get("underlying")
        if u is not None:
            lts = r.get("last_ts")
            cur_ts = spot_ts_by_bucket.get(b)
            if spot_by_bucket[b] is None or (lts is not None and (cur_ts is None or lts > cur_ts)):
                spot_by_bucket[b] = float(u)
                spot_ts_by_bucket[b] = lts

    # Per-strike baseline: each strike's FIRST tick at or after the session open. The
    # query above already returns exactly one row per (strike, option_type), so a
    # strike that entered mid-session (ATM drift) is baselined on its own first tick
    # rather than on 0 — its whole open interest is not reported as "change".
    base_book: dict[int, dict[str, int]] = {}
    for r in base_rows:
        base_book.setdefault(r["strike"], {})[r["option_type"]] = int(r["oi"])
    # Defensive: a leg with no session-open row at all would baseline at 0 and report
    # its ENTIRE open interest as "change". Fall back to the first book it is actually
    # seen with, so an unexpected gap reads as no change rather than a fake spike.
    for b in buckets:
        for strike, v in by_bucket[b].items():
            slot = base_book.setdefault(strike, {})
            for leg, oi in v.items():
                slot.setdefault(leg, oi)

    frames: list[ReplayFrame] = []
    for b in buckets:
        book = by_bucket[b]
        spot = spot_by_bucket[b]
        total_ce = sum(v.get("CE", 0) for v in book.values())
        total_pe = sum(v.get("PE", 0) for v in book.values())
        # Baseline totals cover exactly the strikes PRESENT in this frame, using each
        # one's session-open book. This keeps the frame total change equal to the sum
        # of the per-strike changes (a global baseline over all strikes would make
        # early frames negative once a later strike joined).
        base_ce = sum(base_book.get(k, {}).get("CE", 0) for k in book)
        base_pe = sum(base_book.get(k, {}).get("PE", 0) for k in book)
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
                # Label the frame with the bucket's END: `time_bucket` labels by START,
                # so `b` covers [b, b+step) and labelling with `b` would advertise a
                # clock the data is up to one step ahead of. See the module docstring.
                ts=(b + step).astimezone(IST).isoformat(),
                spot=spot,
                # Spot-less frames (vendor-backfilled days before the vendor's own
                # index depth) fall back to the straddle-minimum ATM so the ATM tile
                # and the frontend's ATM±N strike filter keep working.
                atm=_atm_strike(symbol, spot)
                if spot is not None
                else _min_straddle_atm(ltp_by_bucket.get(b, {})),
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
