"""Backtest ↔ live gate parity (2026-09-09, user-approved):

* the frame carries the date's OWN session length (375 before 2026-08-03,
  385 since) and a last-tick index that feeds the 180 s staleness gate;
* the platform holiday file is frozen into the run and unioned like live;
* `_frame_gaps` names the ≥3-minute holes before execution;
* H2: `build_premium_life` anchors its fetch floor on the cursor;
* H3: preflight's duplication risk keys on the quality index, not presence.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

from app.algo.backtest import runner as runner_mod
from app.algo.backtest.data import MINUTES_PER_DAY, build_day_frame, minute_bar
from app.algo.backtest.deps import BacktestDeps, EventCollector, InMemoryLedger
from app.algo.config_models import default_config
from app.algo.orchestrator import Contract


def _open(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 3, 45, tzinfo=timezone.utc)   # 09:15 IST


def _row(open_utc, strike, ot, i, oi=1000, ltp=100.0):
    return {"strike": strike, "option_type": ot, "bucket": open_utc + timedelta(minutes=i),
            "oi": oi, "o": ltp, "h": ltp + 0.5, "l": ltp - 0.5, "c": ltp}


def _frame(d: date, tick_minutes: list[int]):
    o = _open(d)
    chain = [_row(o, 22500, ot, i) for i in tick_minutes for ot in ("CE", "PE")]
    f = build_day_frame(trade_date=d, symbol="NIFTY", expiry=d, open_utc=o, chain=chain,
                        spot_rows=[], preopen=22500.0, bounds=(22000, 23000), strike_step=50)
    assert f is not None
    return f


def test_frame_minutes_per_day_follows_the_session_close_change():
    pre = _frame(date(2026, 7, 31), [0, 1])
    post = _frame(date(2026, 9, 1), [0, 1])
    assert pre.minutes_per_day == 375 and post.minutes_per_day == 385
    assert len(pre.last_tick_upto) == MINUTES_PER_DAY


def test_last_tick_upto_tracks_the_latest_basket_tick():
    f = _frame(date(2026, 9, 1), [0, 1, 2, 10])
    assert f.last_tick_upto[0] == 0 and f.last_tick_upto[2] == 2
    assert f.last_tick_upto[5] == 2 and f.last_tick_upto[9] == 2
    assert f.last_tick_upto[10] == 10 and f.last_tick_upto[384] == 10


def test_minute_bar_respects_the_date_own_last_bar():
    pre = _frame(date(2026, 7, 31), list(range(0, 385)))
    post = _frame(date(2026, 9, 1), list(range(0, 385)))
    assert minute_bar(pre, 22500, "CE", datetime(2026, 7, 31, 15, 29)) is not None
    assert minute_bar(pre, 22500, "CE", datetime(2026, 7, 31, 15, 30)) is None
    assert minute_bar(post, 22500, "CE", datetime(2026, 9, 1, 15, 39)) is not None
    assert minute_bar(post, 22500, "CE", datetime(2026, 9, 1, 15, 40)) is None


def test_backtest_data_age_mirrors_the_live_staleness_probe():
    d = date(2026, 9, 1)
    f = _frame(d, [0, 1, 2])           # ticks at 09:15, 09:16, 09:17 only
    bd = BacktestDeps(cfg=default_config(today=d), frame=f, ledger=InMemoryLedger(30000.0, "compounding"),
                      collector=EventCollector(), lot_sizes={"NIFTY": 65})
    deps = bd.as_orchestrator_deps()
    assert deps.data_age_s is not None

    async def go():
        bd.set_now(datetime(2026, 9, 1, 9, 15))          # nothing closed yet
        assert await deps.data_age_s("NIFTY") is None
        bd.set_now(datetime(2026, 9, 1, 9, 18))          # closed minute 09:17 has a tick
        assert await deps.data_age_s("NIFTY") == 0.0
        bd.set_now(datetime(2026, 9, 1, 9, 20))          # 09:19 closed; last tick 09:17
        assert await deps.data_age_s("NIFTY") == 120.0
        bd.set_now(datetime(2026, 9, 1, 9, 22))          # 240 s > 180 s gate
        assert await deps.data_age_s("NIFTY") == 240.0

    asyncio.run(go())


def test_platform_holidays_frozen_into_deps():
    d = date(2026, 9, 1)
    f = _frame(d, [0, 1])
    base = dict(cfg=default_config(today=d), frame=f, ledger=InMemoryLedger(30000.0, "compounding"),
                collector=EventCollector(), lot_sizes={"NIFTY": 65})
    without = BacktestDeps(**base).as_orchestrator_deps()
    assert without.is_platform_holiday is None, "pre-feature runs stay byte-identical"
    with_h = BacktestDeps(platform_holidays=frozenset({"2026-10-02"}), **base).as_orchestrator_deps()
    assert with_h.is_platform_holiday is not None
    assert with_h.is_platform_holiday(date(2026, 10, 2)) is True
    assert with_h.is_platform_holiday(date(2026, 10, 1)) is False


def test_frame_gaps_reports_runs_of_three_or_more_silent_minutes():
    f = _frame(date(2026, 9, 1), [0, 1, 2, 5, 6, 12, 13])   # holes: 3-4 (2 min), 7-11 (5 min), 14.. (tail)
    gaps = runner_mod._frame_gaps(f)
    assert gaps[0] == ("09:22", "09:26", 5)
    assert gaps[-1][2] == 385 - 14                      # the silent tail to the close
    assert all(g[2] >= 3 for g in gaps)
    pre = _frame(date(2026, 7, 31), list(range(0, 375)))
    assert runner_mod._frame_gaps(pre) == []             # 375-minute day fully covered


def test_collector_keeps_decisions_for_the_runner():
    col = EventCollector()
    col.record_decision({"ts": datetime(2026, 9, 1, 9, 30), "stage": "hunting"})
    assert col.decisions and col.decisions[0]["stage"] == "hunting"


async def test_build_premium_life_anchors_fetch_on_the_cursor(monkeypatch):
    from app.algo import series

    seen: list[datetime] = []

    async def fake_fetch(symbol, expiry, strike, ot, to_ts):
        seen.append(to_ts)
        return []

    async def fake_official(*a, **k):
        return {}

    monkeypatch.setattr(series, "fetch_premium_minutes", fake_fetch)
    monkeypatch.setattr(series, "fetch_official_closes", fake_official)
    now = datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc)
    cut = datetime(2026, 4, 16, 10, 0, tzinfo=timezone.utc)
    await series.build_premium_life("NIFTY", date(2026, 4, 21), 23850, "PE", cut_utc=cut, now=now)
    assert seen == [cut], "the fetch floor must anchor on the as-of cursor, not on now"
    await series.build_premium_life("NIFTY", date(2026, 4, 21), 23850, "PE", now=now)
    assert seen[-1] == now


def test_preflight_dup_risk_keys_on_the_quality_index():
    from app.algo.backtest import preflight

    assert "oi_day_stats" in str(preflight._STATS_DAYS_SQL)
    assert not hasattr(preflight, "_MATVIEW_DAYS_SQL")
    # the day-plan carries the expected session length + coverage
    plan = preflight.DayPlan(trade_date="2026-09-01", symbol="NIFTY", expiry=None)
    assert plan.expected_minutes == 0 and plan.coverage_pct == 0.0
