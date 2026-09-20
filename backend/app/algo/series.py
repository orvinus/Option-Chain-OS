"""Input-series builders for the entry-filter engines.

Every engine is pure; this module is where data actually comes from. M1 scope:
the OI Structure Engine's input pair — cumulative Call-side and Put-side
open-interest change since market open, one value per CLOSED 1-minute bucket,
in CRORES over a configurable strike basket (ATM ± N, or the full stored
chain when the window is -1).

Built on ``fetch_oi_timeseries`` (services/oi_timeseries.py), which already
solves the two integrity traps: locf carry-forward (a quiet strike contributes
its last OI to every bucket, not just ticked ones) and constant strike-set
membership (a late-arriving strike is seeded backwards so it contributes ZERO
change, never its whole OI as a fake step).

Closed-candle discipline (engine spec §2): the trailing in-progress minute is
dropped here, at the call site, exactly as the spec instructs — the swing
detector has its own confirmation delay but the breakout / red-candle checks
trust their input.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import structlog
from sqlalchemy import bindparam, text

from ..core.db import AsyncSessionLocal
from ..core.time_utils import IST, SESSION_OPEN_MIN, session_close_min, session_last_bar_min
from ..market.symbols import get_registry
from .engines.ump.engine import HtfSeed
from ..services.oi_timeseries import fetch_oi_timeseries, fetch_strike_bounds

log = structlog.get_logger(__name__)

_CRORE = 1e7
_IST_DELTA = timedelta(hours=5, minutes=30)   # IST has no DST — constant offset

_LAST_SPOT_SQL = text(
    """
    SELECT underlying
    FROM oi_snapshots_unified
    WHERE symbol = :symbol
      AND expiry = :expiry
      AND underlying IS NOT NULL
      AND ts <= :to_ts
      AND ts >= CAST(:to_ts AS TIMESTAMPTZ) - INTERVAL '7 days'
    ORDER BY ts DESC
    LIMIT 1
    """
)
# The 7-day floor matters: without it this LIMIT 1 was a 2.4-second top-N
# sort over ~2M rows of BOTH hypertables (measured live 2026-08-18) — and a
# spot older than a week is garbage for ATM resolution anyway.

# Per-contract premium minutes across ALL stored days (live + archive via the
# unified view). A weekly option lives days, not months, so this stays small;
# the HTF feeds (1H / daily / weekly) fold from these minutes in Python.
#
# The 120-day floor is a pure planner aid, NEVER a data cut: no NIFTY/SENSEX
# option contract carries more than ~92 days of data (a monthly's full listing
# life), so results are byte-identical — but without the floor the planner
# opened every chunk of both hypertables (measured live 2026-08-18: 529 ms of
# planning + ~2M rows materialised per call). Do not shrink it below a
# monthly contract's lifetime.
#
# SOURCE PREFERENCE (2026-09-02 TradingView parity audit): a completed minute
# is read from the vendor 1-minute archive (``oi_archive_bars`` — exchange-grade
# open/high/low/close per minute, the same bars TradingView draws) whenever
# that MINUTE exists there; the live tick table fills only the minutes the
# archive lacks, and ALWAYS feeds the current IST day (the nightly top-up /
# boot gap-fill may have archived only part of today — live ticks must never be
# masked mid-session). The old ``oi_snapshots_unified`` path exposed the
# archive's close as ``ltp`` only, so every archived minute folded to
# o=h=l=c: daily/1H highs and lows were understated and the structural-
# reversal set diverged from the chart.
#
# The preference is per MINUTE and not per DAY (fixed 2026-09-12). It used to
# be per day, which silently assumed that a day present in the archive is
# COMPLETE there — and a day is incomplete whenever the nightly top-up was
# interrupted. Measured on NIFTY 23450 CE: the archive held 09:15–13:11 on
# 10 Sep and 09:15–11:54 on 11 Sep, and because those dates existed in the
# archive at all, EVERY live minute of both afternoons was discarded. The
# chart and the engine simply ended at lunchtime. Matching per minute keeps
# the archive's exchange-grade bars wherever they exist — the parity property
# the day rule was protecting — while the live ticks top up the rest, which is
# exactly what the ``arch_today`` branch below has always done for today.
_PREMIUM_MINUTES_TEMPLATE = """
    WITH arch AS (
        SELECT strike, option_type, ts AS bucket,
               COALESCE(open, close) AS o,
               COALESCE(high, close) AS h,
               COALESCE(low, close)  AS l,
               close                 AS c
        FROM oi_archive_bars
        WHERE symbol = :symbol
          AND expiry = :expiry
          AND {contract_filter}
          AND close IS NOT NULL
          AND ts <= :to_ts
          AND ts >= CAST(:to_ts AS TIMESTAMPTZ) - INTERVAL '120 days'
          AND ts < :today_start
    ),
    live AS (
        SELECT strike, option_type,
               time_bucket('1 minute', ts) AS bucket,
               first(ltp, ts) AS o,
               MAX(ltp)       AS h,
               MIN(ltp)       AS l,
               last(ltp, ts)  AS c
        FROM option_oi_snapshots
        WHERE symbol = :symbol
          AND expiry = :expiry
          AND {contract_filter}
          AND ltp IS NOT NULL
          AND src IS NULL
          AND ts <= :to_ts
          AND ts >= CAST(:to_ts AS TIMESTAMPTZ) - INTERVAL '120 days'
        GROUP BY 1, 2, 3
    ),
    -- TODAY's minutes the live feed never saw (boot after the open, a socket
    -- hole) are restored from the vendor's same-day archive by the session
    -- catch-up. Serve those minutes with their real O/H/L/C rather than the
    -- o=h=l=c row the promotion writes into the live table.
    arch_today AS (
        SELECT a.strike, a.option_type, a.ts AS bucket,
               COALESCE(a.open, a.close) AS o,
               COALESCE(a.high, a.close) AS h,
               COALESCE(a.low, a.close)  AS l,
               a.close                   AS c
        FROM oi_archive_bars a
        WHERE a.symbol = :symbol
          AND a.expiry = :expiry
          AND {contract_filter}
          AND a.close IS NOT NULL
          AND a.ts >= :today_start
          AND a.ts <= :to_ts
          AND NOT EXISTS (
              SELECT 1 FROM live lv
              WHERE lv.strike = a.strike
                AND lv.option_type = a.option_type
                AND lv.bucket = a.ts
          )
    )
    SELECT strike, option_type, bucket, o, h, l, c FROM arch
    UNION ALL
    SELECT lv.strike, lv.option_type, lv.bucket, lv.o, lv.h, lv.l, lv.c
    FROM live lv
    WHERE NOT EXISTS (
        SELECT 1 FROM arch a
        WHERE a.strike = lv.strike
          AND a.option_type = lv.option_type
          AND a.bucket = lv.bucket
    )
    UNION ALL
    SELECT strike, option_type, bucket, o, h, l, c FROM arch_today
    ORDER BY 3
