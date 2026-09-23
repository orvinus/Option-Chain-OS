"""Per-day historical data frames for the backtest runner.

One ``DayFrame`` is prefetched per simulated day (a handful of queries over
``oi_snapshots_unified``); every per-minute evaluation then runs entirely in
memory. The in-memory math MUST reproduce the live SQL semantics exactly:

- ``_TIMESERIES_SQL`` (services/oi_timeseries.py): per-strike ``last(oi, ts)``
  per minute, gapfilled + locf, with WINDOW-SCOPED first-seen seeding — a
  strike whose first tick is at minute *k* contributes its first observed OI
  to buckets ``[0..k)`` **only for cursors ≥ k**. Totals are therefore
  cursor-dependent, not prefix-stable: the frame recomputes per cursor rather
  than slicing a whole-day series (slicing would leak future basket
  membership into the past — a silent lookahead).
- ``real_end`` clamp: buckets past the basket's last actual tick are dropped
  (a data outage must look like a stopped series, not a flat healthy one).
- ``_LAST_SPOT_SQL`` has NO lower time bound — before the day's first spot
  tick the live path resolves ATM from the PREVIOUS session's last spot.
  ``preopen_spot`` reproduces that.
- ``_PREMIUM_ONE_MINUTE_SQL`` returns None for a silent minute (no ltp
  ticks); the orchestrator skips the bar. The frame keeps those minutes None.

Strike picking (``pick_strike_in_band``) mirrors the live trailing-15-minute
last-LTP query, with two documented, deterministic refinements: it reads only
CLOSED minutes (live's ``NOW()`` sees a few seconds of the forming minute —
using the full forming bar here would be lookahead), and premium ties resolve
to the LOWEST strike (live has no ORDER BY, so its tie winner is row-order
luck).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from sqlalchemy import text

from ...core.db import AsyncSessionLocal
from ...core.time_utils import IST, ist_naive_to_utc
from ...services.oi_timeseries import OITimeseriesPoint, _safe_ratio
from ..series import (
    _CRORE,
    OIChangePair,
    RatioPair,
    oi_change_pair_from_points,
    ratio_pair_from_points,
    whole_units_cr,
)

SESSION_OPEN = time(9, 15)
SESSION_LAST_MINUTE = time(15, 39)   # last 1-min bucket start inside the frame
MINUTES_PER_DAY = 385                # 09:15 .. 15:39 inclusive

Key = tuple[int, str]                # (strike, option_type)


# ── expiry resolution ────────────────────────────────────────────────────────

_EXPIRIES_LIVE_SQL = text(
    """
    SELECT DISTINCT symbol, expiry FROM option_oi_snapshots
    WHERE option_type IN ('CE','PE') AND symbol = ANY(:symbols)
    """
)
# Two separate table scans on purpose — a DISTINCT over the unified view's
# UNION caused a pool-exhaustion incident (see api/expiries.py).
_EXPIRIES_ARCHIVE_SQL = text(
    """
    SELECT DISTINCT symbol, expiry FROM oi_archive_bars
    WHERE option_type IN ('CE','PE') AND symbol = ANY(:symbols)
    """
)


@dataclass
class ExpiryMap:
    """date → the contract the platform would trade that day: the nearest
    stored expiry ≥ the day (the live ``resolve_expiry`` is now-based and
    unusable for historical days)."""
    by_symbol: dict[str, list[date]]

    def for_day(self, symbol: str, day: date) -> Optional[date]:
        for e in self.by_symbol.get(symbol, []):
            if e >= day:
                return e
        return None


async def load_expiry_map(symbols: list[str]) -> ExpiryMap:
    found: dict[str, set[date]] = {s: set() for s in symbols}
    async with AsyncSessionLocal() as s:
        for sql in (_EXPIRIES_LIVE_SQL, _EXPIRIES_ARCHIVE_SQL):
            rows = (await s.execute(sql, {"symbols": symbols})).all()
            for sym, exp in rows:
                found.setdefault(sym, set()).add(exp)
    return ExpiryMap(by_symbol={k: sorted(v) for k, v in found.items()})


# ── the day frame ────────────────────────────────────────────────────────────

_CHAIN_MINUTES_SQL = text(
    """
    SELECT
        strike,
        option_type,
        time_bucket('1 minute', ts) AS bucket,
        last(oi, ts)                                            AS oi,
        first(ltp, ts) FILTER (WHERE ltp IS NOT NULL)           AS o,
        MAX(ltp)                                                AS h,
        MIN(ltp)                                                AS l,
        last(ltp, ts)  FILTER (WHERE ltp IS NOT NULL)           AS c
    FROM oi_snapshots_unified
    WHERE symbol = :symbol
      AND expiry = :expiry
      AND option_type IN ('CE','PE')
      AND ts >= :open_ts
      AND ts <= :close_ts
    GROUP BY 1, 2, 3
    ORDER BY 3
    """
)

_SPOT_MINUTES_SQL = text(
    """
    SELECT time_bucket('1 minute', ts) AS bucket, last(underlying, ts) AS spot
    FROM oi_snapshots_unified
    WHERE symbol = :symbol
      AND expiry = :expiry
      AND underlying IS NOT NULL
      AND ts >= :open_ts
      AND ts <= :close_ts
    GROUP BY 1
    """
)

_PREOPEN_SPOT_SQL = text(
    """
    SELECT underlying
    FROM oi_snapshots_unified
    WHERE symbol = :symbol
      AND expiry = :expiry
      AND underlying IS NOT NULL
      AND ts < :open_ts
    ORDER BY ts DESC
    LIMIT 1
    """
)

_STRIKE_BOUNDS_SQL = text(
    """
    SELECT MIN(strike) AS lo, MAX(strike) AS hi
    FROM oi_snapshots_unified
    WHERE symbol = :symbol AND expiry = :expiry
    """
)


@dataclass
class DayFrame:
    trade_date: date
    symbol: str
    expiry: date
    open_utc: datetime                      # 09:15 IST as UTC
    minute_iso: list[str]                   # IST ISO per slot (bucket start)
    oi: dict[Key, list[Optional[int]]]      # raw per-minute last(oi) (no fill)
    ohlc: dict[Key, list[Optional[tuple[float, float, float, float]]]]
    has_tick: dict[Key, list[bool]]         # any row that minute
    first_tick: dict[Key, int]              # index of first row
    first_oi: dict[Key, Optional[int]]      # first non-null OI (window seed)
    first_oi_idx: dict[Key, int]            # index of that first non-null OI
    oi_locf: dict[Key, list[Optional[int]]]  # last non-null OI at ≤ idx
    last_close: dict[Key, list[Optional[float]]]  # last ltp close at ≤ idx
    spot: list[Optional[float]]             # locf'd underlying per slot
    preopen_spot: Optional[float]
    strike_bounds: Optional[tuple[int, int]]
    strike_step: int
    minutes_with_data: int = 0
    spotless: bool = False
    # Session length ON THIS DATE (385 from 2026-08-03, 375 before — the
    # exchange moved the close 15:30→15:40). Arrays keep MINUTES_PER_DAY slots
    # (the max); slots past minutes_per_day are never traded minutes.
    minutes_per_day: int = MINUTES_PER_DAY
    # last_tick_upto[i] = last slot ≤ i with ANY basket tick (-1 = none yet) —
    # the backtest's feed-staleness signal (live: seconds since the freshest
    # stored option row; here: whole minutes since the last tick in the frame).
    last_tick_upto: list = field(default_factory=list)
    _cursor_cache: dict = field(default_factory=dict)
    # ── incremental-totals machinery (built once in build_day_frame) ──
    # per minute: keys whose has_tick is set (drives the real_end clamp);
    # per minute: (key, Δeff) OI deltas for t > first_tick (eff = locf, with
    # the window-scoped first-seen seed standing in before first_oi_idx);
    # per minute: contributing keys whose FIRST tick is that minute.
    minute_ticks: list = field(default_factory=list)
    minute_deltas: list = field(default_factory=list)
    joiners: list = field(default_factory=list)
    _basket_states: dict = field(default_factory=dict)

    # ── indexing ──
    def idx(self, ist_naive: datetime) -> int:
        """Slot for an IST-naive wall-clock minute (clamped to the grid)."""
        base = ist_naive.hour * 60 + ist_naive.minute
        return max(0, min(MINUTES_PER_DAY - 1, base - (9 * 60 + 15)))

    def cursor_idx(self, now_ist_naive: datetime) -> int:
        return self.idx(now_ist_naive)


def _slot_of(bucket_utc: datetime, open_utc: datetime) -> Optional[int]:
    if bucket_utc.tzinfo is None:
        bucket_utc = bucket_utc.replace(tzinfo=timezone.utc)
    i = int((bucket_utc - open_utc).total_seconds() // 60)
    if 0 <= i < MINUTES_PER_DAY:
        return i
    return None


async def load_day_frame(
    symbol: str, expiry: date, trade_date: date, strike_step: int
) -> Optional[DayFrame]:
    open_utc = ist_naive_to_utc(datetime.combine(trade_date, SESSION_OPEN))
    close_utc = open_utc + timedelta(minutes=MINUTES_PER_DAY)  # exclusive of 15:40

    params = {
        "symbol": symbol,
        "expiry": expiry,
        "open_ts": open_utc,
        "close_ts": close_utc - timedelta(seconds=1),
    }
    async with AsyncSessionLocal() as s:
        chain = (await s.execute(_CHAIN_MINUTES_SQL, params)).mappings().all()
        spot_rows = (await s.execute(_SPOT_MINUTES_SQL, params)).mappings().all()
        preopen = (await s.execute(_PREOPEN_SPOT_SQL, params)).scalar()
        b = (await s.execute(
            _STRIKE_BOUNDS_SQL, {"symbol": symbol, "expiry": expiry}
        )).mappings().first()

    bounds = None
    if b and b["lo"] is not None and b["hi"] is not None:
        bounds = (int(b["lo"]), int(b["hi"]))

    return build_day_frame(
        trade_date=trade_date,
        symbol=symbol,
        expiry=expiry,
        open_utc=open_utc,
        chain=chain,          # RowMapping supports key access — no dict copy
        spot_rows=spot_rows,
        preopen=float(preopen) if preopen is not None else None,
        bounds=bounds,
        strike_step=strike_step,
    )


def build_day_frame(
    *,
    trade_date: date,
    symbol: str,
    expiry: date,
    open_utc: datetime,
    chain: list[dict],
    spot_rows: list[dict],
    preopen: Optional[float],
    bounds: Optional[tuple[int, int]],
    strike_step: int,
) -> Optional[DayFrame]:
    """Pure frame assembly from raw minute rows — separated from the SQL so
    the derived-array semantics (locf, first-seen, spot carry) are unit-
    testable against brute-force references."""
    if not chain:
        return None

    oi: dict[Key, list[Optional[int]]] = {}
    ohlc: dict[Key, list[Optional[tuple[float, float, float, float]]]] = {}
    has_tick: dict[Key, list[bool]] = {}
    tick_minutes: set[int] = set()

    for r in chain:
        slot = _slot_of(r["bucket"], open_utc)
        if slot is None:
            continue
        key: Key = (int(r["strike"]), str(r["option_type"]))
        if key not in oi:
            oi[key] = [None] * MINUTES_PER_DAY
            ohlc[key] = [None] * MINUTES_PER_DAY
            has_tick[key] = [False] * MINUTES_PER_DAY
        has_tick[key][slot] = True
        tick_minutes.add(slot)
        if r["oi"] is not None:
            oi[key][slot] = int(r["oi"])
        if r["o"] is not None:
            ohlc[key][slot] = (
                float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"])
            )

    first_tick: dict[Key, int] = {}
    first_oi: dict[Key, Optional[int]] = {}
    first_oi_idx: dict[Key, int] = {}
    oi_locf: dict[Key, list[Optional[int]]] = {}
    last_close: dict[Key, list[Optional[float]]] = {}
    for key, arr in oi.items():
        ticks = has_tick[key]
        first_tick[key] = next(i for i, t in enumerate(ticks) if t)
        f_oi: Optional[int] = None
        f_idx = MINUTES_PER_DAY
        locf_arr: list[Optional[int]] = [None] * MINUTES_PER_DAY
        close_arr: list[Optional[float]] = [None] * MINUTES_PER_DAY
        carry_oi: Optional[int] = None
        carry_close: Optional[float] = None
        bars = ohlc[key]
        for i in range(MINUTES_PER_DAY):
            if arr[i] is not None:
                carry_oi = arr[i]
                if f_oi is None:
                    f_oi, f_idx = arr[i], i
            locf_arr[i] = carry_oi
            if bars[i] is not None:
                carry_close = bars[i][3]
            close_arr[i] = carry_close
        first_oi[key] = f_oi
        first_oi_idx[key] = f_idx
        oi_locf[key] = locf_arr
        last_close[key] = close_arr

    # Spot: locf across the day; before the first tick, the previous session's
    # last stored spot (exactly what the un-bounded live _LAST_SPOT_SQL sees).
    spot: list[Optional[float]] = [None] * MINUTES_PER_DAY
    raw_spot: list[Optional[float]] = [None] * MINUTES_PER_DAY
    for r in spot_rows:
        slot = _slot_of(r["bucket"], open_utc)
        if slot is not None and r["spot"] is not None:
            raw_spot[slot] = float(r["spot"])
    carry: Optional[float] = float(preopen) if preopen is not None else None
    preopen_f = carry
    any_spot = False
    for i in range(MINUTES_PER_DAY):
        if raw_spot[i] is not None:
            carry = raw_spot[i]
            any_spot = True
        spot[i] = carry

    # ── incremental-event streams (once per frame; O(total ticks)) ──
    minute_ticks: list[list[Key]] = [[] for _ in range(MINUTES_PER_DAY)]
    minute_deltas: list[list[tuple[Key, int]]] = [[] for _ in range(MINUTES_PER_DAY)]
    joiners: list[list[Key]] = [[] for _ in range(MINUTES_PER_DAY)]
    for key, ticks in has_tick.items():
        ft = first_tick[key]
        seed = first_oi[key]
        locf_arr = oi_locf[key]
        if seed is not None:
            joiners[ft].append(key)
        prev_eff = None
        for t in range(ft, MINUTES_PER_DAY):
            if ticks[t]:
                minute_ticks[t].append(key)
            if seed is None:
                continue
            v = locf_arr[t]
            eff = seed if v is None else v
            if t > ft and prev_eff is not None and eff != prev_eff:
                minute_deltas[t].append((key, eff - prev_eff))
            prev_eff = eff

    from ...core.time_utils import SESSION_OPEN_MIN, session_close_min

    last_tick_upto: list[int] = []
    last = -1
    for i in range(MINUTES_PER_DAY):
        if i in tick_minutes:
            last = i
        last_tick_upto.append(last)

    return DayFrame(
        trade_date=trade_date,
        symbol=symbol,
        expiry=expiry,
        open_utc=open_utc,
        minutes_per_day=session_close_min(trade_date) - SESSION_OPEN_MIN,
        last_tick_upto=last_tick_upto,
        minute_iso=[
            (open_utc + timedelta(minutes=i)).astimezone(IST).isoformat()
            for i in range(MINUTES_PER_DAY)
        ],
        oi=oi,
        ohlc=ohlc,
        has_tick=has_tick,
        first_tick=first_tick,
        first_oi=first_oi,
        first_oi_idx=first_oi_idx,
        oi_locf=oi_locf,
        last_close=last_close,
        spot=spot,
        preopen_spot=preopen_f,
        strike_bounds=bounds,
        strike_step=strike_step,
        minutes_with_data=len(tick_minutes),
        spotless=not any_spot,
        minute_ticks=minute_ticks,
        minute_deltas=minute_deltas,
        joiners=joiners,
    )


# ── per-cursor series (mirrors fetch_oi_timeseries semantics exactly) ───────

def resolve_basket(
    frame: DayFrame, atm_window: int, last_idx: int
) -> Optional[tuple[int, int, Optional[float]]]:
    """(strike_min, strike_max, spot) — mirrors ``_resolve_strike_basket``:
    window < 0 or no spot → the full stored chain; else ATM ± window·step.

    ``last_idx`` is the last CLOSED minute (−1 → pre-open: the previous
    session's spot). Reading the cursor's own forming minute here would be
    up to 59 s of lookahead the live instant never had.
    """
    if atm_window < 0:
        if frame.strike_bounds is None:
            return None
        return frame.strike_bounds[0], frame.strike_bounds[1], None
    spot = frame.spot[last_idx] if last_idx >= 0 else frame.preopen_spot
    if spot is None:
        if frame.strike_bounds is None:
            return None
        return frame.strike_bounds[0], frame.strike_bounds[1], None
    step = frame.strike_step
    atm = round(spot / step) * step
    return int(atm - atm_window * step), int(atm + atm_window * step), spot


class _BasketState:
    """Forward-only incremental totals for one strike window.

    Replaces the per-cursor O(minutes × members) recompute (profiled as the
    #2 hotspot) with O(events) advancement while reproducing the live SQL's
    semantics EXACTLY: window-scoped membership, first-seen seeding (a
    contributing key's join retro-adds its seed to every earlier bucket) and
    the real_end clamp (emission stops at the last RANGE tick). Verified by
    the brute-force unit test and the 633-check SQL parity script.
    """

    __slots__ = ("smin", "smax", "upto", "members", "ce", "pe", "last_tick")

    def __init__(self, smin: int, smax: int) -> None:
        self.smin = smin
        self.smax = smax
        self.upto = -1
        self.members: set[Key] = set()
        self.ce: list[int] = []
        self.pe: list[int] = []
        self.last_tick = -1

    def advance(self, frame: "DayFrame", last_idx: int) -> None:
        smin, smax = self.smin, self.smax
        ce, pe, members = self.ce, self.pe, self.members
        for m in range(self.upto + 1, last_idx + 1):
            dce = 0
            dpe = 0
            for k in frame.joiners[m]:
                if not (smin <= k[0] <= smax):
                    continue
                seed = frame.first_oi[k]
                foi = frame.first_oi_idx[k]
                locf_arr = frame.oi_locf[k]
                arr = ce if k[1] == "CE" else pe

                def _contrib(j: int) -> int:
                    v = locf_arr[j] if j >= foi else None
                    return seed if v is None else v

                # Retro-add the join contribution to every existing bucket
                # (COALESCE(p.oi, f.oi0) for the leading gapfilled rows).
                for j in range(m):
                    arr[j] += _contrib(j)
                # Bucket m itself extends from bucket m−1 (which just gained
                # the joiner's m−1 contribution) — add only the INCREMENT.
                d = _contrib(m) - (_contrib(m - 1) if m > 0 else 0)
                if k[1] == "CE":
                    dce += d
                else:
                    dpe += d
                members.add(k)
            for k, d in frame.minute_deltas[m]:
                if k in members:
                    if k[1] == "CE":
                        dce += d
                    else:
                        dpe += d
            ce.append((ce[m - 1] if m > 0 else 0) + dce)
            pe.append((pe[m - 1] if m > 0 else 0) + dpe)
            # real_end: ANY tick in the strike RANGE (null-OI keys included).
            for k in frame.minute_ticks[m]:
                if smin <= k[0] <= smax:
                    self.last_tick = m
                    break
            self.upto = m


def _basket_state(frame: DayFrame, smin: int, smax: int, last_idx: int) -> _BasketState:
    st = frame._basket_states.get((smin, smax))
    if st is None:
        st = _BasketState(smin, smax)
        frame._basket_states[(smin, smax)] = st
    st.advance(frame, last_idx)
    return st


def totals_points(
    frame: DayFrame, strike_min: int, strike_max: int, last_idx: int
) -> list[OITimeseriesPoint]:
    """OITimeseriesPoint rows for the window [session open, minute
    ``last_idx``] (the last CLOSED minute) — served from the incremental
    basket state; emission skips leading value-less buckets exactly like the
    SQL's COALESCE-not-null filter (no contributing member joined yet)."""
    if last_idx < 0:
        return []
    st = _basket_state(frame, strike_min, strike_max, last_idx)
    if not st.members:
        return []
    n = min(st.last_tick, last_idx) + 1
    return [
        OITimeseriesPoint(
            ts=frame.minute_iso[m],
            total_call_oi=st.ce[m],
            total_put_oi=st.pe[m],
            ratio=_safe_ratio(st.ce[m], st.pe[m]),
            pcr=_safe_ratio(st.pe[m], st.ce[m]),
        )
        for m in range(n)
    ]


def _pairs_at(
    frame: DayFrame, atm_window: int, now_ist_naive: datetime
) -> tuple[Optional[OIChangePair], Optional[RatioPair]]:
    """Both engine input pairs from the incremental basket state in ONE pass.

    Output-identical to feeding ``totals_points`` through the pure transforms
    (pinned by test_fast_pairs_match_pure_transforms + the SQL parity script)
    but without constructing 385 point objects and re-parsing 385 ISO
    timestamps per evaluation minute — the profiled O(minutes²) tax.
    """
    from ..engines.mqae import normalized_pair

    base = now_ist_naive.hour * 60 + now_ist_naive.minute - (9 * 60 + 15)
    last_idx = min(base - 1, MINUTES_PER_DAY - 1)   # closed minutes only
    cache_key = ("pairs", atm_window, last_idx)
    hit = frame._cursor_cache.get(cache_key)
    if hit is not None:
        return hit
    result: tuple[Optional[OIChangePair], Optional[RatioPair]] = (None, None)
    if last_idx >= 0:
        basket = resolve_basket(frame, atm_window, last_idx)
        if basket is not None:
            smin, smax, spot = basket
            st = _basket_state(frame, smin, smax, last_idx)
            n = min(st.last_tick, last_idx) + 1
            if st.members and n >= 1:
                ce, pe = st.ce, st.pe
                base_ce, base_pe = ce[0], pe[0]
                timestamps = frame.minute_iso[:n]
                call_vals = [
                    round((ce[i] - base_ce) / _CRORE * 100) / 100 for i in range(n)
                ]
                put_vals = [
                    round((pe[i] - base_pe) / _CRORE * 100) / 100 for i in range(n)
                ]
                oic = OIChangePair(
                    timestamps=timestamps,
                    call_change_cr=call_vals,
                    put_change_cr=put_vals,
                    strike_min=smin,
                    strike_max=smax,
                    spot=spot,
                    call_change_full_cr=[
                        whole_units_cr((ce[i] - base_ce) / _CRORE) for i in range(n)
                    ],
                    put_change_full_cr=[
                        whole_units_cr((pe[i] - base_pe) / _CRORE) for i in range(n)
                    ],
                )
                green_raw = [pe[i] / ce[i] if ce[i] else None for i in range(n)]
                yellow_raw = [ce[i] / pe[i] if pe[i] else None for i in range(n)]
                green, yellow = normalized_pair(green_raw, yellow_raw)
                rp = None
                if green:
                    rp = RatioPair(
                        timestamps=timestamps[n - len(green):],
                        green_pcr=green,
                        yellow_ratio=yellow,
                        strike_min=smin,
                        strike_max=smax,
                        spot=spot,
                    )
                result = (oic, rp)
    frame._cursor_cache[cache_key] = result
    return result


def oi_change_pair_at(
    frame: DayFrame, atm_window: int, now_ist_naive: datetime
) -> Optional[OIChangePair]:
    return _pairs_at(frame, atm_window, now_ist_naive)[0]


def ratio_pair_at(
    frame: DayFrame, atm_window: int, now_ist_naive: datetime
) -> Optional[RatioPair]:
    return _pairs_at(frame, atm_window, now_ist_naive)[1]


def minute_bar(
    frame: DayFrame, strike: int, option_type: str, target_ist_naive: datetime
) -> Optional[tuple[datetime, float, float, float, float]]:
    """(ts, o, h, l, c) for one CLOSED minute, or None when the contract had
    no ltp tick that minute — the orchestrator skips the bar, exactly like
    the live ``fetch_premium_minute``."""
    from ...core.time_utils import SESSION_OPEN_MIN, session_last_bar_min

    mins = target_ist_naive.hour * 60 + target_ist_naive.minute
    # Last bar = the date's own final 1-min bucket (15:39 / 15:29).
    if not (SESSION_OPEN_MIN <= mins <= session_last_bar_min(target_ist_naive.date())):
        return None
    i = frame.idx(target_ist_naive)
    bar = frame.ohlc.get((strike, option_type), [None] * MINUTES_PER_DAY)[i]
    if bar is None:
        return None
    return (target_ist_naive, bar[0], bar[1], bar[2], bar[3])


STRIKE_PICK_LOOKBACK_MIN = 15


def band_candidate_strikes(
    frame: DayFrame, band_lo: float, band_hi: float
) -> list[int]:
    """Strikes whose premium (minute close) EVER lies inside
    [``band_lo``, ``band_hi``] during the day — a pure-CPU SUPERSET of every
    strike ``pick_strike_in_band`` can select at any cursor, because the
    picker's trailing last-LTP is always one of these minute closes. Used to
    warm the premium cache with one batched SQL; a miss still falls back to
    the exact single-contract fetch."""
    out: set[int] = set()
    for (strike, _ot), series_ in frame.ohlc.items():
        if strike in out:
            continue
        for t in series_:
            if t is not None:
                c = t[3]
                if c is not None and band_lo <= c <= band_hi:
                    out.add(strike)
                    break
    return sorted(out)


def pick_strikes_in_band(
    frame: DayFrame,
    option_type: str,
    band_min: float,
    band_max: float,
    now_ist_naive: datetime,
    count: int = 1,
) -> list[tuple[int, float]]:
    """Historical twin of ``series.select_strikes_in_band``: last close per
    strike over the trailing 15 CLOSED minutes, band filter, then the ``count``
    strikes nearest the band middle ordered nearest-first; ties → lowest
    strike (deterministic — same rule live now uses)."""
    cursor = frame.cursor_idx(now_ist_naive)
    last_closed = cursor - 1          # the cursor's own minute is still forming
    if last_closed < 0:
        return []
    lo = max(0, last_closed - (STRIKE_PICK_LOOKBACK_MIN - 1))
    mid = (band_min + band_max) / 2
    candidates: list[tuple[int, float]] = []
    for (strike, ot), closes in frame.last_close.items():
        if ot != option_type:
            continue
        ltp = closes[last_closed]
        if ltp is None:
            continue
        # last_close is locf'd from session open — enforce the 15-minute
        # recency window: the strike must have ticked inside it.
        if not any(frame.ohlc[(strike, ot)][i] is not None
                   for i in range(lo, last_closed + 1)):
            continue
        if not (band_min <= ltp <= band_max):
            continue
        candidates.append((strike, ltp))
    candidates.sort(key=lambda t: (abs(t[1] - mid), t[0]))
    return candidates[: max(1, count)]


def pick_strike_in_band(
    frame: DayFrame,
    option_type: str,
    band_min: float,
    band_max: float,
    now_ist_naive: datetime,
) -> Optional[tuple[int, float]]:
    """Single-strike head of ``pick_strikes_in_band`` — kept as the N=1
    regression pin (the parity harness and data tests exercise exactly the
    original single-pick rule through this)."""
    picked = pick_strikes_in_band(frame, option_type, band_min, band_max,
                                  now_ist_naive, 1)
    return picked[0] if picked else None
