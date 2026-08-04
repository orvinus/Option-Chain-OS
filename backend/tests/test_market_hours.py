"""Session hours (15:40 close) and the feed watchdog's watch window.

The exchange moved the F&O close from 15:30 to 15:40 on 2026-08-04. The close was
hardcoded in several places, including the expiry SETTLEMENT instant that drives
time-to-expiry for every IV and greek — a stale copy there would report expiry-day
contracts as already settled from 15:30, and understate T before that.

Runnable without pytest:  PYTHONPATH=. python tests/test_market_hours.py
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytz

from app.core.time_utils import (
    MARKET_CLOSE,
    MARKET_OPEN,
    is_nse_regular_session_open,
    session_floor_for,
)
from app.ingest import feed_watchdog
from app.services.option_chain_full import EXPIRY_SETTLE_TIME, _years_to_expiry

IST = pytz.timezone("Asia/Kolkata")
# 2026-08-04 is a Tuesday; 2026-08-08 a Saturday.
WEEKDAY = date(2026, 8, 4)
SATURDAY = date(2026, 8, 8)


def _ist(d: date, hh: int, mm: int) -> datetime:
    return IST.localize(datetime(d.year, d.month, d.day, hh, mm))


async def test_session_runs_to_1540() -> None:
    assert MARKET_CLOSE.hour == 15 and MARKET_CLOSE.minute == 40, (
        f"close must be 15:40, got {MARKET_CLOSE}"
    )
    assert is_nse_regular_session_open(_ist(WEEKDAY, 9, 14)) is False
    assert is_nse_regular_session_open(_ist(WEEKDAY, 9, 15)) is True
    # The ten minutes that used to be "after close".
    assert is_nse_regular_session_open(_ist(WEEKDAY, 15, 31)) is True
    assert is_nse_regular_session_open(_ist(WEEKDAY, 15, 40)) is True
    assert is_nse_regular_session_open(_ist(WEEKDAY, 15, 41)) is False
    assert is_nse_regular_session_open(_ist(SATURDAY, 11, 0)) is False


async def test_expiry_settlement_follows_the_close() -> None:
    """A hardcoded 15:30 here would null out IV for the last ten minutes."""
    assert EXPIRY_SETTLE_TIME == MARKET_CLOSE, (
        "the settlement instant must follow MARKET_CLOSE, not repeat a literal"
    )
    # 15:35 on expiry day: still five minutes of time value, not settled.
    t = _years_to_expiry(WEEKDAY, _ist(WEEKDAY, 15, 35))
    assert t is not None and t > 0, "expiry-day 15:35 must still have positive T"
    # Past the close it is genuinely settled -> None (never a fabricated floor).
    assert _years_to_expiry(WEEKDAY, _ist(WEEKDAY, 15, 41)) is None
    # And T must strictly shrink through the session.
    t0915 = _years_to_expiry(WEEKDAY, _ist(WEEKDAY, 9, 15))
    assert t0915 is not None and t0915 > t


async def test_session_floor_rolls_back_before_open() -> None:
    """Between midnight and the open, the floor belongs to the PREVIOUS day."""
    pre_open = _ist(WEEKDAY, 3, 0)
    floor = session_floor_for(pre_open)
    assert floor < pre_open, "a floor in the future matches no rows and blanks every panel"
    assert floor.date() == WEEKDAY - timedelta(days=1)
    assert (floor.hour, floor.minute) == (MARKET_OPEN.hour, MARKET_OPEN.minute)


async def test_watchdog_window_covers_warmup_and_full_session() -> None:
    w = feed_watchdog._within_watch_window
    assert w(_ist(WEEKDAY, 8, 0)) is False, "not watching hours before the open"
    assert w(_ist(WEEKDAY, 9, 5)) is True, "warm-up so the feed is live AT the open"
    assert w(_ist(WEEKDAY, 12, 0)) is True
    assert w(_ist(WEEKDAY, 15, 35)) is True, "must still watch after the OLD 15:30 close"
    assert w(_ist(WEEKDAY, 16, 30)) is False
    assert w(_ist(SATURDAY, 12, 0)) is False


async def test_watchdog_treats_missing_and_stale_flush_as_unhealthy() -> None:
    from app.runtime import get_runtime

    rt = get_runtime()
    prev = rt.last_flush_at
    try:
        rt.last_flush_at = None
        assert feed_watchdog._data_age_seconds() is None, "no flush yet must not read as fresh"

        rt.last_flush_at = datetime.now(timezone.utc) - timedelta(hours=13)
        age = feed_watchdog._data_age_seconds()
        assert age is not None and age > feed_watchdog.STALE_AFTER_S, (
            "a 13h-old flush — the exact production outage — must count as stale"
        )

        rt.last_flush_at = datetime.now(timezone.utc)
        age = feed_watchdog._data_age_seconds()
        assert age is not None and age <= feed_watchdog.STALE_AFTER_S
    finally:
        rt.last_flush_at = prev


# --------------------------------------------------------------------------- runner
async def _main() -> int:
    tests = [
        v for k, v in sorted(globals().items())
        if k.startswith("test_") and asyncio.iscoroutinefunction(v)
    ]
    passed = 0
    for t in tests:
        try:
            await t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