"""

_PREMIUM_MINUTES_SQL = text(
    _PREMIUM_MINUTES_TEMPLATE.format(
        contract_filter="strike = :strike AND option_type = :option_type"
    )
)

# Same rows per contract as _PREMIUM_MINUTES_SQL, many contracts in one
# round-trip — the unbounded-lookback plan cost (every chunk of both
# hypertables) is paid once instead of once per contract.
_PREMIUM_MINUTES_BATCH_SQL = text(
    _PREMIUM_MINUTES_TEMPLATE.format(
        contract_filter="strike IN :strikes AND option_type IN ('CE', 'PE')"
    )
).bindparams(bindparam("strikes", expanding=True))

# Exchange OFFICIAL daily closes per contract (NSE F&O bhavcopy via the
# vendor; the last-30-minute weighted average, not the last trade). This is
# the close TradingView's D/W bars carry — and therefore what Pine's
# d1C..d3C / wC resolve to. Verified 2026-09-02 on 12 contract-days: the
# bhavcopy close equalled the chart's daily close to the paisa in every case.
# Two sources feed this, and the ORDER matters. ``td_bhavcopy`` is the vendor's
# copy of the exchange bhavcopy and covers only days a contract actually traded.
# ``nse_settlement`` is NSE's own UDiFF file, which additionally carries every
# LISTED-but-idle day as a flat settlement bar -- exactly what TradingView's D/W
# series plots there and what Pine's d1..d3 / w feeds resolve to. DISTINCT ON
# keeps one row per contract-day, preferring the bhavcopy so traded days keep
# the values we have already verified to the paisa (2026-09-02, 12 contract-days).
_OFFICIAL_SOURCES = ("td_bhavcopy", "nse_settlement")
_OFFICIAL_CLOSES_SQL = text(
    """
    SELECT DISTINCT ON (strike, option_type, trade_date)
           strike, option_type, trade_date, high, low, close, open
    FROM eod_bars
    WHERE symbol = :symbol
      AND expiry = :expiry
      AND strike IN :strikes
      AND option_type IN ('CE', 'PE')
      AND source IN :sources
      AND close IS NOT NULL
      AND close > 0
    ORDER BY strike, option_type, trade_date,
             CASE source WHEN 'td_bhavcopy' THEN 0 ELSE 1 END
    """
).bindparams(
    bindparam("strikes", expanding=True), bindparam("sources", expanding=True)
)


def _today_start_utc(now_utc: datetime) -> datetime:
    """UTC instant of the current IST calendar day's midnight — the
    archive/live source boundary for the premium feeds."""
    ist_day = (now_utc + _IST_DELTA).date()
    return datetime(ist_day.year, ist_day.month, ist_day.day, tzinfo=timezone.utc) - _IST_DELTA


@dataclass
class RatioPair:
    """The MQAE's input contract: the platform's PCR line (Green) and Ratio
    line (Yellow), one value per CLOSED 1-minute bucket, gap-filled by
    hold-last (a bucket with a zero side yields no ratio)."""
    timestamps: list[str]
    green_pcr: list[float]
    yellow_ratio: list[float]
    strike_min: int
    strike_max: int
    spot: Optional[float]
    # Total CE/PE OI per surviving bucket (index-aligned with ``timestamps``).
    # Chart extras only — the engine never reads them; appended LAST with
    # defaults so every kwargs constructor stays valid.
    total_call_oi: list[int] = field(default_factory=list)
    total_put_oi: list[int] = field(default_factory=list)


@dataclass
class OIChangePair:
    """The OI Structure Engine's exact input contract (its spec §2)."""
    timestamps: list[str]          # ISO-8601 IST, bucket START, closed buckets only
    call_change_cr: list[float]    # cumulative CE OI Δ since session open, Crores
    put_change_cr: list[float]     # cumulative PE OI Δ since session open, Crores
    strike_min: int
    strike_max: int
    spot: Optional[float]


async def _resolve_strike_basket(
    symbol: str, expiry: date, atm_window: int, to_ts: Optional[datetime]
) -> Optional[tuple[int, int, Optional[float]]]:
    """(strike_min, strike_max, spot). ATM derives from the last stored
    underlying at/before the window end — never a live quote, so historical
    requests resolve the basket that session actually traded around."""
    if atm_window < 0:
        bounds = await fetch_strike_bounds(symbol, expiry)
        if bounds is None:
            return None
        return bounds[0], bounds[1], None

    entry = get_registry().get(symbol)
    step = entry.strike_step if entry is not None else 50
    async with AsyncSessionLocal() as s:
        spot = (
            await s.execute(
                _LAST_SPOT_SQL,
                {
                    "symbol": symbol,
                    "expiry": expiry,
                    "to_ts": to_ts or datetime.now(timezone.utc),
                },
            )
        ).scalar()
    if spot is None:
        # No stored spot (fresh symbol) — fall back to the full chain rather
        # than guessing an ATM.
        bounds = await fetch_strike_bounds(symbol, expiry)
        if bounds is None:
            return None
        return bounds[0], bounds[1], None
    spot_f = float(spot)
    atm = round(spot_f / step) * step
    return int(atm - atm_window * step), int(atm + atm_window * step), spot_f


def oi_change_pair_from_points(
    points,
    *,
    now_utc: datetime,
    strike_min: int,
    strike_max: int,
    spot: Optional[float],
    include_forming: bool = False,
) -> Optional[OIChangePair]:
    """Pure transform: OITimeseriesPoint rows → the engine input pair.

    Shared seam between the SQL builder below and the backtest day-frame,
    which produces identical point rows in memory — the two paths can never
    drift because this is the only implementation of the math.

    ``include_forming`` keeps the trailing in-progress minute. It exists for
    ONE caller — the Algo Config live stream, which renders an intrabar
    preview of the reading. It defaults False so every trading caller (the
    orchestrator, the backtest, the engine dashboards) is byte-identical to
    before: closed-candle discipline is the no-lookahead invariant and must
    never depend on a caller remembering to opt out.
    """
    timestamps: list[str] = []
    call_vals: list[float] = []
    put_vals: list[float] = []
    base_call: Optional[int] = None
    base_put: Optional[int] = None
    for pt in points:
        bucket_start = datetime.fromisoformat(pt.ts)
        # Closed-candle discipline: a 1-minute bucket is closed once its END
        # has passed. The in-progress minute never reaches the engine.
        if not include_forming and bucket_start + timedelta(minutes=1) > now_utc:
            continue
        if base_call is None:
            base_call = pt.total_call_oi
            base_put = pt.total_put_oi
        timestamps.append(pt.ts)
        call_vals.append(round((pt.total_call_oi - base_call) / _CRORE * 100) / 100)
        put_vals.append(round((pt.total_put_oi - (base_put or 0)) / _CRORE * 100) / 100)

    if not timestamps:
        return None
    return OIChangePair(
        timestamps=timestamps,
        call_change_cr=call_vals,
        put_change_cr=put_vals,
        strike_min=strike_min,
        strike_max=strike_max,
        spot=spot,
    )


async def build_oi_change_pair(
    symbol: str,
    expiry: date,
    atm_window: int,
    *,
    from_ts: Optional[datetime] = None,
    to_ts: Optional[datetime] = None,
    now: Optional[datetime] = None,
    include_forming: bool = False,
) -> Optional[OIChangePair]:
    """The engine input pair, or None when nothing is stored for the scope.

    ``now`` is injectable for tests; it decides which trailing bucket is still
    forming and must be dropped. ``include_forming`` is the live-preview
    opt-in — see ``oi_change_pair_from_points``.
    """
    basket = await _resolve_strike_basket(symbol, expiry, atm_window, to_ts)
    if basket is None:
        return None
    strike_min, strike_max, spot = basket

    ts_resp = await fetch_oi_timeseries(
        symbol, expiry, strike_min, strike_max, bucket="1m",
        from_ts=from_ts, to_ts=to_ts,
    )
    if not ts_resp.points:
        return None

    return oi_change_pair_from_points(
        ts_resp.points,
        now_utc=now or datetime.now(timezone.utc),
        strike_min=strike_min,
        strike_max=strike_max,
        spot=spot,
        include_forming=include_forming,
    )


@dataclass
class PremiumMinute:
    ts: datetime            # bucket start, tz-aware UTC
    o: float
    h: float
    l: float
    c: float


@dataclass
class PremiumHistory:
    """One option contract's premium series, split for the UMP engine:
    the evaluation session's minutes plus the higher-timeframe context
    derived from the PRIOR days (Pine feeds: daily[1..3] and last week)."""
    session_minutes: list[PremiumMinute]
    prior_h1: list[tuple[float, float, float, float]]      # (o,h,l,c) chronological
    daily: list[tuple[float, float, float]]                # (H,L,C) newest first, ≤3
    weekly: Optional[tuple[float, float, float]]           # previous ISO week (H,L,C)
    strike: int
    option_type: str


async def resolve_atm_strike(
    symbol: str, expiry: date, to_ts: Optional[datetime] = None
) -> Optional[int]:
    """ATM strike from the last stored underlying at/before ``to_ts``."""
    entry = get_registry().get(symbol)
    step = entry.strike_step if entry is not None else 50
    async with AsyncSessionLocal() as s:
        spot = (
            await s.execute(
                _LAST_SPOT_SQL,
                {
                    "symbol": symbol,
                    "expiry": expiry,
                    "to_ts": to_ts or datetime.now(timezone.utc),
                },
            )
        ).scalar()
    if spot is None:
        return None
    return int(round(float(spot) / step) * step)


