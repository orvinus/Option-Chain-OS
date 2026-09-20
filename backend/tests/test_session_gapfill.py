"""Complete sessions: the archive/live precedence that was losing whole
afternoons, and the hold-last fill for minutes a contract never traded.

Reference case: NIFTY 23450 CE exp 2026-09-15. On 2026-09-10 the vendor
archive held 09:15-13:11 for EVERY contract (an interrupted nightly top-up)
while the live tick table held the full session. Because the archive had
*some* rows for that date, the old day-level precedence discarded every live
minute of the afternoon and the chart ended at lunchtime.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.algo.series import (
    PremiumMinute,
    aggregate_minutes,
    session_filled_minutes,
)

IST = timezone(timedelta(hours=5, minutes=30))


def _m(d: date, hh: int, mm: int, o, h, l, c) -> PremiumMinute:
    return PremiumMinute(
        ts=datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST).astimezone(timezone.utc),
        o=o, h=h, l=l, c=c,
    )


def _ist(m: PremiumMinute):
    return m.ts.astimezone(IST)


# ------------------------------------------------- archive/live precedence


def test_archive_preference_is_per_minute_not_per_day():
    """The regression that lost 2.5 hours of every truncated day."""
    from app.algo.series import _PREMIUM_MINUTES_TEMPLATE as tpl

    assert "arch_days" not in tpl, (
        "a day-level exclusion discards live minutes whenever the archive holds "
        "ANY row for that date — a partially archived day then ends early"
    )
    # The live branch must exclude against the archive on the exact minute.
    assert "SELECT 1 FROM arch a" in tpl
    assert "a.bucket = lv.bucket" in tpl
    # No source selection is made at date granularity any more.
    assert "::date AS d" not in tpl
    # Today still comes from live first, archive only filling what live lacks.
    assert "arch_today" in tpl


# -------------------------------------------------------- hold-last filling


def test_untraded_minutes_are_held_at_the_last_price():
    d = date(2026, 9, 4)
    mins = [
        _m(d, 9, 15, 10.0, 11.0, 9.0, 10.5),
        _m(d, 9, 18, 10.5, 12.0, 10.0, 11.5),
        _m(date(2026, 9, 7), 9, 15, 11.5, 11.5, 11.5, 11.5),   # d is not the frontier
    ]
    got, filled = session_filled_minutes(mins)
    by_min = {(_ist(m).hour, _ist(m).minute): m for m in got}
    assert by_min[(9, 16)].c == 10.5 == by_min[(9, 16)].o, "held from 09:15's close"
    assert by_min[(9, 17)].h == by_min[(9, 17)].l == 10.5, "a held bar has no range"
    assert by_min[(9, 18)].h == 12.0, "a real bar keeps its real range"
    assert by_min[(9, 19)].c == 11.5, "held from 09:18's close"
    assert filled > 0


def test_every_completed_session_runs_open_to_close():
    from app.core.time_utils import SESSION_OPEN_MIN, session_last_bar_min

    d = date(2026, 9, 3)
    got, _ = session_filled_minutes([_m(d, 11, 8, 580.9, 580.9, 580.9, 580.9),
                                     _m(date(2026, 9, 4), 9, 15, 586.7, 586.7, 586.7, 586.7)])
    day_one = [m for m in got if _ist(m).date() == d]
    first, last = _ist(day_one[0]), _ist(day_one[-1])
    assert first.hour * 60 + first.minute == SESSION_OPEN_MIN
    assert last.hour * 60 + last.minute == session_last_bar_min(d)
    assert len(day_one) == session_last_bar_min(d) - SESSION_OPEN_MIN + 1


def test_the_running_session_is_never_filled_past_its_last_real_minute():
    """Filling to the close mid-session would draw the future on the chart."""
    d = date(2026, 9, 11)
    got, _ = session_filled_minutes([_m(d, 9, 15, 100.0, 100.0, 100.0, 100.0),
                                     _m(d, 11, 30, 90.0, 90.0, 90.0, 90.0)])
    last = _ist(got[-1])
    assert (last.hour, last.minute) == (11, 30)


def test_minutes_before_the_first_trade_use_the_prior_official_close():
    d0, d1 = date(2026, 9, 3), date(2026, 9, 4)
    official = {d0: (580.9, 580.9, 580.9, 580.9)}
    got, _ = session_filled_minutes(
        [_m(d0, 11, 8, 580.9, 580.9, 580.9, 580.9), _m(d1, 12, 16, 566.0, 566.0, 566.0, 566.0)],
        official,
    )
    opening = next(m for m in got if _ist(m).date() == d1)
    assert (_ist(opening).hour, _ist(opening).minute) == (9, 15)
    assert opening.c == pytest.approx(580.9), "yesterday's official close, not today's first trade"


def test_first_day_without_history_back_fills_from_its_own_first_trade():
    d = date(2026, 9, 3)
    got, _ = session_filled_minutes([_m(d, 11, 8, 580.9, 580.9, 580.9, 580.9)])
    assert got[0].c == pytest.approx(580.9)


def test_filling_preserves_every_real_bar_untouched():
    d = date(2026, 9, 4)
    real = [_m(d, 9, 15, 10.0, 11.0, 9.0, 10.5), _m(d, 14, 3, 20.0, 22.0, 19.0, 21.0)]
    got, _ = session_filled_minutes(real)
    for r in real:
        assert r in got


def test_empty_input_is_not_an_error():
    assert session_filled_minutes([]) == ([], 0)


def test_filled_minutes_fold_into_continuous_five_minute_candles():
    d = date(2026, 9, 3)
    got, _ = session_filled_minutes([
        _m(d, 11, 8, 580.9, 580.9, 580.9, 580.9),
        _m(date(2026, 9, 4), 9, 15, 586.7, 586.7, 586.7, 586.7),
    ])
    candles = [c for c in aggregate_minutes(got, 300) if c["ts"].startswith("2026-09-03")]
    assert len(candles) == 77, "a full session of 5m candles from one traded minute"
    assert candles[0]["ts"].startswith("2026-09-03T09:15")
    assert all(c["o"] == c["h"] == c["l"] == c["c"] == pytest.approx(580.9)
               for c in candles)


# --------------------------------------------------------- display plumbing


def test_display_marks_held_candles_and_leaves_the_engine_feed_alone():
    from app.api.algo_engines import choose_display_candles

    d = date(2026, 9, 3)
    mins = [
        _m(d, 11, 8, 580.9, 580.9, 580.9, 580.9),
        _m(date(2026, 9, 4), 9, 15, 586.7, 586.7, 586.7, 586.7),
    ]
    got = choose_display_candles("5m", [], 5, mins, None, d, None)
    assert got["candles_source"] == "minutes+held"
    assert got["candles_filled"] > 380, "3 Sep traded once; the rest is held"
    assert "no trade" in got["candles_note"]
    # The caller's own list is never mutated — the engine replays this one.
    assert len(mins) == 2


def test_display_stays_silent_when_a_session_is_already_complete():
    from app.api.algo_engines import choose_display_candles
    from app.core.time_utils import SESSION_OPEN_MIN, session_last_bar_min

    d = date(2026, 9, 9)
    mins = [
        _m(d, m // 60, m % 60, 100.0, 100.0, 100.0, 100.0)
        for m in range(SESSION_OPEN_MIN, session_last_bar_min(d) + 1)
    ]
    got = choose_display_candles("5m", [], 5, mins, None, d, None)
    assert got["candles_source"] == "minutes"
    assert got["candles_note"] is None
    assert "candles_filled" not in got


# --------------------------------------------- truncated-archive detection


async def test_truncated_archive_day_is_repulled_even_with_no_missing_days(monkeypatch):
    """The day is not 'missing' — live covers it — so nothing else would ever
    look at it, and the archive stayed truncated forever."""
    from pathlib import Path

    from app.ingest import gapfill

    calls: list[tuple] = []

    async def fake_puller(symbol, script, *extra):
        calls.append((symbol, extra))
        return 0, []

    async def no_gaps(symbol, lookback, min_minutes):
        return []

    async def usable(symbol, lookback, min_minutes):
        return 20

    async def short(symbol, lookback, min_minutes):
        return [date(2026, 9, 10), date(2026, 9, 11)]

    async def refresh():
        return {}

    import app.services.data_health as dh

    monkeypatch.setattr(gapfill, "_run_puller", fake_puller)
    monkeypatch.setattr(gapfill, "missing_days", no_gaps)
    monkeypatch.setattr(gapfill, "usable_days_count", usable)
    monkeypatch.setattr(gapfill, "archive_short_days", short)
    monkeypatch.setattr(gapfill, "_script_path", lambda: Path("x/truedata_backfill.py"))
    monkeypatch.setattr(gapfill, "_settlement_script_path", lambda: Path("x/nse.py"))
    monkeypatch.setattr(gapfill, "now_ist", lambda: datetime(2026, 9, 12, 17, 0))
    monkeypatch.setattr(gapfill, "notify", lambda key, msg: None)
    monkeypatch.setattr(dh, "refresh_data_health", refresh)
    monkeypatch.setattr(gapfill.settings, "truedata_user", "u")
    monkeypatch.setattr(gapfill.settings, "truedata_password", "p")
    monkeypatch.setattr(gapfill.settings, "gapfill_symbols", "NIFTY")

    rep = await gapfill.run_gapfill_once(reason="test")
    day_pulls = [c for c in calls if "--day" in c[1]]
    assert {c[1][c[1].index("--day") + 1] for c in day_pulls} == {"2026-09-10", "2026-09-11"}
    assert rep["symbols"]["NIFTY"]["archive_short"] == ["2026-09-10", "2026-09-11"]
    assert rep["symbols"]["NIFTY"]["archive_repull"] == {"2026-09-10": 0, "2026-09-11": 0}


async def test_archive_repair_waits_for_the_historical_window(monkeypatch):
    """A vendor pull mid-session competes with the live feed."""
    from pathlib import Path

    from app.ingest import gapfill

    calls: list[tuple] = []

    async def fake_puller(symbol, script, *extra):
        calls.append((symbol, extra))
        return 0, []

    async def no_gaps(symbol, lookback, min_minutes):
        return []

    async def usable(symbol, lookback, min_minutes):
        # Non-zero: a fresh install (0 usable days) deliberately bypasses the
        # deferral, so it would not exercise the policy at all.
        return 20

    async def short(symbol, lookback, min_minutes):
        return [date(2026, 9, 10)]

    monkeypatch.setattr(gapfill, "_run_puller", fake_puller)
    monkeypatch.setattr(gapfill, "missing_days", no_gaps)
    monkeypatch.setattr(gapfill, "usable_days_count", usable)
    monkeypatch.setattr(gapfill, "archive_short_days", short)
    monkeypatch.setattr(gapfill, "_script_path", lambda: Path("x/truedata_backfill.py"))
    monkeypatch.setattr(gapfill, "_settlement_script_path", lambda: Path("x/nse.py"))
    monkeypatch.setattr(gapfill, "now_ist", lambda: datetime(2026, 9, 11, 11, 0))
    monkeypatch.setattr(gapfill.settings, "truedata_user", "u")
    monkeypatch.setattr(gapfill.settings, "truedata_password", "p")
    monkeypatch.setattr(gapfill.settings, "gapfill_symbols", "NIFTY")

    rep = await gapfill.run_gapfill_once(reason="test")
    assert not [c for c in calls if "--day" in c[1]], "deferred, not pulled mid-session"
    assert rep["symbols"]["NIFTY"]["deferred_until"]
