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
from ..core.time_utils import IST, market_close_today, market_open_today, parse_timeframe
from ..market.symbols import get_registry
from ..runtime import get_runtime

log = get_logger("oi_change")

CACHE_TTL_SECONDS = 30
_DEFAULT_STRIKE_STEP = 50


def _safe_ratio(num: float, den: float) -> float | None:
    """Divide, returning ``None`` on a zero denominator (charts gap the line)."""
    if den == 0:
        return None
    return num / den


def _strike_step(symbol: str) -> int:
    """The symbol's registry strike step (fallback 50)."""
    entry = get_registry().get(symbol)
    return (entry.strike_step if entry and entry.strike_step else None) or _DEFAULT_STRIKE_STEP


def _atm_strike(symbol: str, spot: float | None) -> int | None:
    """Nearest strike to ``spot`` using the symbol's registry step (fallback 50)."""
    if spot is None:
        return None
    return int(round(float(spot) / _strike_step(symbol)) * _strike_step(symbol))

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


@dataclass
class MultiTFRow:
    timeframe: str
    call_oi_change: int
    put_oi_change: int
    # Ratio of the CHANGES (call_oi_change / put_oi_change). None on zero put change.
    oi_change_ratio: float | None


@dataclass
class MultiTFResponse:
    symbol: str
    expiry: str
    asof: str
    computed_at: str
    spot: float | None
    atm_strike: int | None
    # Point-in-time level totals (identical across all timeframe rows).
    total_call_oi: int
    total_put_oi: int
    ratio: float | None  # call/put level
    pcr: float | None    # put/call level
    rows: list[MultiTFRow]


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

_SNAPSHOT_AT_OR_AFTER_SQL = text(
    """
    SELECT DISTINCT ON (strike, option_type)
        strike, option_type, oi, ltp, underlying, ts
    FROM option_oi_snapshots
    WHERE symbol = :symbol AND expiry = :expiry AND ts >= :cutoff
    ORDER BY strike, option_type, ts ASC
    """
)

