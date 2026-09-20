"""UMP 2026-09-09 additions: per-zone entry timeframe (5/15), per-kind
scenario switches, replay histories, warm-up re-arming and the display
candle helpers. DB-free.

Run:  cd backend && PYTHONPATH=. python -m pytest tests/test_ump_timeframe_scenarios.py -q
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.algo.config_models import UMP_SCENARIOS, UmpEntryModel, UmpParams, ZoneConfig
from app.algo.engines.ump.engine import UmpEngine
from app.algo.engines.ump.levels import LEVEL_TYPE_NAMES, Level

T0 = datetime(2026, 8, 14, 9, 15)


def mk(levels, *, record_history=False, **entry_over) -> UmpEngine:
    lvls = [Level(price=p, type=t, name=LEVEL_TYPE_NAMES[t]) for p, t in levels]
    params = UmpParams()
    for k, v in entry_over.items():
        setattr(params.entry, k, v)
    return UmpEngine(params, level_provider=lambda: list(lvls), record_history=record_history)


def feed(eng: UmpEngine, bars) -> None:
    for off, o, h, l, c in bars:
        eng.process_minute(T0 + timedelta(minutes=off), o, h, l, c)


def sc1_trigger(close=104.0, tf=5):
    """A green trigger candle spanning the whole first window of ``tf``
    minutes: open 99 (< Base 100), close > ZT."""
    bars = []
    for i in range(tf - 1):
        bars.append((i, 99.0 + i * 0.01, 99.2 + i * 0.01, 98.9 + i * 0.01, 99.1 + i * 0.01))
    bars.append((tf - 1, 99.4, max(close, 99.4) + 0.2, 99.3, close))
    return bars


# ── config model ────────────────────────────────────────────────────────

def test_entry_timeframe_defaults_to_5_for_old_docs():
    m = UmpEntryModel.model_validate({"enable_long": True})
    assert m.entry_timeframe_min == 5
    assert all(m.scenarios[k] for k in UMP_SCENARIOS)


def test_entry_timeframe_rejects_10():
    with pytest.raises(Exception):
        UmpEntryModel.model_validate({"entry_timeframe_min": 10})


def test_scenarios_missing_keys_fill_and_unknown_rejected():
    m = UmpEntryModel.model_validate({"scenarios": {"S1A": False}})
    assert m.scenarios["S1A"] is False and m.scenarios["R2"] is True
    assert not m.scenario_enabled("S1A") and m.scenario_enabled("S1B")
    with pytest.raises(Exception):
        UmpEntryModel.model_validate({"scenarios": {"S9Z": True}})


def test_zone_default_carries_new_fields():
    z = ZoneConfig()
    assert z.ump.entry.entry_timeframe_min == 5
    assert len(z.ump.entry.scenarios) == len(UMP_SCENARIOS)


# ── timeframe ───────────────────────────────────────────────────────────

def test_default_5m_unchanged_by_explicit_5():
    a = mk([(100.0, 1), (130.0, 1)])
    b = mk([(100.0, 1), (130.0, 1)], entry_timeframe_min=5)
    bars = sc1_trigger() + [(5, 103.0, 103.5, 99.5, 101.0), (9, 101.0, 108.0, 100.9, 107.9)]
    feed(a, bars)
    feed(b, bars)
    assert [(e.ts, e.kind, e.price) for e in a.events] == [(e.ts, e.kind, e.price) for e in b.events]
    assert a.in_trade and a.sub == "S1A"


def test_15m_windows_confirm_only_at_minute_29():
    eng = mk([(100.0, 1), (130.0, 1)], entry_timeframe_min=15)
    feed(eng, sc1_trigger(tf=15)[:5])       # 09:15..09:19 — a 5m engine would trigger here
    assert not eng.trig, "at 15m the first window has not closed yet"
    feed(eng, sc1_trigger(tf=15)[5:])       # …09:29 closes the 09:15 window
    assert eng.trig and eng._trig_scen == 1  # noqa: SLF001
    assert eng._bar_index == 0               # noqa: SLF001 — one 15m candle so far
    feed(eng, [(15, 103.0, 103.5, 99.5, 101.0)])   # next window's first bar: S1A test candle
    assert eng.in_trade and eng.sub == "S1A"


def test_15m_trigger_timeout_counts_15m_bars():
    tf15 = mk([(100.0, 1), (130.0, 1)], entry_timeframe_min=15, trigger_timeout_bars=2)
    feed(tf15, sc1_trigger(tf=15))
    assert tf15.trig
    # 2 bars × 15 min = 30 min: a bar 31 minutes after the trigger window start
    # (em5_time 09:15) expires it; nothing entered because the bar never
    # touches the zone.
    feed(tf15, [(46, 110.0, 110.5, 109.0, 110.2)])
    assert not tf15.trig
    tf5 = mk([(100.0, 1), (130.0, 1)], entry_timeframe_min=5, trigger_timeout_bars=2)
    feed(tf5, sc1_trigger())
    feed(tf5, [(11, 110.0, 110.5, 109.0, 110.2)])   # window 09:25: exactly 10 min — NOT expired
    assert tf5.trig
    feed(tf5, [(16, 110.0, 110.5, 109.0, 110.2)])   # window 09:30: 15 min > 10 min at 5m
    assert not tf5.trig


def test_15m_short_last_window_closes_on_next_session_bar():
    """15:30–15:39 never sees minute 44 → the confirmed close is deferred to
    the next window's first bar, like a missing final minute at 5m."""
    eng = mk([(100.0, 1), (130.0, 1)], entry_timeframe_min=15)
    base = datetime(2026, 8, 14, 15, 30)
    for i in range(10):
        eng.process_minute(base + timedelta(minutes=i), 99.0, 104.5, 98.9, 104.0)
    assert not eng.trig and eng._closed_for is None  # noqa: SLF001
    eng.process_minute(datetime(2026, 8, 17, 9, 15), 104.0, 104.1, 103.9, 104.0)
    # The deferred close ran (the short window is confirmed) — it armed the
    # SC1 trigger, which the next-session bar then expired through the
    # wall-clock timeout (Pine parity: the weekend consumes the timeout).
    assert eng._closed_for == base and eng._prev_5m_close == 104.0  # noqa: SLF001
    assert not eng.trig and not eng.in_trade