async def fetch_premium_minutes(
    symbol: str,
    expiry: date,
    strike: int,
    option_type: str,
    to_ts: datetime,
) -> list[PremiumMinute]:
    """All 1-minute premium candles for one contract with ticks at/before
    ``to_ts`` — the raw feed for ``premium_history_from_minutes``. Cached per
    contract by the backtest (one SQL per contract per day instead of one per
    hunt start)."""
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(
                _PREMIUM_MINUTES_SQL,
                {
                    "symbol": symbol,
                    "expiry": expiry,
                    "strike": strike,
                    "option_type": option_type,
                    "to_ts": to_ts,
                    "today_start": _today_start_utc(datetime.now(timezone.utc)),
                },
            )
        ).mappings().all()
    minutes: list[PremiumMinute] = []
    for r in rows:
        b = r["bucket"]
        if getattr(b, "tzinfo", None) is None:
            b = b.replace(tzinfo=timezone.utc)
        minutes.append(
            PremiumMinute(ts=b, o=float(r["o"]), h=float(r["h"]), l=float(r["l"]), c=float(r["c"]))
        )
    return minutes


def aggregate_minutes(
    minutes: list[PremiumMinute], bucket_s: int
) -> list[dict]:
    """Fold a chronological premium series into display candles of
    ``bucket_s`` seconds (5 / 60 / 300 / 3600 / 86400 / 604800 …) with
    date-qualified keys so day boundaries can never alias.

    Three regimes, matching TradingView's NSE bars:

    - **sub-minute** — folded on the wall clock from the 1-second rows.
    - **a minute up to an hour** — SESSION-ANCHORED at 09:15 (10m = 09:15,
      09:25 …; 30m = 09:15, 09:45 …; 1h = 09:15, 10:15 …). 5m/15m (the entry
      timeframes) coincide with the wall clock.
    - **a day or longer** — CALENDAR periods, not intra-day arithmetic: 1d is
      one candle per session stamped at that date's 09:15, and 1W is one
      candle per ISO week stamped at its MONDAY 09:15. Doing this by seconds
      would collapse a week onto its individual days (every bar of every day
      floors to the same 09:15 offset), so the period is taken from the
      calendar and only the stamp comes from the session open.

    ``bucket_s`` ≤ the series' own cadence returns the rows unchanged. Pure;
    used by the UMP chart for both the entry-timeframe candles and the display
    interval selector."""
    out: list[dict] = []
    cur: Optional[list[float]] = None
    cur_key: Optional[str] = None
    anchor = SESSION_OPEN_MIN * 60
    _DAY_S = 86400
    for m in minutes:
        ist = m.ts.astimezone(IST)
        secs = ist.hour * 3600 + ist.minute * 60 + ist.second
        if bucket_s >= _DAY_S:
            # Calendar period: the whole session (1d) or the whole ISO week
            # (1W, stamped at its Monday). IST has no DST, so the day shift is
            # safe on a tz-aware value.
            base = ist - timedelta(days=ist.weekday()) if bucket_s >= 7 * _DAY_S else ist
            start = base.replace(
                hour=anchor // 3600, minute=(anchor % 3600) // 60,
                second=0, microsecond=0,
            )
        else:
            if bucket_s <= 1:
                bsecs = secs
            elif bucket_s >= 60 and secs >= anchor:
                bsecs = anchor + ((secs - anchor) // bucket_s) * bucket_s
            else:
                bsecs = (secs // bucket_s) * bucket_s
            start = ist.replace(
                hour=bsecs // 3600, minute=(bsecs % 3600) // 60, second=bsecs % 60,
                microsecond=0,
            )
        key = start.isoformat()
        if key != cur_key:
            if cur is not None:
                out.append({"ts": cur_key, "o": cur[0], "h": cur[1], "l": cur[2], "c": cur[3]})
            cur = [m.o, m.h, m.l, m.c]
            cur_key = key
        else:
            assert cur is not None
            cur[1] = max(cur[1], m.h)
            cur[2] = min(cur[2], m.l)
            cur[3] = m.c
    if cur is not None:
        out.append({"ts": cur_key, "o": cur[0], "h": cur[1], "l": cur[2], "c": cur[3]})
    return out


# 1-second premium series for ONE contract inside ONE bounded window — the
# UMP chart's 1s display interval. Reads the live tick table only (the
# archive is 1-minute OHLC; sub-minute detail exists nowhere else), pure
# live rows (``src IS NULL``), on the (expiry, strike, option_type, ts)
# index. A full session is ≤ 23,100 rows; callers bound the window to the
# cursor's session day.
_PREMIUM_SECONDS_SQL = text(
    """
    SELECT time_bucket('1 second', ts) AS bucket,
           first(ltp, ts) AS o, MAX(ltp) AS h, MIN(ltp) AS l, last(ltp, ts) AS c
    FROM option_oi_snapshots
    WHERE symbol = :symbol
      AND expiry = :expiry
      AND strike = :strike
      AND option_type = :option_type
      AND ltp IS NOT NULL
      AND src IS NULL
      AND ts >= :from_ts
      AND ts <= :to_ts
    GROUP BY 1
    ORDER BY 1
    """
)


async def fetch_premium_seconds(
    symbol: str,
    expiry: date,
    strike: int,
    option_type: str,
    from_ts: datetime,
    to_ts: datetime,
) -> list[PremiumMinute]:
    """1-second OHLC of one contract in ``[from_ts, to_ts]`` (UTC). Empty
    when the window predates the live table's retention or the day was
    served by the archive only — the caller falls back to 1-minute bars."""
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(
                _PREMIUM_SECONDS_SQL,
                {
                    "symbol": symbol,
                    "expiry": expiry,
                    "strike": strike,
                    "option_type": option_type,
                    "from_ts": from_ts,
                    "to_ts": to_ts,
                },
            )
        ).mappings().all()
    out: list[PremiumMinute] = []
    for r in rows:
        b = r["bucket"]
        if getattr(b, "tzinfo", None) is None:
            b = b.replace(tzinfo=timezone.utc)
        out.append(
            PremiumMinute(ts=b, o=float(r["o"]), h=float(r["h"]), l=float(r["l"]), c=float(r["c"]))
        )
    return out


# (high, low, close, open). ``open`` is APPENDED, not inserted, so the legacy
# 3-tuple form still unpacks everywhere it is read (see ``_official_entry``);
# only the chart's settlement-derived candles need it.
OfficialDay = tuple[Optional[float], Optional[float], float, Optional[float]]


async def fetch_official_closes_batch(
    symbol: str, expiry: date, strikes: list[int]
) -> dict[tuple[int, str], dict[date, OfficialDay]]:
    """Official (bhavcopy) daily (high, low, close) for many strikes of one
    expiry, keyed (strike, option_type) → {IST trade date: (h, l, c)}.
    Contracts without rows map to an empty dict (the engine then folds the
    day from its 1-minute bars). High/low may be None on a malformed row;
    the close is always present."""
    out: dict[tuple[int, str], dict[date, OfficialDay]] = {}
    for st in strikes:
        out[(int(st), "CE")] = {}
        out[(int(st), "PE")] = {}
    if not strikes:
        return out
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(
                _OFFICIAL_CLOSES_SQL,
                {"symbol": symbol, "expiry": expiry, "strikes": strikes,
                 "sources": list(_OFFICIAL_SOURCES)},
            )
        ).all()
    for strike, ot, d, h, l, c, o in rows:
        out.setdefault((int(strike), ot), {})[d] = (
            float(h) if h is not None and h > 0 else None,
            float(l) if l is not None and l > 0 else None,
            float(c),
            float(o) if o is not None and o > 0 else None,
        )
    return out


async def fetch_official_closes(
    symbol: str, expiry: date, strike: int, option_type: str
) -> dict[date, OfficialDay]:
    """One contract's official daily closes (see the batch form)."""
    got = await fetch_official_closes_batch(symbol, expiry, [strike])
    return got.get((int(strike), option_type), {})


async def fetch_premium_minutes_batch(
    symbol: str,
    expiry: date,
    strikes: list[int],
    to_ts: datetime,
) -> dict[tuple[int, str], list[PremiumMinute]]:
    """Premium minutes for many strikes (both option types) of one expiry in
    ONE query. Per-(strike, option_type) output is row-identical to
    ``fetch_premium_minutes`` — same filters, grouped per contract. A strike
    with no data still yields empty lists for both its contracts, matching
    what the single-contract fetch would return."""
    if not strikes:
        return {}
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(
                _PREMIUM_MINUTES_BATCH_SQL,
                {
                    "symbol": symbol,
                    "expiry": expiry,
                    "strikes": strikes,
                    "to_ts": to_ts,
                    "today_start": _today_start_utc(datetime.now(timezone.utc)),
                },
            )
        ).all()
    out: dict[tuple[int, str], list[PremiumMinute]] = {}
    for st in strikes:
        out[(int(st), "CE")] = []
        out[(int(st), "PE")] = []
    utc = timezone.utc
    for strike, ot, b, o, h, l, c in rows:
        if b.tzinfo is None:
            b = b.replace(tzinfo=utc)
        out[(int(strike), ot)].append(
            PremiumMinute(ts=b, o=float(o), h=float(h), l=float(l), c=float(c))
        )
    return out


