"""Date-aware session close + official daily close (2026-09-02 TradingView
parity audit).

Run:  cd backend && PYTHONPATH=. python -m pytest tests/test_session_close.py -q

Two defects were found by diffing the Pine debug export against the engine
on 20 NIFTY contracts:

1. The exchange moved the F&O close from 15:30 to 15:40 on 2026-08-03; the
   premium folds and the engine still cut every session at 15:29, so the two
   final 5m candles, the last hourly bar and every daily close were missing.
2. TradingView's daily bar closes at the exchange's OFFICIAL close (the NSE
   bhavcopy value = last-30-minute weighted average), not the last trade.
   Pine's d1C..d3C / wC — and every level derived from them — use it.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.algo.config_models import UmpParams
from app.algo.engines.ump.engine import UmpEngine
from app.algo.series import (
    PremiumMinute,
    official_day_close,
    premium_history_from_minutes,
    premium_life_from_minutes,
)
from app.core.time_utils import (
    SESSION_CLOSE_CHANGE_DATE,
    session_close_min,
    session_last_bar_min,
)

_IST = timedelta(hours=5, minutes=30)
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


def m(y: int, mo: int, d: int, hh: int, mm: int, px: float = 100.0) -> PremiumMinute:
    ts = datetime(y, mo, d, hh, mm, tzinfo=timezone.utc) - _IST
    return PremiumMinute(ts=ts, o=px, h=px + 0.5, l=px - 0.5, c=px)


# ------------------------------------------------------------ time_utils

def test_session_close_switches_on_2026_08_03():
    assert SESSION_CLOSE_CHANGE_DATE == date(2026, 8, 3)
    assert session_close_min(date(2026, 7, 31)) == 15 * 60 + 30
    assert session_close_min(date(2026, 8, 3)) == 15 * 60 + 40
    assert session_last_bar_min(date(2026, 7, 31)) == 15 * 60 + 29
    assert session_last_bar_min(date(2026, 9, 1)) == 15 * 60 + 39


# ------------------------------------------------------------ premium life

def test_life_keeps_1539_after_change_and_cuts_1529_before():
    life = premium_life_from_minutes(
        [m(2026, 7, 31, 15, 29), m(2026, 7, 31, 15, 30), m(2026, 7, 31, 15, 39),
         m(2026, 9, 1, 15, 29), m(2026, 9, 1, 15, 39), m(2026, 9, 1, 15, 40)],
        23900, "PE", now_utc=NOW,
    )
    assert life is not None
    walls = [(x.ts + _IST).strftime("%d %H:%M") for x in life.minutes]
    assert walls == ["31 15:29", "01 15:29", "01 15:39"], (
        "pre-change days end 15:29; post-change days keep 15:30–15:39 and drop 15:40"
    )


def test_life_carries_official_closes():
    life = premium_life_from_minutes(
        [m(2026, 9, 1, 10, 0)], 23900, "PE", now_utc=NOW,
        official_close={date(2026, 9, 1): 86.4},
    )
    assert life is not None
    assert life.official_close == {date(2026, 9, 1): 86.4}


# ------------------------------------------------------ official day close

def test_official_day_close_prefers_bhavcopy_then_tail_mean_then_last():
    day = [(m(2026, 9, 1, 15, 5, 80.0), datetime(2026, 9, 1, 15, 5)),
           (m(2026, 9, 1, 15, 10, 90.0), datetime(2026, 9, 1, 15, 10)),
           (m(2026, 9, 1, 15, 39, 70.0), datetime(2026, 9, 1, 15, 39))]
    d = date(2026, 9, 1)
    assert official_day_close(day, d, {d: 86.4}) == 86.4
    # No bhavcopy: mean of closes at/after 15:10 (close 15:40 − 30 min).
    assert official_day_close(day, d, {}) == 80.0
    assert official_day_close(day, d, None) == 80.0
    # Nothing in the tail window: last trade.
    early = [(m(2026, 9, 1, 10, 0, 55.0), datetime(2026, 9, 1, 10, 0))]
    assert official_day_close(early, d, None) == 55.0


def test_history_daily_and_weekly_close_use_official_close():
    # Fri 2026-08-28 (prior week) and Mon 2026-08-31 / Tue 2026-09-01
    # (session week), session on Wed 2026-09-02.
    bars = []
    for (y, mo, d, px) in [(2026, 8, 28, 40.0), (2026, 8, 31, 50.0), (2026, 9, 1, 77.0)]:
        bars += [m(y, mo, d, 9, 15, px), m(y, mo, d, 15, 39, px)]
    bars.append(m(2026, 9, 2, 9, 15, 120.0))
    official = {date(2026, 8, 28): 41.85, date(2026, 8, 31): 52.35, date(2026, 9, 1): 86.4}
    hist = premium_history_from_minutes(
        bars, 23900, "PE", session_date=date(2026, 9, 2), now_utc=NOW,
        official_close=official,
    )
    assert hist is not None
    assert [round(c, 2) for (_h, _l, c) in hist.daily] == [86.4, 52.35, 41.85]
    assert hist.weekly is not None and round(hist.weekly[2], 2) == 41.85
    # High/low still come from the bars themselves.
    assert hist.daily[0][0] == 77.5 and hist.daily[0][1] == 76.5


# ------------------------------------------------------------------ engine

def _feed(eng: UmpEngine, y: int, mo: int, d: int, hh: int, mm: int, px: float) -> None:
    eng.process_minute(datetime(y, mo, d, hh, mm), px, px + 0.5, px - 0.5, px)


def test_engine_rollover_uses_official_close_and_1530_1539_bars():
    eng = UmpEngine(UmpParams(), official_close={date(2026, 9, 1): 86.4})
    _feed(eng, 2026, 9, 1, 9, 15, 50.0)
    _feed(eng, 2026, 9, 1, 15, 35, 99.0)     # post-15:30 bar — must count
    _feed(eng, 2026, 9, 1, 15, 39, 77.4)
    _feed(eng, 2026, 9, 1, 15, 40, 300.0)    # post-close print — ignored
    _feed(eng, 2026, 9, 2, 9, 15, 120.0)     # rollover
    assert eng.feeds.daily[0] == (99.5, 49.5, 86.4), "H/L from bars, C = official close"
    assert eng.feeds.h1_candles, "the 15:15–15:40 short hour flushed next morning"
    assert eng.feeds.h1_candles[-1].c == 77.4


def test_engine_rollover_falls_back_to_last_30_minute_mean():
    eng = UmpEngine(UmpParams())
    _feed(eng, 2026, 9, 1, 15, 5, 80.0)
    _feed(eng, 2026, 9, 1, 15, 10, 90.0)
    _feed(eng, 2026, 9, 1, 15, 39, 70.0)
    _feed(eng, 2026, 9, 2, 9, 15, 120.0)
    assert eng.feeds.daily[0][2] == 80.0, "mean of the 15:10+ closes, not the last trade"


def test_engine_pre_change_day_still_ends_1529():
    eng = UmpEngine(UmpParams())
    _feed(eng, 2026, 7, 31, 9, 15, 50.0)
    _feed(eng, 2026, 7, 31, 15, 29, 60.0)
    _feed(eng, 2026, 7, 31, 15, 35, 500.0)   # did not exist on the exchange then
    _feed(eng, 2026, 8, 3, 9, 15, 55.0)
    assert eng.feeds.daily[0][0] == 60.5, "the 15:35 print never shaped the daily high"
