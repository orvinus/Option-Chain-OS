"""Time helpers (IST market clock).

Session hours are CONFIGURABLE (``MARKET_OPEN_IST`` / ``MARKET_CLOSE_IST``) because
the exchange has changed them before and will again — they were hardcoded in six
places, including the expiry settlement instant that drives every IV and greek.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytz

from .config import settings

IST = pytz.timezone("Asia/Kolkata")


def _parse_hhmm(raw: str, fallback: time) -> time:
    """Parse "HH:MM" from config; fall back rather than crash the whole app."""
    try:
        hh, mm = (int(p) for p in raw.strip().split(":", 1))
        return time(hh, mm)
    except (ValueError, AttributeError):
        return fallback


MARKET_OPEN = _parse_hhmm(settings.market_open_ist, time(9, 15))
# 15:40 since 2026-08-04 (was 15:30). Anything deriving from the close — the session
# floor, staleness gating, and the expiry SETTLEMENT instant used for time-to-expiry —
# must read this, never a literal.
MARKET_CLOSE = _parse_hhmm(settings.market_close_ist, time(15, 40))

# The exchange moved the F&O close from 15:30 to 15:40 with effect from
# 2026-08-03 (first session with 15:3x bars in the vendor archive). Anything
# that folds HISTORICAL minutes into session bars (the UMP premium feeds:
# 1H / daily / weekly, the TV-parity replay) needs the close that applied ON
# THAT DATE — a single MARKET_CLOSE literal would either cut the last ten
# minutes off every post-change day (the 2026-09-02 TradingView parity
# audit: every daily close, the last hourly bar and the 15:30/15:35 5m
# candles were missing) or admit post-close prints on pre-change days.
SESSION_CLOSE_CHANGE_DATE = date(2026, 8, 3)
_CLOSE_MIN_OLD = 15 * 60 + 30
_CLOSE_MIN_NEW = 15 * 60 + 40
SESSION_OPEN_MIN = 9 * 60 + 15


def session_close_min(d: date) -> int:
    """Exclusive session end for IST date ``d`` in minutes since midnight
    (940 = 15:40 from 2026-08-03, 930 = 15:30 before)."""
    return _CLOSE_MIN_NEW if d >= SESSION_CLOSE_CHANGE_DATE else _CLOSE_MIN_OLD


def session_last_bar_min(d: date) -> int:
    """Minute-of-day of the LAST 1-minute bar of the session on ``d`` — the
    bucket that starts one minute before the close (15:39 / 15:29), which is
    also TradingView's final 1-min bar of that day."""
    return session_close_min(d) - 1


def now_ist() -> datetime:
    return datetime.now(IST)


def ist_naive_to_utc(naive: datetime) -> datetime:
    """Attach IST to a naive datetime and convert to UTC. USE THIS, ALWAYS.

    ``naive.replace(tzinfo=IST)`` is WRONG and silently so. ``IST`` is a pytz
    zone, and pytz's tzinfo objects carry every historical offset that zone has
    ever had; ``replace`` picks the FIRST one, which for Asia/Kolkata is Local
    Mean Time at **+05:53**. Every timestamp built that way lands **23 minutes**
    off, with no error and a perfectly plausible-looking value.

    This is not hypothetical. ``scripts/truedata_backfill.py`` used that idiom,
    so the whole six-month TrueData archive was written 23 minutes early —
    measured 2026-08-13 by joining archive to live rows at varying offsets:
    11% agreement at 0 minutes, **92% at −23**. Replay and the timeseries charts
    were showing archive-date data shifted by 23 minutes.

    pytz requires ``localize()`` for exactly this reason. Prefer this helper so
    the trap has one place to be avoided rather than five to be repeated.
    """
    if naive.tzinfo is not None:
        return naive.astimezone(timezone.utc)
    return IST.localize(naive).astimezone(timezone.utc)


def market_open_today(ts: datetime | None = None) -> datetime:
    ts = (ts or now_ist()).astimezone(IST)
    return IST.localize(datetime.combine(ts.date(), MARKET_OPEN))


def market_close_today(ts: datetime | None = None) -> datetime:
    ts = (ts or now_ist()).astimezone(IST)
    return IST.localize(datetime.combine(ts.date(), MARKET_CLOSE))


def session_floor_for(ts: datetime) -> datetime:
    """Most recent session open at or before *ts*.

    ``market_open_today(ts)`` returns 09:15 on *ts*'s own calendar date, which is in
    the FUTURE whenever *ts* falls between midnight and 09:15 IST. Used as a query
    floor (``WHERE ts >= floor``) that silently matched nothing, so every panel went
    blank for any data anchored in those hours — e.g. rows written overnight by the
    poller, or a session viewed just after midnight. Roll back a day in that case.
    """
    ts_ist = ts.astimezone(IST)
    floor = market_open_today(ts_ist)
    if floor > ts_ist:
        floor = market_open_today(ts_ist - timedelta(days=1))
    return floor


def is_nse_regular_session_open(ts: datetime | None = None) -> bool:
    """Weekday NSE cash/F&O regular session, MARKET_OPEN–MARKET_CLOSE IST
    (configurable; holidays not checked)."""
    ts_ist = (ts or now_ist()).astimezone(IST)
    if ts_ist.weekday() >= 5:
        return False
    t = ts_ist.time()
    return MARKET_OPEN <= t <= MARKET_CLOSE


def floor_minute(ts: datetime) -> datetime:
    return ts.replace(second=0, microsecond=0)


TIMEFRAME_TO_DELTA: dict[str, timedelta | str] = {
    "1s": timedelta(seconds=1),
    "15s": timedelta(seconds=15),
    "30s": timedelta(seconds=30),
    "45s": timedelta(seconds=45),
    "1m": timedelta(minutes=1),
    "3m": timedelta(minutes=3),
    "5m": timedelta(minutes=5),
    "10m": timedelta(minutes=10),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "2h": timedelta(hours=2),
    "3h": timedelta(hours=3),
    "full_day": "full_day",
}


def parse_timeframe(tf: str) -> timedelta | str:
    tf = tf.lower().strip()
    if tf not in TIMEFRAME_TO_DELTA:
        raise ValueError(
            f"Unsupported timeframe '{tf}'. "
            f"Allowed: {list(TIMEFRAME_TO_DELTA.keys())}"
        )
    return TIMEFRAME_TO_DELTA[tf]