async def build_premium_history(
    symbol: str,
    expiry: date,
    strike: int,
    option_type: str,
    *,
    session_date: Optional[date] = None,
    now: Optional[datetime] = None,
) -> Optional[PremiumHistory]:
    """Everything the UMP engine needs for one contract: the session's CLOSED
    1-minute candles plus 1H / previous-daily / previous-weekly context folded
    from all stored history before the session (IST calendar)."""
    now_utc = now or datetime.now(timezone.utc)
    all_minutes = await fetch_premium_minutes(
        symbol, expiry, strike, option_type, now_utc
    )
    official = await fetch_official_closes(symbol, expiry, strike, option_type)
    return premium_history_from_minutes(
        all_minutes, strike, option_type, session_date=session_date, now_utc=now_utc,
        official_close=official,
    )


def _official_entry(official: Optional[dict], d: date):
    """Normalise a per-day official record to (h, l, c); accepts the legacy
    close-only float form too."""
    if not official:
        return None
    v = official.get(d)
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return (None, None, float(v)) if v > 0 else None
    h, l, c = v[0], v[1], v[2]
    if c is None or c <= 0:
        return None
    return (h, l, float(c))


def official_day_close(
    day_minutes: list[tuple[PremiumMinute, datetime]],
    d: date,
    official_close: Optional[dict],
) -> float:
    """The close TradingView's daily bar carries for session ``d``: the
    exchange's official close when known, else the mean of the last 30
    session-minute closes (≈ the official last-half-hour average), else the
    last trade. ``day_minutes`` = (minute, IST wall time) pairs of that day.
    Mirrors ``UmpEngine._official_day_close`` — keep the two in step."""
    known = _official_entry(official_close, d)
    if known is not None:
        return known[2]
    tail_from = session_close_min(d) - 30
    tail = [m.c for m, w in day_minutes if w.hour * 60 + w.minute >= tail_from]
    if tail:
        return sum(tail) / len(tail)
    return day_minutes[-1][0].c


def official_day_hlc(
    day_minutes: list[tuple[PremiumMinute, datetime]],
    d: date,
    official_close: Optional[dict],
) -> tuple[float, float, float]:
    """Daily (H, L, C) as TradingView's D bar carries it: the exchange's
    official high/low/close when the bhavcopy row is present (the vendor's
    1-minute bars occasionally miss a printed extreme — 2026-09-01 24100CE
    low 83.40 official vs 84.00 in the minute bars), else H/L folded from
    the minutes with the close from ``official_day_close``."""
    h = max(m.h for m, _w in day_minutes)
    l = min(m.l for m, _w in day_minutes)
    known = _official_entry(official_close, d)
    if known is not None:
        oh, ol, oc = known
        return (oh if oh is not None else h, ol if ol is not None else l, oc)
    return (h, l, official_day_close(day_minutes, d, official_close))


def official_ohlc_map(
    official_close: Optional[dict[date, OfficialDay]]
) -> dict[date, tuple[float, float, float, float]]:
    """Every official day as a full (O, H, L, C) candle, keyed by IST date.

    Missing members fall back to the close, so a settlement-only day (the
    exchange publishes nothing but a settlement price when a listed contract
    does not trade) becomes the flat bar TradingView plots there.
    """
    out: dict[date, tuple[float, float, float, float]] = {}
    if not official_close:
        return out
    for d in official_close:
        rec = _official_entry(official_close, d)
        if rec is None:
            continue
        h, l, c = rec
        raw = official_close[d]
        o = raw[3] if isinstance(raw, (tuple, list)) and len(raw) > 3 else None
        out[d] = (
            float(o) if o is not None and o > 0 else c,
            float(h) if h is not None and h > 0 else c,
            float(l) if l is not None and l > 0 else c,
            c,
        )
    return out


def _day_bar(d: date, o: float, h: float, l: float, c: float) -> PremiumMinute:
    """One whole session as a single bar stamped at that date's 09:15 IST, so
    the calendar bucketing in ``aggregate_minutes`` files it under its own day
    and its own ISO week."""
    return PremiumMinute(
        ts=datetime(
            d.year, d.month, d.day, SESSION_OPEN_MIN // 60, SESSION_OPEN_MIN % 60,
            tzinfo=timezone.utc,
        ) - _IST_DELTA,
        o=o, h=h, l=l, c=c,
    )


def session_filled_minutes(
    minutes: list[PremiumMinute],
    official_close: Optional[dict[date, OfficialDay]] = None,
) -> tuple[list[PremiumMinute], int]:
    """Every minute of every stored session, holding the last price across the
    minutes the contract did not trade. Returns (series, synthesized_count).

    DISPLAY ONLY. The engine is deliberately NOT fed this: TradingView draws no
    bar for a minute with no trade, Pine therefore never evaluates one, and our
    Pine-parity numbers depend on matching that exactly. The engine already
    survives a missing minute — a 5m window whose final minute never arrives
    still fires its confirmed close from the stored running values — so there
    is nothing to repair there, only something to break.

    On the chart the holes are a real nuisance: a far-OTM strike can trade for
    one minute in a whole session (NIFTY 23450 CE traded exactly once on
    2026-09-03) and the chart then shows a single candle for the day. A held
    price is the honest reading of an untraded minute — nothing happened, the
    contract is still worth its last trade — and it is the same value the
    exchange's own daily bar carries for such a day.

    Never draws the future: the last real minute in the series is the frontier,
    and the session holding it is filled only up to that minute. Earlier
    sessions fill to their own close. Minutes BEFORE a day's first trade are
    back-filled from the previous session's official close when it is known,
    else from that day's first traded price.
    """
    if not minutes:
        return [], 0
    days = official_ohlc_map(official_close)
    by_day: dict[date, list[PremiumMinute]] = {}
    for m in minutes:
        by_day.setdefault(m.ts.astimezone(IST).date(), []).append(m)
    frontier_day = max(by_day)
    frontier_min = max(
        m.ts.astimezone(IST).hour * 60 + m.ts.astimezone(IST).minute
        for m in by_day[frontier_day]
    )

    out: list[PremiumMinute] = []
    filled = 0
    prior_close: Optional[float] = None
    for d in sorted(by_day):
        real = {
            m.ts.astimezone(IST).hour * 60 + m.ts.astimezone(IST).minute: m
            for m in by_day[d]
        }
        last_min = frontier_min if d == frontier_day else session_last_bar_min(d)
        first_real = min(real)
        # A price to stand in before the day's first trade.
        held = prior_close if prior_close is not None else real[first_real].o
        for mins in range(SESSION_OPEN_MIN, last_min + 1):
            bar = real.get(mins)
            if bar is not None:
                out.append(bar)
                held = bar.c
                continue
            stamp = datetime(
                d.year, d.month, d.day, mins // 60, mins % 60, tzinfo=timezone.utc
            ) - _IST_DELTA
            out.append(PremiumMinute(ts=stamp, o=held, h=held, l=held, c=held))
            filled += 1
        rec = days.get(d)
        prior_close = rec[3] if rec else held
    return out, filled


