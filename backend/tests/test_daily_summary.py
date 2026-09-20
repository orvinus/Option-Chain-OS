"""§5 daily Telegram summary: builder text + the 15:45 scheduler. DB-free."""
from __future__ import annotations

from datetime import date, datetime

from app.algo.daily_summary import build_daily_summary, summary_due

D = date(2026, 9, 9)   # Wednesday


def _row(zone, pnl, side="CALL", strike=24500, ot="CE", exit_hm="10:15", fees=25.0):
    return {
        "zone_id": zone, "pnl_rupees": pnl, "side": side, "strike": strike,
        "option_type": ot, "exit_ts": f"2026-09-09T{exit_hm}:00+05:30",
        "fees": {"total": fees}, "exit_reason": "TARGET",
    }


def test_summary_due_matrix():
    sent = None
    assert not summary_due(datetime(2026, 9, 9, 15, 44), sent, False)
    assert summary_due(datetime(2026, 9, 9, 15, 45), sent, False)
    assert summary_due(datetime(2026, 9, 9, 16, 10), sent, False), "restart after 15:45 still sends"
    assert not summary_due(datetime(2026, 9, 9, 16, 10), D, False), "already sent today"
    assert summary_due(datetime(2026, 9, 10, 15, 50), D, False), "next day again"
    assert not summary_due(datetime(2026, 9, 12, 15, 50), None, False), "Saturday"
    assert not summary_due(datetime(2026, 9, 9, 15, 50), None, True), "holiday"


def test_summary_no_trades():
    t = build_daily_summary(day=D, ledger="paper", closed=[], allocated=30000,
                            kill_events=[], open_position=None, now=datetime(2026, 9, 9, 15, 45))
    assert t.startswith("📋 Daily summary 2026-09-09 (Wednesday) [paper] — sent 15:45 IST")
    assert "Trades 0" in t and "Kills: none" in t and "Open/carried: none" in t


def test_summary_mixed_trades_kills_and_open_position():
    closed = [
        _row("Z1", 1595.0, exit_hm="09:58"),
        _row("Z1", -560.0, side="PUT", strike=24400, ot="PE", exit_hm="11:40"),
        _row("Z2", 821.0, exit_hm="13:10"),
    ]
    t = build_daily_summary(
        day=D, ledger="paper", closed=closed, allocated=30000,
        kill_events=[(datetime(2026, 9, 9, 11, 42), "Max daily loss breached")],
        open_position={"side": "CALL", "strike": 24600, "option_type": "CE", "lots": 2,
                       "entry_fill": 98.0, "unrealized_gross": 410.0},
        now=datetime(2026, 9, 9, 15, 45),
    )
    lines = t.split("\n")
    assert "Trades 3 · 2W/1L · win 67%" in t
    assert "Gross +₹1,931 · fees ₹75 · Net +₹1,856 (+6.2% of allocated ₹30,000)" in t
    assert "Zones: Z1 +₹1,035 (2) · Z2 +₹821 (1)" in t
    assert "Best +₹1,595 CALL 24500CE · Worst −₹560 PUT 24400PE" in t
    assert any(l.startswith("Max intraday drawdown −₹560 (1.9%) 09:58→11:40") for l in lines), lines
    assert "Kills: 11:42 Max daily loss breached" in t
    assert "Open/carried: CALL 24600CE × 2 @ ₹98.00, unrealized +₹410" in t


def test_summary_drawdown_none_when_monotonic():
    closed = [_row("Z1", 100.0, exit_hm="10:00"), _row("Z1", 200.0, exit_hm="11:00")]
    t = build_daily_summary(day=D, ledger="live", closed=closed, allocated=0,
                            kill_events=[], open_position=None, now=datetime(2026, 9, 9, 15, 45))
    assert "Max intraday drawdown: none" in t and "of allocated" not in t
