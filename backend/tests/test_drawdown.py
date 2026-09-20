"""§7 max drawdown — the shared helper must reproduce the ORIGINAL backtest
summarize() loop byte-for-byte, and summarize() must map its result onto the
same keys it always returned.

Run:  cd backend && PYTHONPATH=. python -m pytest tests/test_drawdown.py -q

No database: summarize() is exercised with its three store reads
monkeypatched.
"""
from __future__ import annotations

import asyncio
import random
from typing import Any, Optional

import pytest

from app.algo.drawdown import Drawdown, equity_points_from_pnl, scan_drawdown


def _legacy_loop(equity_points: list[dict[str, Any]], starting: float) -> tuple:
    """Verbatim copy of backtest/store.summarize()'s drawdown loop as it
    stood before the helper existed (2026-09-09) — the regression oracle."""
    equity = starting
    peak = starting
    max_dd = 0.0
    max_dd_pct = 0.0
    dd_from = dd_to = None
    peak_date: Optional[str] = None
    for p in equity_points:
        equity = p["equity"]
        if equity > peak:
            peak = equity
            peak_date = p["date"]
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = round(dd / peak * 100, 2) if peak > 0 else 0.0
            dd_from = peak_date
            dd_to = p["date"]
    return round(max_dd, 2), max_dd_pct, dd_from, dd_to


def _helper(points: list[dict[str, Any]], starting: float) -> tuple:
    d = scan_drawdown(((p["date"], p["equity"]) for p in points), starting=starting)
    return round(d.max_dd, 2), d.max_dd_pct, d.from_key, d.to_key


def _pts(*equities: float) -> list[dict[str, Any]]:
    return [{"date": f"2026-08-{i + 1:02d}", "equity": float(e)} for i, e in enumerate(equities)]


def test_empty_series_is_flat():
    assert _helper([], 30000.0) == _legacy_loop([], 30000.0) == (0.0, 0.0, None, None)
    assert scan_drawdown([], starting=0.0) == Drawdown(0.0, 0.0, None, None)


def test_monotonic_up_has_no_drawdown():
    pts = _pts(30100, 30250, 30900, 31000)
    assert _helper(pts, 30000.0) == _legacy_loop(pts, 30000.0) == (0.0, 0.0, None, None)


def test_flat_series_has_no_drawdown():
    pts = _pts(30000, 30000, 30000)
    assert _helper(pts, 30000.0) == _legacy_loop(pts, 30000.0) == (0.0, 0.0, None, None)


def test_first_day_loss_measures_from_starting_with_no_peak_key():
    """The peak key stays None until equity first EXCEEDS ``starting`` —
    a loss straight out of the gate reports from=None, to=day 1."""
    pts = _pts(29440, 29600, 29900)
    got = _helper(pts, 30000.0)
    assert got == _legacy_loop(pts, 30000.0)
    assert got == (560.0, 1.87, None, "2026-08-01")


def test_peak_to_trough_uses_peak_at_the_time_of_the_trough():
    pts = _pts(30500, 31000, 30400, 30700, 32000, 31500)
    got = _helper(pts, 30000.0)
    assert got == _legacy_loop(pts, 30000.0)
    # deepest = 31000 → 30400 (600); the later 32000 → 31500 (500) is smaller
    assert got == (600.0, round(600 / 31000 * 100, 2), "2026-08-02", "2026-08-03")


def test_equal_depth_keeps_the_first_occurrence():
    """Strict ``>`` — a later drawdown of the same rupee depth does not move
    the from/to record."""
    pts = _pts(31000, 30500, 31000, 30500)
    got = _helper(pts, 30000.0)
    assert got == _legacy_loop(pts, 30000.0)
    assert got[2:] == ("2026-08-01", "2026-08-02")


def test_zero_or_negative_peak_reports_zero_pct():
    pts = _pts(-100, -400)
    got = _helper(pts, 0.0)
    assert got == _legacy_loop(pts, 0.0)
    assert got == (400.0, 0.0, None, "2026-08-02")


def test_helper_equals_legacy_loop_on_random_series():
    rng = random.Random(20260909)
    for _ in range(2000):
        n = rng.randint(0, 25)
        starting = rng.choice([0.0, 100.0, 30000.0, 250000.0, -50.0])
        equity = starting
        pts = []
        for i in range(n):
            equity += rng.choice([-1, 1]) * rng.choice([0.0, 1.0, 37.5, 560.0, 1234.56])
            pts.append({"date": f"d{i:03d}", "equity": round(equity, 2)})
        assert _helper(pts, starting) == _legacy_loop(pts, starting), (starting, pts)