def daily_series_minutes(
    official_close: Optional[dict[date, OfficialDay]],
    minutes: list[PremiumMinute],
) -> list[PremiumMinute]:
    """The contract's whole life as one bar per session — the input the DAY and
    WEEK charts fold, and the same view of the world the engine's daily feed
    holds.

    Two things are being fixed here at once.

    A contract is listed days or weeks before it first trades. On those days
    the exchange publishes only a settlement price and no intraday bars exist
    anywhere, which is why TradingView's intraday charts begin at the first
    trade while its DAILY and WEEKLY charts reach back to listing. Folding from
    ``minutes`` alone loses that entire stretch.

    And on a day that DID trade, TradingView's daily bar closes at the
    exchange's official close, not at the last 1-minute print — the two differ
    by a few rupees most days (4 Sep 2026 on 23450 CE: 566.25 official against
    561.10 folded). The engine already prefers the official value, so a chart
    folded from minutes disagreed with the levels drawn on it.

    The RUNNING session is the exception: it always folds from the minutes we
    actually have. Using an official row for it would draw a full day when the
    as-of cursor is mid-session — look-ahead, visible right on the chart.
    """
    days = official_ohlc_map(official_close)
    by_min: dict[date, list[PremiumMinute]] = {}
    for m in minutes:
        by_min.setdefault(m.ts.astimezone(IST).date(), []).append(m)
    running = max(by_min) if by_min else None

    out: list[PremiumMinute] = []
    for d in sorted(set(days) | set(by_min)):
        if running is not None and d >= running:
            continue
        if d in days:
            o, h, l, c = days[d]
            out.append(_day_bar(d, o, h, l, c))
        else:
            rows = by_min[d]
            out.append(_day_bar(
                d, rows[0].o, max(r.h for r in rows),
                min(r.l for r in rows), rows[-1].c,
            ))
    if running is not None:
        out.extend(by_min[running])
    return out


def settlement_only_dates(
    official_close: Optional[dict[date, OfficialDay]],
    minutes: list[PremiumMinute],
) -> list[date]:
    """Dates the DAY/WEEK chart can only draw from a settlement price, because
    the contract was listed but never traded that day."""
    days = official_ohlc_map(official_close)
    if not days:
        return []
    traded = {m.ts.astimezone(IST).date() for m in minutes}
    return sorted(d for d in days if d not in traded)


def htf_seed_from_official(
    official_close: Optional[dict[date, OfficialDay]],
    before: date,
) -> HtfSeed:
    """Pine's D and W feeds as they stand at the OPEN of ``before``.

    ``before`` is the IST date of the first bar we are about to replay, and
    every date used here is STRICTLY earlier than it — the seed can therefore
    never see a bar the replay has not reached, which is what keeps the
    backtest's resume determinism sentinel green.

    Returns the three newest prior sessions (newest first, Pine d1/d2/d3), the
    last COMPLETED ISO week, and the part of the RUNNING ISO week that precedes
    ``before`` so the week publishes its true extremes when it closes.
    """
    days = official_ohlc_map(official_close)
    prior = sorted(d for d in days if d < before)
    if not prior:
        return HtfSeed()

    daily = [
        (days[d][1], days[d][2], days[d][3])       # (H, L, C)
        for d in reversed(prior[-3:])
    ]

    running_iso = before.isocalendar()[:2]
    by_week: dict[tuple[int, int], list[date]] = {}
    for d in prior:
        by_week.setdefault(d.isocalendar()[:2], []).append(d)

    weekly: Optional[tuple[float, float, float]] = None
    done = sorted(k for k in by_week if k != running_iso)
    if done:
        last = by_week[done[-1]]
        weekly = (
            max(days[d][1] for d in last),
            min(days[d][2] for d in last),
            days[last[-1]][3],
        )

    run = by_week.get(running_iso) or []
    if not run:
        return HtfSeed(daily=daily, weekly=weekly)
    return HtfSeed(
        daily=daily,
        weekly=weekly,
        week_iso=running_iso,
        week_days=len(run),
        week_h=max(days[d][1] for d in run),
        week_l=min(days[d][2] for d in run),
        week_c=days[run[-1]][3],
    )


