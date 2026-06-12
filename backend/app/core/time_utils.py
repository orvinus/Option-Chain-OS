"""Time helpers (IST market clock)."""
from __future__ import annotations

from datetime import datetime, time, timedelta

import pytz

IST = pytz.timezone("Asia/Kolkata")
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)


def now_ist() -> datetime:
    return datetime.now(IST)


def market_open_today(ts: datetime | None = None) -> datetime:
    ts = (ts or now_ist()).astimezone(IST)
    return IST.localize(datetime.combine(ts.date(), MARKET_OPEN))


def market_close_today(ts: datetime | None = None) -> datetime:
    ts = (ts or now_ist()).astimezone(IST)
    return IST.localize(datetime.combine(ts.date(), MARKET_CLOSE))


def is_nse_regular_session_open(ts: datetime | None = None) -> bool:
    """Weekday NSE cash/F&O regular session 09:15–15:30 IST (holidays not checked)."""
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