def test_equity_points_from_pnl_cumulates_in_order():
    pts = equity_points_from_pnl(
        [("t1", 100.0), ("t2", -250.5), ("t3", None), ("t4", 50.0)], starting=30000.0
    )
    assert pts == [("t1", 30100.0), ("t2", 29849.5), ("t3", 29849.5), ("t4", 29899.5)]
    assert equity_points_from_pnl([], starting=5.0) == []


# ── summarize() mapping — same keys, same numbers, no DB ──────────────────

def test_summarize_maps_helper_onto_legacy_keys(monkeypatch: pytest.MonkeyPatch):
    from app.algo.backtest import store as bt_store

    days = [
        {"trade_date": "2026-08-03", "status": "done",
         "detail": {"net": 500.0, "equity_after": 30500.0}},
        {"trade_date": "2026-08-04", "status": "done",
         "detail": {"net": -900.0, "equity_after": 29600.0}},
        {"trade_date": "2026-08-05", "status": "skipped", "detail": {}},
        {"trade_date": "2026-08-06", "status": "done",
         "detail": {"net": 1200.0, "equity_after": 30800.0}},
        {"trade_date": "2026-08-07", "status": "done",
         "detail": {"net": -300.0, "equity_after": 30500.0}},
    ]
    trades = [
        {"exit_ts": "2026-08-03T10:00:00", "exit_date": "2026-08-03", "day": "monday",
         "zone_id": "Z1", "pnl_rupees": 500.0, "fees": {"total": 40.0}},
        {"exit_ts": "2026-08-04T10:00:00", "exit_date": "2026-08-04", "day": "tuesday",
         "zone_id": "Z1", "pnl_rupees": -900.0, "fees": {"total": 40.0}},
        {"exit_ts": None, "exit_date": None, "day": "friday",
         "zone_id": "Z2", "pnl_rupees": None, "fees": None},
    ]

    async def run_trades(run_id, limit=10000):
        return trades

    async def run_days(run_id):
        return days

    async def get_run(run_id, include_config=False):
        return {"settings": {"starting_balance": 30000.0}}

    monkeypatch.setattr(bt_store, "run_trades", run_trades)
    monkeypatch.setattr(bt_store, "run_days", run_days)
    monkeypatch.setattr(bt_store, "get_run", get_run)

    s = asyncio.run(bt_store.summarize(7))
    pts = [{"date": d["trade_date"], "equity": d["detail"]["equity_after"]}
           for d in days if d["status"] == "done"]
    exp_dd, exp_pct, exp_from, exp_to = _legacy_loop(pts, 30000.0)
    assert s["max_drawdown"] == exp_dd == 900.0
    assert s["max_drawdown_pct"] == exp_pct == round(900 / 30500 * 100, 2)
    assert s["max_drawdown_from"] == exp_from == "2026-08-03"
    assert s["max_drawdown_to"] == exp_to == "2026-08-04"
    assert s["starting_balance"] == 30000.0
    assert s["final_equity"] == 30500.0
    assert s["trades"] == 2 and s["wins"] == 1 and s["losses"] == 1
    assert [p["date"] for p in s["equity"]] == [p["date"] for p in pts]


def test_summarize_without_starting_balance_anchors_on_first_day(monkeypatch: pytest.MonkeyPatch):
    from app.algo.backtest import store as bt_store

    days = [
        {"trade_date": "2026-08-03", "status": "done",
         "detail": {"net": -200.0, "equity_after": 29800.0}},
        {"trade_date": "2026-08-04", "status": "done",
         "detail": {"net": -100.0, "equity_after": 29700.0}},
    ]

    async def run_trades(run_id, limit=10000):
        return []

    async def run_days(run_id):
        return days

    async def get_run(run_id, include_config=False):
        return {"settings": {}}

    monkeypatch.setattr(bt_store, "run_trades", run_trades)
    monkeypatch.setattr(bt_store, "run_days", run_days)
    monkeypatch.setattr(bt_store, "get_run", get_run)

    s = asyncio.run(bt_store.summarize(8))
    pts = [{"date": d["trade_date"], "equity": d["detail"]["equity_after"]} for d in days]
    assert s["starting_balance"] == 30000.0
    assert (s["max_drawdown"], s["max_drawdown_pct"], s["max_drawdown_from"],
            s["max_drawdown_to"]) == _legacy_loop(pts, 30000.0) == (300.0, 1.0, None, "2026-08-04")