def premium_history_from_minutes(
    all_minutes: list[PremiumMinute],
    strike: int,
    option_type: str,
    *,
    session_date: Optional[date],
    now_utc: datetime,
    official_close: Optional[dict[date, OfficialDay]] = None,
) -> Optional[PremiumHistory]:
    """Pure transform: cached per-contract minutes → PremiumHistory as of
    ``now_utc`` (shared seam between the SQL wrapper above and the backtest's
    per-contract cache — the folding math exists exactly once).

    Rows may extend BEYOND ``now_utc`` (the cache fetches through end-of-day);
    the closed-candle filter below produces identical output either way: a
    fully-closed bucket is unaffected, and the forming bucket is dropped.

    IST wall time is derived with a fixed +05:30 delta instead of pytz
    ``astimezone`` — India has no DST, so the wall date/hour are identical,
    and this fold runs on thousands of minutes per contract.
    """
    minutes: list[PremiumMinute] = []
    walls: list[datetime] = []
    for m in all_minutes:
        # Closed-candle discipline: drop the still-forming minute (and, for
        # cached feeds, everything after ``now_utc``).
        if m.ts + timedelta(minutes=1) > now_utc:
            continue
        minutes.append(m)
        walls.append(m.ts + _IST_DELTA)
    if not minutes:
        return None

    if session_date is None:
        session_date = walls[-1].date()

    session = [m for m, w in zip(minutes, walls) if w.date() == session_date]
    prior = [(m, w) for m, w in zip(minutes, walls) if w.date() < session_date]
    if not session:
        return None

    def _fold(group: list[PremiumMinute]) -> tuple[float, float, float, float]:
        return (group[0].o, max(g.h for g in group), min(g.l for g in group), group[-1].c)

    # Prior 1H candles — SESSION-ANCHORED buckets (09:15–10:15, 10:15–11:15…),
    # exactly TV's NSE hourly bars. Clock-hour buckets (the old key) produced
    # different 1H OHLC and therefore a different structural-reversal set
    # (found in the 2026-08-18 Pine parity audit). Out-of-session minutes
    # (historical 24/7 pollution) are excluded — TV's hourly feed never has
    # them.
    _open_min = SESSION_OPEN_MIN
    # Session-window filter for EVERY higher-timeframe fold (1H, daily,
    # weekly). Historical 24/7 pollution rows (the pre-2026-08-18 hold-last
    # bug wrote overnight/weekend rows) would otherwise corrupt daily H/L/C —
    # TV's D/W feeds only ever see trading-session bars. The last bar is
    # date-dependent (15:29 before 2026-08-03, 15:39 since).
    prior_session = [
        (m, w)
        for m, w in prior
        if _open_min <= w.hour * 60 + w.minute <= session_last_bar_min(w.date())
    ]
    h1: list[tuple[float, float, float, float]] = []
    bucket: list[PremiumMinute] = []
    bucket_key: Optional[tuple] = None
    for m, w in prior_session:
        mins = w.hour * 60 + w.minute
        key = (w.date(), (mins - _open_min) // 60)
        if key != bucket_key:
            if bucket:
                h1.append(_fold(bucket))
            bucket = []
            bucket_key = key
        bucket.append(m)
    if bucket:
        h1.append(_fold(bucket))

    # Previous daily candles (H,L,C), newest first, up to 3. H/L from the
    # 1-min bars; C = the exchange's official close (what TV's D bar shows).
    by_day: dict[date, list[tuple[PremiumMinute, datetime]]] = {}
    for m, w in prior_session:
        by_day.setdefault(w.date(), []).append((m, w))
    day_hlc: dict[date, tuple[float, float, float]] = {
        d: official_day_hlc(by_day[d], d, official_close) for d in by_day
    }
    daily: list[tuple[float, float, float]] = [
        day_hlc[d] for d in sorted(by_day.keys(), reverse=True)[:3]
    ]

    # Previous ISO week's (H,L,C) — strictly before the session's week: the
    # max/min of its official daily H/L, closing at its last session's
    # official close.
    session_week = session_date.isocalendar()[:2]
    by_week: dict[tuple, list[date]] = {}
    for d in sorted(by_day.keys()):
        wk = d.isocalendar()[:2]
        if wk != session_week:
            by_week.setdefault(wk, []).append(d)
    weekly: Optional[tuple[float, float, float]] = None
    if by_week:
        last_week = sorted(by_week.keys())[-1]
        days = by_week[last_week]
        weekly = (
            max(day_hlc[d][0] for d in days),
            min(day_hlc[d][1] for d in days),
            day_hlc[days[-1]][2],
        )

    return PremiumHistory(
        session_minutes=session,
        prior_h1=h1,
        daily=daily,
        weekly=weekly,
        strike=strike,
        option_type=option_type,
    )


@dataclass
class PremiumLife:
    """One contract's ENTIRE stored life as closed 1-minute session bars —
    the feed for the TV-parity continuous replay (the Pine chart runs on the
    contract's whole listed history, not one session). Feeds start EMPTY and
    the engine grows daily/weekly/1H itself via day rollover."""
    minutes: list[PremiumMinute]     # chronological, session-filtered, all days
    session_dates: list[date]        # IST dates actually present, ordered
    strike: int
    option_type: str
    # Exchange official daily (high, low, close) per IST date for the
    # engine's day rollover; empty when the bhavcopy has not been pulled.
    official_close: dict[date, OfficialDay] = field(default_factory=dict)


def premium_life_from_minutes(
    all_minutes: list[PremiumMinute],
    strike: int,
    option_type: str,
    *,
    now_utc: datetime,
    cut_utc: Optional[datetime] = None,
    official_close: Optional[dict[date, OfficialDay]] = None,
) -> Optional[PremiumLife]:
    """Pure transform: raw contract minutes → the continuous replay feed.

    Filters, in order: closed-candle discipline (drop the forming minute and
    anything past ``now_utc``), the as-of cursor (bucket START at/before
    ``cut_utc`` — same semantics as the single-day ``at`` trim), and the
    trading-session window 09:15:00–15:29:59 IST. The session cut at 15:30 is
    deliberate TV parity: NSE options trade until 15:30, so TV's last 1-min
    bar starts 15:29 and its last 5m bar is 15:25–15:30. A 15:30+ DB bucket
    has no TV-bar equivalent — feeding it would open a phantom 5m window,
    mis-fire the gap-fill confirmed close and distort the equal-close chain
    into the next morning. It also excises the historical 24/7 pollution rows.
    """
    _open_min = SESSION_OPEN_MIN
    minutes: list[PremiumMinute] = []
    dates: list[date] = []
    for m in all_minutes:
        if m.ts + timedelta(minutes=1) > now_utc:
            continue
        if cut_utc is not None and m.ts > cut_utc:
            continue
        w = m.ts + _IST_DELTA
        mins = w.hour * 60 + w.minute
        # Last bar = TV's final 1-min bar of THAT date (15:29 / 15:39).
        if not (_open_min <= mins <= session_last_bar_min(w.date())):
            continue
        if w.weekday() > 4:                # weekend pollution can't be a bar
            continue
        minutes.append(m)
        if not dates or dates[-1] != w.date():
            dates.append(w.date())
    if not minutes:
        return None
    return PremiumLife(
        minutes=minutes,
        session_dates=dates,
        strike=strike,
        option_type=option_type,
        official_close=dict(official_close or {}),
    )


async def build_premium_life(
    symbol: str,
    expiry: date,
    strike: int,
    option_type: str,
    *,
    cut_utc: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> Optional[PremiumLife]:
    """SQL wrapper: fetch the contract's full stored minutes (the existing
    120-day-floored query already spans every stored day) and fold them into
    the continuous replay feed."""
    now_utc = now or datetime.now(timezone.utc)
    # The fetch's 120-day planner floor anchors on ITS to_ts. Anchoring it on
    # "now" hid every contract older than ~4 months from an as-of replay
    # (QA 2026-09-02 H2: 404 "no stored premium data" with 2,153 archive rows
    # present). Anchor on the cursor instead; the closed-candle filter below
    # still uses now_utc, so today's archive/live split is unchanged.
    fetch_to = min(now_utc, cut_utc) if cut_utc is not None else now_utc
    all_minutes = await fetch_premium_minutes(
        symbol, expiry, strike, option_type, fetch_to
    )
    official = await fetch_official_closes(symbol, expiry, strike, option_type)
    return premium_life_from_minutes(
        all_minutes, strike, option_type, now_utc=now_utc, cut_utc=cut_utc,
        official_close=official,
    )


_LATEST_LTP_PER_STRIKE_SQL = text(
    """
    SELECT strike, last(ltp, ts) AS ltp
    FROM option_oi_snapshots
    WHERE symbol = :symbol
      AND expiry = :expiry
      AND option_type = :option_type
      AND ltp IS NOT NULL
      AND ts >= NOW() - INTERVAL '15 minutes'
    GROUP BY strike
    """
)

_FRESHEST_OPTION_ROW_SQL = text(
    """
    SELECT EXTRACT(EPOCH FROM (NOW() - MAX(ts))) AS age_s
    FROM option_oi_snapshots
    WHERE symbol = :symbol
      AND ts > NOW() - INTERVAL '30 minutes'
    """
)


async def freshest_option_row_age_s(symbol: str) -> Optional[float]:
    """Age in seconds of the freshest stored option row for THIS symbol —
    the trading staleness gate's ground truth. Symbol-scoped on purpose: the
    process-global flush timestamp could read "fresh" off another symbol's
    ticks while the chain the engine actually trades sat stale (and its old
    wiring crashed with an ImportError — 2026-08-18). None = nothing in the
    last 30 minutes = blocked. Indexed, ~3 ms, once per minute."""
    async with AsyncSessionLocal() as s:
        row = (
            await s.execute(_FRESHEST_OPTION_ROW_SQL, {"symbol": symbol})
        ).mappings().first()
    if row is None or row["age_s"] is None:
        return None
    return max(0.0, float(row["age_s"]))


_LATEST_CONTRACT_LTP_SQL = text(
    """
    SELECT last(ltp, ts) AS ltp
    FROM option_oi_snapshots
    WHERE symbol = :symbol
      AND expiry = :expiry
      AND strike = :strike
      AND option_type = :option_type
      AND ltp IS NOT NULL
      AND ts >= NOW() - INTERVAL '2 minutes'
    """
)


async def latest_contract_ltp(
    symbol: str, expiry: date, strike: int, option_type: str
) -> Optional[float]:
    """The contract's freshest LTP (within 2 minutes) — used by the live
    paper simulator's latency re-pricing. None = nothing fresh."""
    async with AsyncSessionLocal() as s:
        row = (
            await s.execute(
                _LATEST_CONTRACT_LTP_SQL,
                {
                    "symbol": symbol,
                    "expiry": expiry,
                    "strike": strike,
                    "option_type": option_type,
                },
            )
        ).mappings().first()
    if row is None or row["ltp"] is None:
        return None
    return float(row["ltp"])


_PREMIUM_ONE_MINUTE_SQL = text(
    """
    SELECT first(ltp, ts) AS o, MAX(ltp) AS h, MIN(ltp) AS l, last(ltp, ts) AS c
    FROM option_oi_snapshots
    WHERE symbol = :symbol
      AND expiry = :expiry
      AND strike = :strike
      AND option_type = :option_type
      AND ltp IS NOT NULL
      AND ts >= :bucket_start
      AND ts < :bucket_start + INTERVAL '1 minute'
    """
)


_FORMING_BAR_SQL = text(
    """
    SELECT
      first(ltp, ts) FILTER (WHERE ts >= :m1_start)  AS m1_o,
      MAX(ltp)       FILTER (WHERE ts >= :m1_start)  AS m1_h,
      MIN(ltp)       FILTER (WHERE ts >= :m1_start)  AS m1_l,
      last(ltp, ts)  FILTER (WHERE ts >= :m1_start)  AS m1_c,
      first(ltp, ts)                                  AS b5_o,
      MAX(ltp)                                        AS b5_h,
      MIN(ltp)                                        AS b5_l,
      last(ltp, ts)                                   AS b5_c,
      MAX(ts)                                         AS as_of
    FROM option_oi_snapshots
    WHERE symbol = :symbol
      AND expiry = :expiry
      AND strike = :strike
      AND option_type = :option_type
      AND ltp IS NOT NULL
      AND ts >= :b5_start
    """
)


_LAST_CONTRACT_TS_SQL = text(
    """
    SELECT MAX(ts) AS t
    FROM option_oi_snapshots
    WHERE symbol = :symbol
      AND expiry = :expiry
      AND strike = :strike
      AND option_type = :option_type
      AND ltp IS NOT NULL
    """
)


async def last_contract_ts(
    symbol: str, expiry: date, strike: int, option_type: str
) -> Optional[datetime]:
    """The last moment THIS contract printed a price.

    Distinct from ``freshest_row_ts``, which answers the same question for the
    whole chain — a deep-OTM strike routinely stops printing well before the
    chain's last row, so anchoring its candle to the chain's max produces an
    empty window. Used by the frozen (replay / post-close) display path.
    """
    async with AsyncSessionLocal() as s:
        row = (
            await s.execute(
                _LAST_CONTRACT_TS_SQL,
                {"symbol": symbol, "expiry": expiry,
                 "strike": strike, "option_type": option_type},
            )
        ).mappings().first()
    return row["t"] if row and row["t"] else None


_LATEST_CONTRACT_QUOTE_SQL = text(
    """
    SELECT ltp, ts
    FROM option_oi_snapshots
    WHERE symbol = :symbol
      AND expiry = :expiry
      AND strike = :strike
      AND option_type = :option_type
      AND ltp IS NOT NULL
    ORDER BY ts DESC
    LIMIT 1
    """
)


async def latest_contract_quote(
    symbol: str, expiry: date, strike: int, option_type: str
) -> Optional[tuple[float, datetime]]:
    """(last traded price, its timestamp) for ONE contract — the newest stored
    tick, not a closed-minute close.

    Used wherever a human reads "the price now" for a specific position: the
    live strip's unrealized P&L and the manual square-off's reference price.
    The per-minute engine keeps reading closed buckets (``fetch_premium_minute``)
    — this is display/reference only. Index-backed (expiry, strike,
    option_type, ts DESC): one row, single-digit milliseconds.
    """
    async with AsyncSessionLocal() as s:
        row = (
            await s.execute(
                _LATEST_CONTRACT_QUOTE_SQL,
                {"symbol": symbol, "expiry": expiry,
                 "strike": strike, "option_type": option_type},
            )
        ).mappings().first()
    if row is None or row["ltp"] is None:
        return None
    ts = row["ts"]
    if getattr(ts, "tzinfo", None) is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return float(row["ltp"]), ts


async def forming_premium_bar(
    symbol: str,
    expiry: date,
    strike: int,
    option_type: str,
    *,
    m1_start: datetime,
    b5_start: datetime,
) -> Optional[dict]:
    """The IN-PROGRESS 1-minute and 5-minute premium bars, in ONE round trip.

    Display-only — this is the one reader in the platform that deliberately
    does NOT wait for a bucket to close, so the Algo Config chart can show a
    candle forming instead of jumping when the minute rolls. Every trading
    consumer keeps using ``fetch_premium_minute`` (closed buckets only).

    Reads ``option_oi_snapshots`` (never the union view) on the existing
    ``(expiry, strike, option_type, ts DESC)`` index, bounded to the current
    5-minute window — a few rows, single-digit milliseconds. The per-second
    LTP the DB already carries under ``PERSIST_BUCKET=1s`` finally reaches the
    UI here, instead of being discarded by the closed-candle filters.

    None = the contract has not printed inside the window.
    """
    async with AsyncSessionLocal() as s:
        row = (
            await s.execute(
                _FORMING_BAR_SQL,
                {
                    "symbol": symbol,
                    "expiry": expiry,
                    "strike": strike,
                    "option_type": option_type,
                    "m1_start": m1_start,
                    "b5_start": b5_start,
                },
            )
        ).mappings().first()
    if row is None or row["b5_c"] is None:
        return None

    def _bar(prefix: str) -> Optional[dict]:
        o, h, l, c = (row[f"{prefix}_o"], row[f"{prefix}_h"],
                      row[f"{prefix}_l"], row[f"{prefix}_c"])
        if c is None:
            return None
        return {"o": float(o), "h": float(h), "l": float(l), "c": float(c)}

    return {
        "m1": _bar("m1"),
        "b5": _bar("b5"),
        "m1_start": m1_start,
        "b5_start": b5_start,
        "as_of": row["as_of"],
    }


async def select_strikes_in_band(
    symbol: str,
    expiry: date,
    option_type: str,
    band_min: float,
    band_max: float,
    count: int = 1,
) -> list[tuple[int, float]]:
    """Multi-strike band rule (supersedes the single-strike locked rule,
    user decision 2026-08-18): among the confirmed side's strikes whose LIVE
    premium sits inside the zone's Premium Min–Max band, return the ``count``
    strikes nearest the band's middle, ordered nearest-first.

    Tie-break: equal distance → lower strike (deterministic; previously live
    ties fell to SQL row order — this tightening also matches the backtest
    twin's documented rule). Empty list → no strike qualifies → no trade.
    ``count=1`` reproduces the original single-strike pick exactly."""
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(
                _LATEST_LTP_PER_STRIKE_SQL,
                {"symbol": symbol, "expiry": expiry, "option_type": option_type},
            )
        ).mappings().all()
    if not rows:
        # Zero FRESH rows for the whole side = the live feed is not currently
        # carrying this symbol at all (e.g. a SENSEX trading day while the
        # feed follows NIFTY) — distinct from "rows exist, none in band".
        log.warning("algo.series.no_fresh_options", symbol=symbol, expiry=str(expiry))
    mid = (band_min + band_max) / 2
    candidates: list[tuple[int, float]] = []
    for r in rows:
        ltp = float(r["ltp"]) if r["ltp"] is not None else None
        if ltp is None or not (band_min <= ltp <= band_max):
            continue
        candidates.append((int(r["strike"]), ltp))
    candidates.sort(key=lambda t: (abs(t[1] - mid), t[0]))
    return candidates[: max(1, count)]


_BAND_LTP_AT_SQL = text(
    """
    SELECT strike, last(ltp, ts) AS ltp
    FROM oi_snapshots_unified
    WHERE symbol = :symbol
      AND expiry = :expiry
      AND option_type = :option_type
      AND ltp IS NOT NULL
      AND ts > CAST(:ref AS TIMESTAMPTZ) - INTERVAL '15 minutes'
      AND ts <= CAST(:ref AS TIMESTAMPTZ)
    GROUP BY strike
    """
)


async def band_candidates_at(
    symbol: str,
    expiry: date,
    option_type: str,
    band_min: float,
    band_max: float,
    count: int,
    ref_utc: datetime,
) -> list[tuple[int, float]]:
    """Dashboard twin of ``select_strikes_in_band`` that works at ANY point in
    time (live or an archived session) via the unified view: last LTP per
    strike over the 15 minutes ending at ``ref_utc``, band filter, top-N
    nearest the band middle. Lets the Ultra Master Pro page show the strikes
    the orchestrator would actually hunt instead of a bare ATM guess."""
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(
                _BAND_LTP_AT_SQL,
                {
                    "symbol": symbol,
                    "expiry": expiry,
                    "option_type": option_type,
                    "ref": ref_utc,
                },
            )
        ).mappings().all()
    mid = (band_min + band_max) / 2
    candidates: list[tuple[int, float]] = []
    for r in rows:
        ltp = float(r["ltp"]) if r["ltp"] is not None else None
        if ltp is None or not (band_min <= ltp <= band_max):
            continue
        candidates.append((int(r["strike"]), ltp))
    candidates.sort(key=lambda t: (abs(t[1] - mid), t[0]))
    return candidates[: max(1, count)]


