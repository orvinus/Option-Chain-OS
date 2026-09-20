"""Replay sub-candles: the finer series the UMP chart animates a forming bar from.

Before this, replaying at a 5-minute display revealed each candle whole, in one
jump, because the payload carried nothing finer than what was being displayed.
The engine histories are minute-stamped but carry levels and state, not prices,
so a price path could not be reconstructed from them.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.algo.series import PremiumMinute, aggregate_minutes, session_filled_minutes
from app.api.algo_engines import choose_display_candles

IST = timezone(timedelta(hours=5, minutes=30))


def _m(d: date, hh: int, mm: int, o, h, l, c) -> PremiumMinute:
    return PremiumMinute(
        ts=datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST).astimezone(timezone.utc),
        o=o, h=h, l=l, c=c,
    )


def _session(d: date, n: int) -> list[PremiumMinute]:
    out = []
    for i in range(n):
        px = 100.0 + (i % 7)
        out.append(_m(d, 9 + (15 + i) // 60, (15 + i) % 60, px, px + 1, px - 1, px + 0.5))
    return out


def _group(subs, disp_ts, disp_sec):
    """The sub-candles falling inside one display bucket — the same window the
    client uses to fold a forming bar."""
    start = datetime.fromisoformat(disp_ts)
    end = start + timedelta(seconds=disp_sec)
    return [x for x in subs if start <= datetime.fromisoformat(x["ts"]) < end]


def test_every_display_candle_is_covered_by_subcandles():
    """Each display bucket must own at least one sub-candle and never more than
    its width, so the forming bar always has something to grow from.

    NOT an exact multiple overall: the session runs 09:15 to the close, which
    does not divide evenly by 5, 15 or 60 minutes, so the LAST bucket of a day
    is legitimately short. The client counts per bucket for exactly this reason.
    """
    mins = _session(date(2026, 9, 9), 375)
    filled, _ = session_filled_minutes(mins, None)
    subs = aggregate_minutes(filled, 60)
    for disp_sec in (300, 900, 3600):
        disp = aggregate_minutes(filled, disp_sec)
        width = disp_sec // 60
        counts = [len(_group(subs, c["ts"], disp_sec)) for c in disp]
        assert all(1 <= n <= width for n in counts), f"{disp_sec}s: {counts[:5]}…"
        assert all(n == width for n in counts[:-1]), (
            f"{disp_sec}s: only the LAST bucket may be short, got {counts[:5]}…"
        )
        assert sum(counts) == len(subs), "every sub-candle belongs to exactly one bucket"


def test_a_display_candle_folds_from_its_own_subcandles():
    """Open from the first sub, high/low the extremes, close from the last."""
    mins = _session(date(2026, 9, 9), 60)
    filled, _ = session_filled_minutes(mins, None)
    disp = aggregate_minutes(filled, 300)
    subs = aggregate_minutes(filled, 60)
    for c in disp:
        group = _group(subs, c["ts"], 300)
        assert group, "no sub-candles for this display candle"
        assert c["o"] == pytest.approx(group[0]["o"])
        assert c["c"] == pytest.approx(group[-1]["c"])
        assert c["h"] == pytest.approx(max(g["h"] for g in group))
        assert c["l"] == pytest.approx(min(g["l"] for g in group))


def test_subcandles_come_from_the_same_filled_series_as_the_display():
    """A thin strike's display candles are built from the HELD series. If the
    sub-candles came from the raw minutes instead they would not tile, and the
    forming bar would visibly disagree with the closed one."""
    d = date(2026, 9, 3)
    sparse = [_m(d, 11, 8, 580.9, 580.9, 580.9, 580.9),
              _m(date(2026, 9, 4), 9, 15, 586.7, 586.7, 586.7, 586.7)]
    filled, synthesized = session_filled_minutes(sparse, None)
    assert synthesized > 300, "this fixture is meant to be mostly held"
    disp = aggregate_minutes(filled, 300)
    subs = aggregate_minutes(filled, 60)
    counts = [len(_group(subs, c["ts"], 300)) for c in disp]
    assert all(n >= 1 for n in counts), "a display candle with no sub-candles cannot animate"
    assert sum(counts) == len(subs)
    # The raw minutes alone could never cover it.
    assert len(aggregate_minutes(sparse, 60)) < len(disp)


def test_display_candles_are_unchanged_by_any_of_this():
    """The sub-series is additive; the displayed candles must not move."""
    mins = _session(date(2026, 9, 9), 120)
    got = choose_display_candles("5m", [], 5, mins, None, date(2026, 9, 9), None)
    assert got["candles_interval"] == 300
    assert got["candles"] == aggregate_minutes(
        session_filled_minutes(mins, None)[0], 300
    )


def test_the_endpoint_only_ships_subcandles_for_replay_and_coarser_intervals():
    """Guards the three conditions: replay only, coarser-than-a-minute only,
    and 1-minute resolution."""
    import inspect

    from app.api import algo_engines

    src = inspect.getsource(algo_engines.ump_eval)
    assert 'if history and int(display.get("candles_interval") or 0) > sub_sec:' in src, (
        "sub-candles must be gated on history AND on the display being coarser"
    )
    assert '"subcandles_interval"] = sub_sec' in src
    assert "sub_sec = 60" in src
