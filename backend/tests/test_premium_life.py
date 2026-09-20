"""premium_life_from_minutes — the TV-parity continuous-replay feed.

Run:  cd backend && PYTHONPATH=. python tests/test_premium_life.py

Pure tests over synthetic PremiumMinute lists: the session-window filter
(09:15:00 to the date's last session bar — 15:29 before 2026-08-03, 15:39
since the exchange moved the close; the cut is TV parity AND the excision of
the historical 24/7 hold-last pollution), the as-of cursor,
closed-candle discipline, weekend exclusion and ordered session_dates.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.algo.series import PremiumMinute, premium_life_from_minutes

_IST = timedelta(hours=5, minutes=30)


def m(y: int, mo: int, d: int, hh: int, mm: int, px: float = 100.0) -> PremiumMinute:
    """A 1-minute bar whose IST wall time is the given clock."""
    ts = datetime(y, mo, d, hh, mm, tzinfo=timezone.utc) - _IST
    return PremiumMinute(ts=ts, o=px, h=px + 0.5, l=px - 0.5, c=px + 0.1)


NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)   # far after every bar


def test_session_window_is_inclusive_at_both_edges():
    life = premium_life_from_minutes(
        [m(2026, 8, 13, 9, 14), m(2026, 8, 13, 9, 15), m(2026, 8, 13, 15, 29),
         m(2026, 8, 13, 15, 39), m(2026, 8, 13, 15, 40)],
        24500, "CE", now_utc=NOW,
    )
    assert life is not None
    walls = [(x.ts + _IST).strftime("%H:%M") for x in life.minutes]
    assert walls == ["09:15", "15:29", "15:39"], (
        "09:14 pre-open and the 15:40 bucket are cut; 15:39 is the last bar since 2026-08-03"
    )


def test_session_window_pre_change_day_ends_1529():
    life = premium_life_from_minutes(
        [m(2026, 7, 30, 9, 15), m(2026, 7, 30, 15, 29), m(2026, 7, 30, 15, 30),
         m(2026, 7, 30, 15, 39)],
        24500, "CE", now_utc=NOW,
    )
    assert life is not None
    walls = [(x.ts + _IST).strftime("%H:%M") for x in life.minutes]
    assert walls == ["09:15", "15:29"], "before 2026-08-03 the exchange closed at 15:30"


def test_pollution_rows_excluded():
    life = premium_life_from_minutes(
        [m(2026, 8, 13, 10, 0),
         m(2026, 8, 13, 23, 59),           # overnight hold-last pollution
         m(2026, 8, 14, 3, 12),            # pre-dawn pollution
         m(2026, 8, 15, 11, 0),            # SATURDAY — weekend pollution
         m(2026, 8, 16, 11, 0),            # Sunday
         m(2026, 8, 17, 10, 0)],
        24500, "CE", now_utc=NOW,
    )
    assert life is not None
    assert life.session_dates == [datetime(2026, 8, 13).date(), datetime(2026, 8, 17).date()]
    assert len(life.minutes) == 2


def test_cursor_keeps_prior_days_and_partial_cursor_day():
    cut = datetime(2026, 8, 14, 10, 30, tzinfo=timezone.utc) - _IST  # 10:30 IST 08-14
    life = premium_life_from_minutes(
        [m(2026, 8, 13, 10, 0), m(2026, 8, 13, 15, 29),
         m(2026, 8, 14, 10, 29), m(2026, 8, 14, 10, 30), m(2026, 8, 14, 10, 31)],
        24500, "CE", now_utc=NOW, cut_utc=cut,
    )
    assert life is not None
    walls = [(x.ts + _IST).strftime("%d %H:%M") for x in life.minutes]
    assert walls == ["13 10:00", "13 15:29", "14 10:29", "14 10:30"], (
        "bucket START at/before the cursor survives; later buckets are cut"
    )


def test_closed_candle_discipline():
    now = (datetime(2026, 8, 13, 10, 5, tzinfo=timezone.utc) - _IST) + timedelta(seconds=30)
    life = premium_life_from_minutes(
        [m(2026, 8, 13, 10, 4), m(2026, 8, 13, 10, 5)],
        24500, "CE", now_utc=now,
    )
    assert life is not None
    assert len(life.minutes) == 1, "the still-forming 10:05 bucket is dropped"


def test_session_dates_ordered_with_sparse_days():
    life = premium_life_from_minutes(
        [m(2026, 7, 16, 10, 44), m(2026, 7, 27, 9, 16), m(2026, 8, 13, 15, 29)],
        24500, "CE", now_utc=NOW,
    )
    assert life is not None
    assert [d.isoformat() for d in life.session_dates] == [
        "2026-07-16", "2026-07-27", "2026-08-13"
    ]


def test_empty_after_filters_returns_none():
    assert premium_life_from_minutes(
        [m(2026, 8, 15, 11, 0)], 24500, "CE", now_utc=NOW
    ) is None, "a weekend-only feed folds to nothing"


def _run_all() -> None:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL  {name}: {e}")
    if failures:
        raise SystemExit(f"{failures} test(s) failed")
    print("all premium-life tests passed")


if __name__ == "__main__":
    _run_all()