async def freshest_row_ts(symbol: str, expiry: date) -> Optional[datetime]:
    """The freshest stored row's timestamp for a symbol+expiry (live or
    archive, lifetime-bounded). Off-hours the 15-minute live window is empty,
    so the strike ladder/candidates re-anchor to THIS moment — the chain
    frozen at its last trade — instead of showing nothing."""
    from ..services.oi_timeseries import _MAX_TS_SQL, _expiry_lifetime_window_utc

    win_from, win_to = _expiry_lifetime_window_utc(expiry)
    async with AsyncSessionLocal() as s:
        row = (
            await s.execute(
                _MAX_TS_SQL,
                {"symbol": symbol, "expiry": expiry,
                 "win_from": win_from, "win_to": win_to},
            )
        ).mappings().first()
    if row is None or row["max_ts"] is None:
        return None
    ts = row["max_ts"]
    if getattr(ts, "tzinfo", None) is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


async def strike_ladder_at(
    symbol: str,
    expiry: date,
    option_type: str,
    ref_utc: datetime,
) -> list[tuple[int, float]]:
    """EVERY stored strike's last premium (15 minutes ending at ``ref_utc``),
    sorted by strike — the Ultra Master Pro page's full manual-selection
    ladder (the band candidates are the hunted subset of exactly this)."""
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(
                _BAND_LTP_AT_SQL,
                {
                    "symbol": symbol,
                    "expiry": expiry,
                    "option_type": option_type,
                    "ref": ref_utc,
                },
            )
        ).mappings().all()
    out = [
        (int(r["strike"]), float(r["ltp"]))
        for r in rows
        if r["ltp"] is not None
    ]
    out.sort(key=lambda t: t[0])
    return out