# ── scenario switches ──────────────────────────────────────────────────

def test_scenario_off_s1a_does_not_fall_through_to_s1b():
    eng = mk([(100.0, 1), (130.0, 1)], scenarios={"S1A": False})
    feed(eng, sc1_trigger())
    feed(eng, [(5, 103.0, 103.5, 99.5, 101.0)])   # S1A geometry (wick through Base)
    assert not eng.in_trade and eng.trig, "refused at commit; trigger stays armed"
    assert eng.events == []
    # Same candle: the running low (99.5) keeps the S1A geometry → still refused.
    feed(eng, [(6, 103.0, 103.2, 100.7, 102.0)])
    assert not eng.in_trade and eng.trig
    feed(eng, [(10, 103.0, 103.2, 100.7, 102.0)])  # next candle: S1B geometry
    assert eng.in_trade and eng.sub == "S1B"


def test_scenario_off_r1_still_allows_r2():
    eng = mk([(100.0, 1), (130.0, 1)], scenarios={"R1": False})
    feed(eng, [(5, 101.2, 101.3, 99.8, 100.6)])   # R1 bar on the 100 level
    assert not eng.in_trade
    # R2 on the same level: open > UM, low <= UM, low > B, close > UM.
    feed(eng, [(10, 102.0, 102.2, 101.2, 101.9)])
    assert eng.in_trade and eng.sub == "R2"


def test_all_scenarios_off_never_enters_but_arms():
    eng = mk([(100.0, 1), (130.0, 1)], scenarios={k: False for k in UMP_SCENARIOS})
    feed(eng, sc1_trigger())
    assert eng.trig
    feed(eng, [(5, 103.0, 103.5, 99.5, 101.0), (6, 103.0, 103.2, 100.7, 102.0)])
    assert not eng.in_trade and eng.trades == []


# ── histories ──────────────────────────────────────────────────────────

def test_history_off_by_default_records_nothing():
    eng = mk([(100.0, 1), (130.0, 1)])
    feed(eng, sc1_trigger() + [(5, 103.0, 103.5, 99.5, 101.0)])
    assert eng.levels_history == [] and eng.state_history == []


def test_history_records_levels_once_and_state_on_change():
    eng = mk([(100.0, 1), (130.0, 1)], record_history=True)
    feed(eng, sc1_trigger() + [(5, 103.0, 103.5, 99.5, 101.0), (9, 101.0, 108.0, 100.9, 107.9)])
    assert len(eng.levels_history) == 1, "static injected levels → one row"
    kinds = [(r["in_trade"], r["trig"]) for r in eng.state_history]
    assert (False, True) in kinds and (True, False) in kinds
    last = eng.state_history[-1]
    assert last["in_trade"] and last["sub"] == "S1A" and last["trail_sl"] is not None


# ── warm-up re-arming ──────────────────────────────────────────────────