# Earliest snapshot per (strike, option_type) inside an explicit [cutoff, upper]
# window. Used by the range-path baseline clamp so a strike absent on the from-day
# can't pull a FUTURE day's first row as its "since data start" baseline.
_SNAPSHOT_AT_OR_AFTER_BOUNDED_SQL = text(
    """
    SELECT DISTINCT ON (strike, option_type)
        strike, option_type, oi, ltp, underlying, ts
    FROM option_oi_snapshots
    WHERE symbol = :symbol AND expiry = :expiry AND ts >= :cutoff AND ts <= :upper
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
        as_of: datetime | None = None,
    ) -> OIChangeResponse:
        """Strike-wise OI change for a timeframe.

        ``as_of`` (tz-aware) computes the timeframe as of a historical instant
        (a picked past date), mirroring ``get_multi``; omit it for the live
        latest snapshot. This lets the OI Change page show "15m as of a past
        date" identically to the Multi-TF grid's 15m row for that date.
        """
        symbol = (symbol or get_runtime().active_symbol).upper()
        as_of_utc = as_of.astimezone(timezone.utc) if as_of is not None else None
        async with self._lock:
            anchor = await self._fetch_anchor_ts(symbol, expiry)
            ref_key = as_of_utc.isoformat() if as_of_utc else (anchor.isoformat() if anchor else "now")
            cache_key = (timeframe, expiry.isoformat(), symbol, ref_key)
            cached = self._cache.get(cache_key)
            if cached is not None:
                # Live spot only overrides for the live (non-as_of) snapshot.
                if live_spot is not None and as_of_utc is None and live_spot != cached.spot:
                    from dataclasses import replace as dc_replace
                    return dc_replace(cached, spot=live_spot)
                return cached
            result = await self._compute(timeframe, expiry, symbol, anchor, live_spot, as_of_utc)
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
        from_ist = from_utc.astimezone(IST)
        from_floor = market_open_today(from_ist).astimezone(timezone.utc)

        # Resolve the effective window end. An explicit ``to_utc`` wins. When it is
        # omitted the window is "up to latest" (a live, left-anchored window) —
        # only meaningful for the CURRENT session: if ``from_ts`` is on a PAST day,
        # an open-ended window would floor the now-side to the global ``MAX(ts)``
        # (the latest data day) and return the WRONG day, so clamp the effective
        # end to that past day's session close. (The API also rejects this shape;
        # this is defence-in-depth for direct engine callers.)
        effective_to = to_utc
        if (
            effective_to is None
            and anchor is not None
            and from_ist.date() < anchor.astimezone(IST).date()
        ):
            effective_to = market_close_today(from_ist).astimezone(timezone.utc)

        # Floor the now-side at the session open of the window's effective upper
        # bound so previous-session frozen strikes don't leak into the output.
        upper = effective_to or anchor or now_utc
        floor = market_open_today(upper.astimezone(IST)).astimezone(timezone.utc)
        # Upper bound for the baseline-clamp's "earliest row in window" lookup.
        clamp_upper = effective_to if effective_to is not None else (from_floor + timedelta(days=1))
        async with AsyncSessionLocal() as s:
            if effective_to is None:
                now_rows = (
                    await s.execute(_LATEST_SNAPSHOT_SQL, {**params, "floor": floor})
                ).mappings().all()
            else:
                now_rows = (
                    await s.execute(
                        _SNAPSHOT_AT_OR_BEFORE_FLOOR_SQL,
                        {**params, "cutoff": effective_to, "floor": floor},
                    )
                ).mappings().all()
            # FLOOR the then/baseline side to the FROM day's session open, mirroring
            # the now-side floor. Without this a strike lacking a same-day row
            # at/before ``from_ts`` silently borrows a PREVIOUS session's much-larger
            # OI as its baseline, making ``now - then`` a huge wrong-sign delta — the
            # historical-window analogue of the "1 Min shows −2Cr" bug the timeframe
            # path already guards against.
            then_rows = (
                await s.execute(
                    _SNAPSHOT_AT_OR_BEFORE_FLOOR_SQL,
                    {**params, "cutoff": from_utc, "floor": from_floor},
                )
            ).mappings().all()
            # Baseline clamp (mirrors the timeframe path): a strike whose first
            # row lands AFTER from_ts (it entered the subscription window
            # mid-range) has no floored baseline — without a clamp its change is
            # reported as ``now - 0``, i.e. its full OI. Use its earliest stored
            # snapshot WITHIN the window as the baseline so the change is "since
            # data start" (bounded above so a future day's first row can't leak in).
            then_keys = {(r["strike"], r["option_type"]) for r in then_rows}
            missing = [
                (r["strike"], r["option_type"]) for r in now_rows
                if (r["strike"], r["option_type"]) not in then_keys
            ]
            if missing:
                earliest_rows = (
                    await s.execute(
                        _SNAPSHOT_AT_OR_AFTER_BOUNDED_SQL,
                        {**params, "cutoff": from_floor, "upper": clamp_upper},
                    )
                ).mappings().all()
                missing_set = set(missing)
                then_rows = list(then_rows) + [
                    r for r in earliest_rows
                    if (r["strike"], r["option_type"]) in missing_set
                ]
        # Report the effective upper bound (or now) as the window's asof.
        asof_override = effective_to if effective_to is not None else None
        return _assemble("range", expiry, now_rows, then_rows, asof_override, now_utc, live_spot)

    async def _compute(
        self,
        timeframe: str,
        expiry: date,
        symbol: str,
        anchor_from_db: datetime | None,
        live_spot: float | None = None,
        as_of_utc: datetime | None = None,
    ) -> OIChangeResponse:
        delta_or_marker = parse_timeframe(timeframe)
        now_utc = datetime.now(timezone.utc)
        # "now" reference: an explicit historical instant, else the DB anchor, else wall clock.
        anchor = as_of_utc or anchor_from_db or now_utc
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
            # NOW snapshot: the live latest, or the last row at/before a historical as_of.
            if as_of_utc is None:
                now_rows = (
                    await s.execute(_LATEST_SNAPSHOT_SQL, {**params, "floor": session_floor})
                ).mappings().all()
            else:
                now_rows = (
                    await s.execute(
                        _SNAPSHOT_AT_OR_BEFORE_FLOOR_SQL,
                        {**params, "cutoff": as_of_utc, "floor": session_floor},
                    )
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
        # Live only — a historical as_of reads the exact stored snapshot at that instant.
        if timeframe in SUBMINUTE_TIMEFRAMES and anchor_from_db is not None and as_of_utc is None:
            then_rows = await self._merge_hold_last(symbol, expiry, anchor, now_rows, then_rows)

        # asof = the historical instant, else the live DB anchor (None when neither).
        asof_override = as_of_utc if as_of_utc is not None else (anchor_from_db if anchor_from_db is not None else None)
        # Live spot only applies to the live snapshot; historical uses the stored underlying.
        eff_live_spot = live_spot if as_of_utc is None else None
        return _assemble(timeframe, expiry, now_rows, then_rows, asof_override, now_utc, eff_live_spot)

    async def get_multi(
        self,
        timeframes: list[str],
        expiry: date,
        symbol: str | None = None,
        live_spot: float | None = None,
        as_of: datetime | None = None,
        atm_window: int | None = None,
    ) -> MultiTFResponse:
        """One row per timeframe (call/put OI change) + shared level ratio/pcr/spot/atm.

        ``as_of`` (tz-aware) computes the grid as of a historical instant (for the
        replay clock / a picked date); omit it for the live latest snapshot. Shares
        a single "now" snapshot across all timeframes (1 now-query + N then-queries)
        so it is far cheaper than calling ``get()`` per timeframe. ``atm_window`` (>=0)
        restricts every sum to strikes within ATM ± N (None / <0 = the full chain).
        """
        symbol = (symbol or get_runtime().active_symbol).upper()
        as_of_utc = as_of.astimezone(timezone.utc) if as_of is not None else None
        win = atm_window if (atm_window is not None and atm_window >= 0) else None
        async with self._lock:
            anchor = await self._fetch_anchor_ts(symbol, expiry)
            ref_key = as_of_utc.isoformat() if as_of_utc else (anchor.isoformat() if anchor else "now")
            cache_key = ("multi", tuple(sorted(timeframes)), expiry.isoformat(), symbol, ref_key, win)
            cached = self._cache.get(cache_key)
            if cached is not None:
                if live_spot is not None and as_of_utc is None and live_spot != cached.spot:
                    from dataclasses import replace as dc_replace
                    return dc_replace(cached, spot=live_spot, atm_strike=_atm_strike(symbol, live_spot))
                return cached
            result = await self._compute_multi(timeframes, expiry, symbol, anchor, as_of_utc, live_spot, win)
            self._cache[cache_key] = result
            return result

    async def _compute_multi(
        self,
        timeframes: list[str],
        expiry: date,
        symbol: str,
        anchor_from_db: datetime | None,
        as_of_utc: datetime | None,
        live_spot: float | None,
        atm_window: int | None = None,
    ) -> MultiTFResponse:
        now_utc = datetime.now(timezone.utc)
        ref = as_of_utc or anchor_from_db or now_utc
        session_floor = market_open_today(ref.astimezone(IST)).astimezone(timezone.utc)
        params = {"symbol": symbol, "expiry": expiry}

        async with AsyncSessionLocal() as s:
            # NOW snapshot — shared across every timeframe.
            if as_of_utc is None:
                now_rows = (
                    await s.execute(_LATEST_SNAPSHOT_SQL, {**params, "floor": session_floor})
                ).mappings().all()
            else:
                now_rows = (
                    await s.execute(
                        _SNAPSHOT_AT_OR_BEFORE_FLOOR_SQL,
                        {**params, "cutoff": as_of_utc, "floor": session_floor},
                    )
                ).mappings().all()
            now_map = {(r["strike"], r["option_type"]): dict(r) for r in now_rows}

            # Resolve spot/ATM up-front so an ATM ± N window can filter strikes before
            # every sum (each timeframe's OI-change AND the shared totals/ratio/pcr).
            stored_spot = next(
                (float(r["underlying"]) for r in now_map.values() if r.get("underlying") is not None),
                None,
            )
            spot = live_spot if (live_spot is not None and as_of_utc is None) else stored_spot
            atm = _atm_strike(symbol, spot)
            if atm_window is not None and atm is not None:
                step = _strike_step(symbol)
                lo, hi = atm - atm_window * step, atm + atm_window * step
                def in_window(strike: int) -> bool:
                    return lo <= strike <= hi
            else:
                def in_window(strike: int) -> bool:
                    return True

            # Earliest-today snapshot (for the per-tf baseline clamp) fetched at most once.
            earliest_rows: list | None = None

            rows_out: list[MultiTFRow] = []
            for tf in timeframes:
                delta_or_marker = parse_timeframe(tf)
                if delta_or_marker == "full_day":
                    then_rows = list(
                        (
                            await s.execute(_SNAPSHOT_AT_OR_AFTER_SQL, {**params, "cutoff": session_floor})
                        ).mappings().all()
                    )
                else:
                    assert isinstance(delta_or_marker, timedelta)
                    cutoff = ref - delta_or_marker
                    then_rows = list(
                        (
                            await s.execute(
                                _SNAPSHOT_AT_OR_BEFORE_FLOOR_SQL,
                                {**params, "cutoff": cutoff, "floor": session_floor},
                            )
                        ).mappings().all()
                    )
                    then_keys = {(r["strike"], r["option_type"]) for r in then_rows}
                    missing = set(now_map.keys()) - then_keys
                    if missing:
                        if earliest_rows is None:
                            earliest_rows = (
                                await s.execute(
                                    _SNAPSHOT_AT_OR_AFTER_SQL, {**params, "cutoff": session_floor}
                                )
                            ).mappings().all()
                        then_rows += [
                            r for r in earliest_rows
                            if (r["strike"], r["option_type"]) in missing
                        ]
                then_map = {(r["strike"], r["option_type"]): dict(r) for r in then_rows}
                ce_chg = 0
                pe_chg = 0
                for (strike, otype), nrow in now_map.items():
                    if not in_window(strike):
                        continue
                    trow = then_map.get((strike, otype))
                    d = int(nrow["oi"]) - (int(trow["oi"]) if trow else 0)
                    if otype == "CE":
                        ce_chg += d
                    else:
                        pe_chg += d
                rows_out.append(
                    MultiTFRow(
                        timeframe=tf,
                        call_oi_change=ce_chg,
                        put_oi_change=pe_chg,
                        oi_change_ratio=_safe_ratio(ce_chg, pe_chg),
                    )
                )

        total_ce = sum(int(r["oi"]) for k, r in now_map.items() if k[1] == "CE" and in_window(k[0]))
        total_pe = sum(int(r["oi"]) for k, r in now_map.items() if k[1] == "PE" and in_window(k[0]))
        max_ts: datetime | None = None
        for r in now_map.values():
            if max_ts is None or r["ts"] > max_ts:
                max_ts = r["ts"]
        asof_ts = as_of_utc or max_ts or ref
        return MultiTFResponse(
            symbol=symbol,
            expiry=expiry.isoformat(),
            asof=asof_ts.astimezone(IST).isoformat(),
            computed_at=now_utc.astimezone(IST).isoformat(),
            spot=spot,
            atm_strike=atm,
            total_call_oi=total_ce,
            total_put_oi=total_pe,
            ratio=_safe_ratio(total_ce, total_pe),
            pcr=_safe_ratio(total_pe, total_ce),
            rows=rows_out,
        )

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