async def fetch_premium_minute(
    symbol: str,
    expiry: date,
    strike: int,
    option_type: str,
    bucket_start_utc: datetime,
) -> Optional[PremiumMinute]:
    """One closed 1-minute premium candle for a specific contract — the
    orchestrator's per-minute engine feed (tiny, indexed, hot-path safe)."""
    async with AsyncSessionLocal() as s:
        row = (
            await s.execute(
                _PREMIUM_ONE_MINUTE_SQL,
                {
                    "symbol": symbol,
                    "expiry": expiry,
                    "strike": strike,
                    "option_type": option_type,
                    "bucket_start": bucket_start_utc,
                },
            )
        ).mappings().first()
    if row is None or row["o"] is None:
        return None
    return PremiumMinute(
        ts=bucket_start_utc,
        o=float(row["o"]),
        h=float(row["h"]),
        l=float(row["l"]),
        c=float(row["c"]),
    )


async def build_ratio_pair(
    symbol: str,
    expiry: date,
    atm_window: int,
    *,
    from_ts: Optional[datetime] = None,
    to_ts: Optional[datetime] = None,
    now: Optional[datetime] = None,
    include_forming: bool = False,
) -> Optional[RatioPair]:
    """The MQAE input pair — Green = PCR (PE/CE totals), Yellow = Ratio
    (CE/PE totals) per closed 1-minute bucket over the basket, exactly the two
    series the Ratio tab renders in those colours. Hold-last fills the rare
    zero-side buckets; leading gaps are dropped on both lines together."""
    basket = await _resolve_strike_basket(symbol, expiry, atm_window, to_ts)
    if basket is None:
        return None
    strike_min, strike_max, spot = basket

    ts_resp = await fetch_oi_timeseries(
        symbol, expiry, strike_min, strike_max, bucket="1m",
        from_ts=from_ts, to_ts=to_ts,
    )
    if not ts_resp.points:
        return None

    return ratio_pair_from_points(
        ts_resp.points,
        now_utc=now or datetime.now(timezone.utc),
        strike_min=strike_min,
        strike_max=strike_max,
        spot=spot,
        include_forming=include_forming,
    )


MQAE_TIMEFRAME_MIN: dict[str, int] = {"1m": 1, "5m": 5, "10m": 10, "15m": 15, "30m": 30}
_SESSION_OPEN_MIN = 9 * 60 + 15


def _ist_minute(iso: str) -> int:
    return int(iso[11:13]) * 60 + int(iso[14:16])


def ratio_pair_for_timeframe(
    pair: Optional[RatioPair],
    timeframe: Optional[str],
    *,
    include_forming: bool = False,
) -> Optional[RatioPair]:
    """The MQAE input pair on the Ratio panel's selected timeframe.

    The single conversion live trading, the backtest, the dashboard endpoint and
    the live strip all apply, so the four can never disagree.

    - ``"1m"`` (or unset): the pair unchanged — the original engine input.
    - ``"5m" … "30m"``: one point per session-anchored bucket (09:15 anchor),
      valued at the bucket's LAST minute — the level at bucket close, exactly
      what the chart's ``lastInBucket`` draws — labelled with the bucket START.
      Only CLOSED buckets: a bucket whose last minute has not closed is a
      forming candle and would be look-ahead for a trading decision. Once the
      session is over, the final short bucket (e.g. 15:15–15:40 on 30m) counts
      as closed. ``include_forming`` keeps the in-progress bucket — for the
      display-only intrabar strip, never a trading path.
    - ``"full_day"``: the cumulative-change lines since the pair's first point
      (09:15 for a session window) at 1-minute resolution — Green = ΣΔPut ÷
      ΣΔCall, Yellow = ΣΔCall ÷ ΣΔPut, the chart's Full Day mode. Points where
      either side's change is exactly 0 (always the first) are dropped from
      BOTH lines so they stay index-aligned for the models.

    Returns None when the conversion leaves nothing (caller treats it like any
    other insufficient series).
    """
    from dataclasses import replace as _replace

    from ..core.time_utils import session_close_min

    if pair is None or not timeframe or timeframe == "1m":
        return pair
    ts = pair.timestamps
    if not ts:
        return None
    n = min(len(ts), len(pair.green_pcr), len(pair.yellow_ratio))
    have_totals = len(pair.total_call_oi) >= n and len(pair.total_put_oi) >= n

    if timeframe == "full_day":
        if not have_totals:
            return None
        c0, p0 = pair.total_call_oi[0], pair.total_put_oi[0]
        out_ts: list[str] = []
        g: list[float] = []
        y: list[float] = []
        tc: list[int] = []
        tp: list[int] = []
        for i in range(n):
            dc = pair.total_call_oi[i] - c0
            dp = pair.total_put_oi[i] - p0
            if dc == 0 or dp == 0:
                continue
            out_ts.append(ts[i])
            g.append(dp / dc)
            y.append(dc / dp)
            tc.append(pair.total_call_oi[i])
            tp.append(pair.total_put_oi[i])
        if not g:
            return None
        return _replace(pair, timestamps=out_ts, green_pcr=g, yellow_ratio=y,
                        total_call_oi=tc, total_put_oi=tp)

    step = MQAE_TIMEFRAME_MIN.get(timeframe)
    if step is None:
        raise ValueError(f"unknown MQAE timeframe {timeframe!r}")
    last_idx: dict[int, int] = {}
    for i in range(n):
        m = _ist_minute(ts[i]) - _SESSION_OPEN_MIN
        if m < 0:
            continue
        last_idx[m // step] = i          # insertion order = chronological
    data_end = _ist_minute(ts[n - 1]) + 1
    from datetime import date as _date

    session_over = data_end >= session_close_min(_date.fromisoformat(ts[n - 1][:10]))
    out_ts, g, y, tc, tp = [], [], [], [], []
    for key, i in last_idx.items():
        start = _SESSION_OPEN_MIN + key * step
        closed = start + step <= data_end or session_over
        if not closed and not include_forming:
            continue
        out_ts.append(f"{ts[i][:11]}{start // 60:02d}:{start % 60:02d}{ts[i][16:]}")
        g.append(pair.green_pcr[i])
        y.append(pair.yellow_ratio[i])
        if have_totals:
            tc.append(pair.total_call_oi[i])
            tp.append(pair.total_put_oi[i])
    if not g:
        return None
    return _replace(pair, timestamps=out_ts, green_pcr=g, yellow_ratio=y,
                    total_call_oi=tc, total_put_oi=tp)


def ratio_pair_from_points(
    points,
    *,
    now_utc: datetime,
    strike_min: int,
    strike_max: int,
    spot: Optional[float],
    include_forming: bool = False,
) -> Optional[RatioPair]:
    """Pure transform: OITimeseriesPoint rows → the MQAE input pair (shared
    seam with the backtest day-frame, like ``oi_change_pair_from_points``).

    ``include_forming`` — same contract and same warning as
    ``oi_change_pair_from_points``: live-preview only, never a trading path.
    """
    from .engines.mqae import normalized_pair

    timestamps: list[str] = []
    green_raw: list[Optional[float]] = []
    yellow_raw: list[Optional[float]] = []
    total_call: list[int] = []
    total_put: list[int] = []
    for pt in points:
        bucket_start = datetime.fromisoformat(pt.ts)
        if not include_forming and bucket_start + timedelta(minutes=1) > now_utc:
            continue
        timestamps.append(pt.ts)
        green_raw.append(pt.pcr)
        yellow_raw.append(pt.ratio)
        total_call.append(int(pt.total_call_oi))
        total_put.append(int(pt.total_put_oi))

    green, yellow = normalized_pair(green_raw, yellow_raw)
    if not green:
        return None
    # normalized_pair may drop leading gap buckets — keep timestamps aligned
    # to the SURVIVING points (gaps only ever occur at the session head, when
    # one side has no OI yet). Hold-last never removes interior points, so the
    # same leading trim keeps the OI totals aligned too.
    trim = len(timestamps) - len(green)
    timestamps = timestamps[trim:]
    total_call = total_call[trim:]
    total_put = total_put[trim:]
    return RatioPair(
        timestamps=timestamps,
        green_pcr=green,
        yellow_ratio=yellow,
        strike_min=strike_min,
        strike_max=strike_max,
        spot=spot,
        total_call_oi=total_call,
        total_put_oi=total_put,
    )
