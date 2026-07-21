"""Timeframe-aware Open Interest Change engine.

Given a timeframe (5m, 15m, 1h, full_day, ...) and an expiry, the engine returns
the per-strike (call_oi, put_oi, call_oi_change, put_oi_change) snapshot used by
the dashboard's main chart.

The query strategy:

  - "now" snapshot       = latest row per (strike, option_type) in
                           ``option_oi_snapshots`` for the given expiry.
  - "then" snapshot      = latest row per (strike, option_type) where
                           ``ts <= anchor - timeframe`` (or ``ts >= session_open``
                           on the anchor date for ``full_day``).  ``anchor`` is
                           ``MAX(ts)`` for the symbol+expiry (falls back to wall
                           clock when the table is empty).

Both are computed in a single Postgres round-trip using
``DISTINCT ON (strike, option_type) ... ORDER BY ... ts DESC`` queries.

Caching: results are cached in process for ``CACHE_TTL_SECONDS`` keyed by
``(timeframe, expiry, anchor_ts)`` where ``anchor_ts`` is ``MAX(ts)`` for that
symbol+expiry (so post-close data stays stable until new ticks arrive).
``on_aggregator_flush`` remains for future hooks; cache invalidation is driven
by ``anchor_ts`` changing after each flush.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from cachetools import TTLCache
from sqlalchemy import text

from ..core.db import AsyncSessionLocal
from ..core.logging import get_logger
from ..core.time_utils import IST, market_open_today, parse_timeframe
from ..runtime import get_runtime

log = get_logger("oi_change")

CACHE_TTL_SECONDS = 30

# The XTS feed disseminates OI only ~once per minute, so an exact 1s/15s/30s/45s
# window is empty most of the time. For these sub-minute timeframes we "hold the
# last change": where the exact window shows no OI movement for a strike, the
# baseline falls back to that strike's most recent OI move (within HOLD horizon),
# so the displayed delta persists until the feed pushes a new value.
SUBMINUTE_TIMEFRAMES = frozenset({"1s", "15s", "30s", "45s"})
HOLD_LAST_HORIZON = timedelta(minutes=5)


@dataclass
class OIChangeRow:
    strike: int
    call_oi: int
    put_oi: int
    call_oi_change: int
    put_oi_change: int
    call_ltp: float | None
    put_ltp: float | None
    call_ltp_change: float | None
    put_ltp_change: float | None


@dataclass
class OIChangeResponse:
    timeframe: str
    expiry: str
    spot: float | None
    # Latest exchange timestamp on option snapshot rows (Angel exchange_feed_time → DB ts).
    # After hours this often freezes at the last trade while the pipeline keeps recomputing.
    asof: str
    # Wall-clock IST when this snapshot was computed (always moves on each REST/WS push).
    computed_at: str
    total_call_oi_change: int
    total_put_oi_change: int
    rows: list[OIChangeRow]


# The ``now`` side is floored at the session open of the anchor's trading day:
# strikes whose data froze in a PREVIOUS session (window drift, weekend
# artifacts) must not appear in the "current" chain with days-old OI. The
# ``then``/baseline side is intentionally NOT floored — output strikes come
# exclusively from the now-map.
_LATEST_SNAPSHOT_SQL = text(
    """
    SELECT DISTINCT ON (strike, option_type)
        strike, option_type, oi, ltp, underlying, ts
    FROM option_oi_snapshots
    WHERE symbol = :symbol AND expiry = :expiry AND ts >= :floor
    ORDER BY strike, option_type, ts DESC
    """
)

_SNAPSHOT_AT_OR_BEFORE_FLOOR_SQL = text(
    """
    SELECT DISTINCT ON (strike, option_type)
        strike, option_type, oi, ltp, underlying, ts
    FROM option_oi_snapshots
    WHERE symbol = :symbol AND expiry = :expiry AND ts <= :cutoff AND ts >= :floor
    ORDER BY strike, option_type, ts DESC
    """
)

_SNAPSHOT_AT_OR_BEFORE_SQL = text(
    """
    SELECT DISTINCT ON (strike, option_type)
        strike, option_type, oi, ltp, underlying, ts
    FROM option_oi_snapshots
    WHERE symbol = :symbol AND expiry = :expiry AND ts <= :cutoff
    ORDER BY strike, option_type, ts DESC
    """
)

_SNAPSHOT_AT_OR_AFTER_SQL = text(
    """
    SELECT DISTINCT ON (strike, option_type)
        strike, option_type, oi, ltp, underlying, ts
    FROM option_oi_snapshots
    WHERE symbol = :symbol AND expiry = :expiry AND ts >= :cutoff
    ORDER BY strike, option_type, ts ASC
    """
)

# Per (strike, option_type): the most recent snapshot whose OI differs from the
# current latest OI, i.e. the value just before the last OI move. Bounded to a
# recent horizon so far/illiquid strikes that stopped moving don't surface a
# stale all-day delta on a sub-minute view.
_PREV_DISTINCT_OI_SQL = text(
    """
    WITH latest AS (
        SELECT DISTINCT ON (strike, option_type)
            strike, option_type, oi AS cur_oi
        FROM option_oi_snapshots
        WHERE symbol = :symbol AND expiry = :expiry
        ORDER BY strike, option_type, ts DESC
    )
    SELECT DISTINCT ON (o.strike, o.option_type)
        o.strike, o.option_type, o.oi, o.ltp, o.underlying, o.ts
    FROM option_oi_snapshots o
    JOIN latest l USING (strike, option_type)
    WHERE o.symbol = :symbol AND o.expiry = :expiry
      AND o.oi <> l.cur_oi
      AND o.ts >= :horizon
    ORDER BY o.strike, o.option_type, o.ts DESC
    """
)

_MAX_TS_SQL = text(
    """
    SELECT MAX(ts) AS max_ts
    FROM option_oi_snapshots
    WHERE symbol = :symbol AND expiry = :expiry
    """
)

_MIN_TS_SQL = text(
    """
    SELECT MIN(ts) AS min_ts
    FROM option_oi_snapshots
    WHERE symbol = :symbol AND expiry = :expiry
    """
)


def _assemble(
    timeframe: str,
    expiry: date,
    now_rows: list,
    then_rows: list,
    asof_override: datetime | None,
    now_utc: datetime,
    live_spot: float | None,
) -> "OIChangeResponse":
    """Build an OIChangeResponse by diffing the ``now`` vs ``then`` snapshots.

    Shared by the timeframe path (``_compute``) and the explicit window path
    (``_compute_range``). ``asof_override`` pins the reported ``asof`` (e.g. the
    DB anchor or the window's upper bound); when ``None`` the latest observed
    row timestamp is used.
    """
    now_map: dict[tuple[int, str], dict] = {(r["strike"], r["option_type"]): dict(r) for r in now_rows}
    then_map: dict[tuple[int, str], dict] = {(r["strike"], r["option_type"]): dict(r) for r in then_rows}

    strikes = sorted({k[0] for k in now_map.keys()})
    rows: list[OIChangeRow] = []
    max_ts: Optional[datetime] = None
    spot: Optional[float] = None
    total_ce_chg = 0
    total_pe_chg = 0
    for strike in strikes:
        ce_now = now_map.get((strike, "CE"))
        pe_now = now_map.get((strike, "PE"))
        ce_then = then_map.get((strike, "CE"))
        pe_then = then_map.get((strike, "PE"))
        ce_oi_now = int(ce_now["oi"]) if ce_now else 0
        pe_oi_now = int(pe_now["oi"]) if pe_now else 0
        ce_oi_then = int(ce_then["oi"]) if ce_then else 0
        pe_oi_then = int(pe_then["oi"]) if pe_then else 0
        ce_ltp_now = float(ce_now["ltp"]) if ce_now and ce_now.get("ltp") is not None else None
        pe_ltp_now = float(pe_now["ltp"]) if pe_now and pe_now.get("ltp") is not None else None
        ce_ltp_then = float(ce_then["ltp"]) if ce_then and ce_then.get("ltp") is not None else None
        pe_ltp_then = float(pe_then["ltp"]) if pe_then and pe_then.get("ltp") is not None else None
        ce_oi_chg = ce_oi_now - ce_oi_then
        pe_oi_chg = pe_oi_now - pe_oi_then
        total_ce_chg += ce_oi_chg
        total_pe_chg += pe_oi_chg
        rows.append(
            OIChangeRow(
                strike=strike,
                call_oi=ce_oi_now,
                put_oi=pe_oi_now,
                call_oi_change=ce_oi_chg,
                put_oi_change=pe_oi_chg,
                call_ltp=ce_ltp_now,
                put_ltp=pe_ltp_now,
                call_ltp_change=(ce_ltp_now - ce_ltp_then) if ce_ltp_now is not None and ce_ltp_then is not None else None,
                put_ltp_change=(pe_ltp_now - pe_ltp_then) if pe_ltp_now is not None and pe_ltp_then is not None else None,
            )
        )
        for r in (ce_now, pe_now):
            if not r:
                continue
            if max_ts is None or r["ts"] > max_ts:
                max_ts = r["ts"]
            if spot is None and r.get("underlying") is not None:
                spot = float(r["underlying"])

    asof_ts = asof_override if asof_override is not None else (max_ts or now_utc)
    computed_wall = now_utc.astimezone(IST).isoformat()
    # Prefer the live WebSocket spot for accurate ATM calculation
    final_spot = live_spot if live_spot is not None else spot
    return OIChangeResponse(
        timeframe=timeframe,
        expiry=expiry.isoformat(),
        spot=final_spot,
        asof=asof_ts.astimezone(IST).isoformat(),
        computed_at=computed_wall,
        total_call_oi_change=total_ce_chg,
        total_put_oi_change=total_pe_chg,
        rows=rows,
    )


class OIChangeEngine:
    def __init__(self) -> None:
        self._cache: TTLCache = TTLCache(maxsize=512, ttl=CACHE_TTL_SECONDS)
        self._lock = asyncio.Lock()

    def on_aggregator_flush(self, bucket: datetime) -> None:
        """Hook called by the aggregator after a successful flush."""
        _ = bucket  # reserved for metrics / future invalidation strategies

    async def _fetch_anchor_ts(self, symbol: str, expiry: date) -> datetime | None:
        async with AsyncSessionLocal() as s:
            row = (
                await s.execute(_MAX_TS_SQL, {"symbol": symbol, "expiry": expiry})
            ).mappings().first()
        if not row or row["max_ts"] is None:
            return None
        ts = row["max_ts"]
        if getattr(ts, "tzinfo", None) is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)

    async def get(
        self,
        timeframe: str,
        expiry: date,
        symbol: str | None = None,
        live_spot: float | None = None,
    ) -> OIChangeResponse:
        symbol = (symbol or get_runtime().active_symbol).upper()
        async with self._lock:
            anchor = await self._fetch_anchor_ts(symbol, expiry)
            cache_key = (timeframe, expiry.isoformat(), symbol, anchor)
            cached = self._cache.get(cache_key)
            if cached is not None:
                if live_spot is not None and live_spot != cached.spot:
                    from dataclasses import replace as dc_replace
                    return dc_replace(cached, spot=live_spot)
                return cached
            result = await self._compute(timeframe, expiry, symbol, anchor, live_spot)
            self._cache[cache_key] = result
            return result

    async def get_range(
        self,
        from_ts: datetime,
        to_ts: datetime | None,
        expiry: date,
        symbol: str | None = None,
        live_spot: float | None = None,
    ) -> OIChangeResponse:
        """OI change over an explicit window: snapshot(to_ts) - snapshot(from_ts).

        ``to_ts is None`` means "up to the latest snapshot" (a live, left-anchored
        window). Both bounds are timezone-aware datetimes (converted to UTC here).
        Cached for ``CACHE_TTL_SECONDS`` keyed by the rounded bounds + the DB
        anchor so live windows refresh as new ticks land.
        """
        symbol = (symbol or get_runtime().active_symbol).upper()
        from_utc = from_ts.astimezone(timezone.utc)
        to_utc = to_ts.astimezone(timezone.utc) if to_ts is not None else None
        async with self._lock:
            anchor = await self._fetch_anchor_ts(symbol, expiry)
            cache_key = (
                "range",
                from_utc.isoformat(),
                to_utc.isoformat() if to_utc else "now",
                expiry.isoformat(),
                symbol,
                anchor,
            )
            cached = self._cache.get(cache_key)
            if cached is not None:
                if live_spot is not None and live_spot != cached.spot:
                    from dataclasses import replace as dc_replace
                    return dc_replace(cached, spot=live_spot)
                return cached
            result = await self._compute_range(from_utc, to_utc, expiry, symbol, live_spot, anchor)
            self._cache[cache_key] = result
            return result

    async def _compute_range(
        self,
        from_utc: datetime,
        to_utc: datetime | None,
        expiry: date,
        symbol: str,
        live_spot: float | None = None,
        anchor: datetime | None = None,
    ) -> OIChangeResponse:
        now_utc = datetime.now(timezone.utc)
        params = {"symbol": symbol, "expiry": expiry}
        # Floor the now-side at the session open of the window's effective upper
        # bound so previous-session frozen strikes don't leak into the output.
        upper = to_utc or anchor or now_utc
        floor = market_open_today(upper.astimezone(IST)).astimezone(timezone.utc)
        async with AsyncSessionLocal() as s:
            if to_utc is None:
                now_rows = (
                    await s.execute(_LATEST_SNAPSHOT_SQL, {**params, "floor": floor})
                ).mappings().all()
            else:
                now_rows = (
                    await s.execute(
                        _SNAPSHOT_AT_OR_BEFORE_FLOOR_SQL,
                        {**params, "cutoff": to_utc, "floor": floor},
                    )
                ).mappings().all()
            then_rows = (
                await s.execute(_SNAPSHOT_AT_OR_BEFORE_SQL, {**params, "cutoff": from_utc})
            ).mappings().all()
            # Baseline clamp (mirrors the timeframe path): a strike whose first
            # row lands AFTER from_ts (it entered the subscription window
            # mid-range) has no true baseline — without a clamp its change is
            # reported as ``now - 0``, i.e. its full OI. Use its earliest stored
            # snapshot as the baseline so the change is "since data start".
            then_keys = {(r["strike"], r["option_type"]) for r in then_rows}
            missing = [
                (r["strike"], r["option_type"]) for r in now_rows
                if (r["strike"], r["option_type"]) not in then_keys
            ]
            if missing:
                earliest_rows = (
                    await s.execute(_SNAPSHOT_AT_OR_AFTER_SQL, {**params, "cutoff": from_utc})
                ).mappings().all()
                missing_set = set(missing)
                then_rows = list(then_rows) + [
                    r for r in earliest_rows
                    if (r["strike"], r["option_type"]) in missing_set
                ]
        # Report the upper bound (or now) as the window's asof.
        asof_override = to_utc if to_utc is not None else None
        return _assemble("range", expiry, now_rows, then_rows, asof_override, now_utc, live_spot)

    async def _compute(
        self,
        timeframe: str,
        expiry: date,
        symbol: str,
        anchor_from_db: datetime | None,
        live_spot: float | None = None,
    ) -> OIChangeResponse:
        delta_or_marker = parse_timeframe(timeframe)
        now_utc = datetime.now(timezone.utc)
        anchor = anchor_from_db or now_utc
        session_floor = market_open_today(anchor.astimezone(IST)).astimezone(timezone.utc)
        params = {"symbol": symbol, "expiry": expiry}
        if delta_or_marker == "full_day":
            # Baseline = earliest snapshot at/after today's open (already today-
            # anchored — no floor/backfill needed).
            then_sql = _SNAPSHOT_AT_OR_AFTER_SQL
            then_params = {**params, "cutoff": session_floor}
            floored_then = False
        else:
            assert isinstance(delta_or_marker, timedelta)
            cutoff = anchor - delta_or_marker
            # FLOOR the baseline to today's session open. The now-side is already
            # floored; if the then-side were not, a strike lacking a today row
            # at/before ``cutoff`` — e.g. one just re-added by ATM-drift
            # resubscription, or the first row after an ~83s reconnect gap —
            # would silently borrow a PREVIOUS session's much-larger OI as its
            # baseline, making ``now - then`` a huge, wrong-sign NEGATIVE that
            # sums to −Cr on short timeframes (the "1 Min shows −2Cr" bug).
            then_sql = _SNAPSHOT_AT_OR_BEFORE_FLOOR_SQL
            then_params = {**params, "cutoff": cutoff, "floor": session_floor}
            floored_then = True

        async with AsyncSessionLocal() as s:
            now_rows = (
                await s.execute(_LATEST_SNAPSHOT_SQL, {**params, "floor": session_floor})
            ).mappings().all()
            then_rows = list((await s.execute(then_sql, then_params)).mappings().all())
            # Per-strike baseline clamp (mirrors ``_compute_range``): a now-strike
            # with no floored ``then`` row (it entered the window mid-session) is
            # backfilled with its EARLIEST today snapshot, so its change reads as
            # "since its first row today" (small) rather than ``now - 0`` (a full
            # phantom add) or a leak to a prior session (huge negative).
            if floored_then:
                then_keys = {(r["strike"], r["option_type"]) for r in then_rows}
                missing = {
                    (r["strike"], r["option_type"]) for r in now_rows
                } - then_keys
                if missing:
                    earliest_rows = (
                        await s.execute(
                            _SNAPSHOT_AT_OR_AFTER_SQL, {**params, "cutoff": session_floor}
                        )
                    ).mappings().all()
                    then_rows += [
                        r for r in earliest_rows
                        if (r["strike"], r["option_type"]) in missing
                    ]

        # Sub-minute timeframes: hold the last OI move where the exact window is flat.
        if timeframe in SUBMINUTE_TIMEFRAMES and anchor_from_db is not None:
            then_rows = await self._merge_hold_last(symbol, expiry, anchor, now_rows, then_rows)

        asof_override = anchor if anchor_from_db is not None else None
        return _assemble(timeframe, expiry, now_rows, then_rows, asof_override, now_utc, live_spot)

    async def _merge_hold_last(
        self,
        symbol: str,
        expiry: date,
        anchor: datetime,
        now_rows: list,
        exact_then_rows: list,
    ) -> list:
        """Build a 'held' baseline for sub-minute timeframes.

        Per (strike, option_type): if the exact-window baseline already differs
        from the current OI, keep it (a real in-window move). Otherwise fall back
        to the strike's most recent OI move within HOLD_LAST_HORIZON, so the delta
        persists between the feed's ~once-a-minute OI updates instead of dropping
        to 0. Strikes with no recent move stay flat (baseline = current = 0 delta).
        """
        horizon = anchor - HOLD_LAST_HORIZON
        async with AsyncSessionLocal() as s:
            held_rows = (
                await s.execute(
                    _PREV_DISTINCT_OI_SQL,
                    {"symbol": symbol, "expiry": expiry, "horizon": horizon},
                )
            ).mappings().all()

        now_map = {(r["strike"], r["option_type"]): dict(r) for r in now_rows}
        exact_map = {(r["strike"], r["option_type"]): dict(r) for r in exact_then_rows}
        held_map = {(r["strike"], r["option_type"]): dict(r) for r in held_rows}

        merged: list[dict] = []
        for key, nrow in now_map.items():
            exact = exact_map.get(key)
            if exact is not None and int(exact["oi"]) != int(nrow["oi"]):
                merged.append(exact)          # genuine change inside the exact window
            elif key in held_map:
                merged.append(held_map[key])  # hold the last move
            elif exact is not None:
                merged.append(exact)          # flat: baseline == now -> 0 delta
            else:
                merged.append(nrow)           # no baseline yet -> 0 delta (avoid inflation)
        return merged


_singleton: OIChangeEngine | None = None


def get_oi_engine() -> OIChangeEngine:
    global _singleton
    if _singleton is None:
        _singleton = OIChangeEngine()
    return _singleton