def test_rearm_restores_scenarios_and_timeframe():
    from app.algo.orchestrator import disarm_entries, rearm_entries

    zone = ZoneConfig()
    zone.ump.entry.scenarios["S1C"] = False
    zone.ump.entry.entry_timeframe_min = 15
    p = zone.ump.model_copy(deep=True)
    disarm_entries(p)
    assert not p.entry.enable_long and not p.entry.enable_retest
    p.entry.scenarios["S1C"] = True   # simulate drift
    rearm_entries(p, zone.ump)
    assert p.entry.enable_long and p.entry.enable_retest
    assert p.entry.scenarios["S1C"] is False
    assert p.entry.entry_timeframe_min == 15
    assert p.entry.scenarios is not zone.ump.entry.scenarios


# ── display candle helpers ─────────────────────────────────────────────

def _minutes(day: date, start_min: int, n: int):
    from app.algo.series import PremiumMinute
    from app.core.time_utils import ist_naive_to_utc

    out = []
    for i in range(n):
        ist = datetime.combine(day, datetime.min.time()) + timedelta(minutes=start_min + i)
        out.append(PremiumMinute(ts=ist_naive_to_utc(ist), o=100 + i, h=101 + i, l=99 + i, c=100.5 + i))
    return out


def test_aggregate_minutes_buckets_and_multi_day():
    from app.algo.series import aggregate_minutes

    mins = _minutes(date(2026, 9, 1), 555, 385) + _minutes(date(2026, 9, 2), 555, 5)
    c5 = aggregate_minutes(mins, 300)
    assert len(c5) == 77 + 1
    assert c5[0]["ts"].startswith("2026-09-01T09:15") and c5[-1]["ts"].startswith("2026-09-02T09:15")
    assert c5[0]["o"] == 100 and c5[0]["c"] == 104.5 and c5[0]["h"] == 105 and c5[0]["l"] == 99
    c15 = aggregate_minutes(mins[:385], 900)
    assert len(c15) == 26            # 25 full windows + the short 15:30–15:39 one
    assert c15[-1]["ts"].startswith("2026-09-01T15:30")
    assert aggregate_minutes(mins[:3], 60) == [
        {"ts": m.ts.astimezone(__import__("app.core.time_utils", fromlist=["IST"]).IST).isoformat(),
         "o": m.o, "h": m.h, "l": m.l, "c": m.c} for m in mins[:3]
    ]


def _seconds(day: date, start_min: int, n: int):
    from app.algo.series import PremiumMinute
    from app.core.time_utils import ist_naive_to_utc

    out = []
    for i in range(n):
        ist = datetime.combine(day, datetime.min.time()) + timedelta(minutes=start_min, seconds=i)
        out.append(PremiumMinute(ts=ist_naive_to_utc(ist), o=100 + i, h=101 + i, l=99 + i, c=100.5 + i))
    return out


def test_aggregate_minutes_session_anchored_for_long_buckets():
    from app.algo.series import aggregate_minutes

    mins = _minutes(date(2026, 9, 1), 555, 385)
    c10 = aggregate_minutes(mins, 600)
    assert c10[0]["ts"].startswith("2026-09-01T09:15") and c10[1]["ts"].startswith("2026-09-01T09:25")
    assert len(c10) == 39                       # 38 full + the 15:35-15:39 stub
    c30 = aggregate_minutes(mins, 1800)
    assert [c["ts"][11:16] for c in c30[:3]] == ["09:15", "09:45", "10:15"]
    c1h = aggregate_minutes(mins, 3600)
    assert [c["ts"][11:16] for c in c1h[:2]] == ["09:15", "10:15"] and len(c1h) == 7
    c1d = aggregate_minutes(mins + _minutes(date(2026, 9, 2), 555, 30), 86400)
    assert len(c1d) == 2 and c1d[0]["ts"].startswith("2026-09-01T09:15")
    assert c1d[0]["o"] == 100 and c1d[0]["c"] == mins[-1].c


def test_aggregate_minutes_weekly_buckets_on_the_iso_week():
    """1W is a CALENDAR period, not 604800 seconds of intra-day arithmetic:
    every session of an ISO week folds into ONE candle stamped at that week's
    Monday 09:15. Doing it by seconds would collapse the week back onto its
    individual days (each day floors to the same 09:15 offset)."""
    from app.algo.series import aggregate_minutes

    # Tue 2026-09-01 … Fri 2026-09-04, then Mon 2026-09-07 (the next ISO week).
    mins = (
        _minutes(date(2026, 9, 1), 555, 10)
        + _minutes(date(2026, 9, 2), 555, 10)
        + _minutes(date(2026, 9, 3), 555, 10)
        + _minutes(date(2026, 9, 4), 555, 10)
        + _minutes(date(2026, 9, 7), 555, 10)
    )
    wk = aggregate_minutes(mins, 604800)
    assert len(wk) == 2, [c["ts"] for c in wk]
    # 2026-09-01 is a Tuesday → its week is stamped at Monday 2026-08-31.
    assert wk[0]["ts"].startswith("2026-08-31T09:15"), wk[0]["ts"]
    assert wk[1]["ts"].startswith("2026-09-07T09:15"), wk[1]["ts"]
    # The first weekly candle spans all four sessions of that week.
    first_week = mins[:40]
    assert wk[0]["o"] == first_week[0].o
    assert wk[0]["c"] == first_week[-1].c
    assert wk[0]["h"] == max(m.h for m in first_week)
    assert wk[0]["l"] == min(m.l for m in first_week)
    # A week must never equal the day series.
    assert len(aggregate_minutes(mins, 86400)) == 5


def test_weekly_is_distinct_from_daily_for_a_single_week():
    from app.algo.series import aggregate_minutes

    mins = _minutes(date(2026, 9, 1), 555, 5) + _minutes(date(2026, 9, 2), 555, 5)
    assert len(aggregate_minutes(mins, 86400)) == 2
    assert len(aggregate_minutes(mins, 604800)) == 1
    secs = _seconds(date(2026, 9, 1), 555, 120)
    c15s = aggregate_minutes(secs, 15)
    assert len(c15s) == 8 and c15s[1]["ts"][11:19] == "09:15:15"


def test_display_interval_table_matches_frontend_set():
    from app.api.algo_engines import _DISPLAY_INTERVALS

    assert list(_DISPLAY_INTERVALS) == [
        "1s", "5s", "15s", "30s", "1m", "3m", "5m", "10m", "15m", "30m",
        "1h", "2h", "3h", "1d", "1W",
    ]
    assert _DISPLAY_INTERVALS["1d"] == 86400 and _DISPLAY_INTERVALS["5s"] == 5
    assert _DISPLAY_INTERVALS["1W"] == 604800


def test_default_cut_follows_session_close():
    from datetime import time

    from app.api.algo_engines import default_cut_for

    assert default_cut_for(date(2026, 7, 1)) == time(15, 30)
    assert default_cut_for(date(2026, 9, 1)) == time(15, 40)


def test_choose_display_candles_fallbacks():
    from app.api.algo_engines import choose_display_candles

    mins = _minutes(date(2026, 9, 1), 555, 10)
    entry = [{"ts": "x", "o": 1, "h": 1, "l": 1, "c": 1}]
    r = choose_display_candles("entry", entry, 15, mins, None, date(2026, 9, 1))
    assert r["candles"] is entry and r["candles_interval"] == 900 and r["candles_source"] == "entry"
    r = choose_display_candles("5m", entry, 5, mins, None, date(2026, 9, 1))
    assert r["candles_interval"] == 300 and len(r["candles"]) == 2
    r = choose_display_candles("1s", entry, 5, mins, [], date(2026, 9, 1))
    assert r["candles_interval"] == 60 and "not stored" in r["candles_note"]
    secs = _minutes(date(2026, 9, 1), 555, 3)
    r = choose_display_candles("1s", entry, 5, mins, secs, date(2026, 9, 1))
    assert r["candles_source"] == "live_1s" and r["candles_interval"] == 1 and len(r["candles"]) == 3
    assert r["candles_window"]["from"].startswith("2026-09-01T09:15")
    r = choose_display_candles("30s", entry, 5, mins, _seconds(date(2026, 9, 1), 555, 90), date(2026, 9, 1))
    assert r["candles_source"] == "live_1s" and r["candles_interval"] == 30 and len(r["candles"]) == 3
    r = choose_display_candles("1h", entry, 5, mins, None, date(2026, 9, 1))
    assert r["candles_interval"] == 3600 and len(r["candles"]) == 1 and r["candles_window"] is None


def test_replay_ump_returns_entry_tf_candles():
    from app.api.algo_engines import replay_ump

    mins = _minutes(date(2026, 9, 1), 555, 30)
    p5 = UmpParams()
    p15 = UmpParams()
    p15.entry.entry_timeframe_min = 15
    _, c5 = replay_ump(mins, p5)
    eng, c15 = replay_ump(mins, p15, record_history=True)
    assert len(c5) == 6 and len(c15) == 2
    assert eng.state_history, "history requested"
    assert eng.levels_history == [] or isinstance(eng.levels_history, list)
