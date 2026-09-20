"""§5 Telegram alert texts (pure builders) + notify() safety. DB-free."""
from __future__ import annotations

from datetime import datetime

from app.algo.alerts import (
    entry_message,
    exit_message,
    fmt_ist,
    kill_message,
    pause_message,
    risk_kill_message,
)

NOW = datetime(2026, 9, 9, 9, 31)


def test_fmt_ist():
    assert fmt_ist(NOW) == "09:31 IST"
    assert fmt_ist(None) == "--:-- IST"


def test_entry_message_has_price_lots_time_scenario():
    m = entry_message(
        now=NOW, side="CALL", symbol="NIFTY", strike=24500, option_type="CE",
        expiry="2026-09-15", fill=101.5, lots=3, lot_size=65, sub_scenario="S1A",
        day="tuesday", zone="Z1", ledger="paper", trade_id=12,
    )
    assert "ENTRY CALL" in m and "24500CE" in m and "₹101.50" in m
    assert "× 3 lot(s)" in m and "195 qty" in m
    assert "09:31 IST" in m and "S1A" in m and "trade #12" in m and "[paper]" in m


def test_exit_message_has_entry_exit_pnl_pct_fees_time():
    m = exit_message(
        now=datetime(2026, 9, 9, 9, 58), side="CALL", symbol="NIFTY", strike=24500,
        option_type="CE", entry_fill=101.5, exit_fill=110.0, exit_reason="TARGET",
        pnl=1595.0, pnl_pct=5.32, fees_total=62.4, held_min=27, day="tuesday",
        zone="Z1", ledger="paper", trade_id=12,
    )
    assert "EXIT CALL" in m and "09:58 IST" in m and "Target hit" in m
    assert "entry ₹101.50 → exit ₹110.00 (+8.37%)" in m
    assert "P&L +₹1,595 (+5.32% of allocated)" in m
    assert "fees ₹62" in m and "held 27 min" in m and "trade #12" in m


def test_exit_message_without_pct_or_fees():
    m = exit_message(
        now=NOW, side="PUT", symbol="SENSEX", strike=80000, option_type="PE",
        entry_fill=200.0, exit_fill=180.0, exit_reason="MAX_SL", pnl=-1300.0,
        pnl_pct=None, fees_total=None, held_min=None, day="monday", zone="Z2",
        ledger="live", trade_id=3,
    )
    assert "P&L −₹1,300" in m and "of allocated" not in m and "fees" not in m
    assert "Stop-Loss (Max cap)" in m


def test_risk_kill_messages_carry_time_reason_and_figures():
    m = risk_kill_message("max_loss", now=datetime(2026, 9, 9, 11, 42), realized=-3120,
                          limit=3000, pct=20.0, allocated=15000)
    assert m.startswith("🛑 KILL 11:42 IST — Max daily loss breached")
    assert "−₹3,120" in m and "−₹3,000" in m and "20%" in m and "₹15,000" in m
    p = risk_kill_message("profit_lock", now=datetime(2026, 9, 9, 13, 5), realized=4610,
                          limit=4500, pct=30.0, allocated=15000)
    assert p.startswith("✅ KILL 13:05 IST — Profit lock reached") and "+₹4,610" in p


def test_pause_and_kill_messages():
    m = pause_message(now=datetime(2026, 9, 9, 12, 20), streak=3, streak_loss=2450,
                      cap_trades=3, cap_pct=25.0)
    assert "⏸ PAUSE 12:20 IST" in m and "auto-paused" in m and "3 consecutive losses" in m and "Manual Resume" in m
    d = kill_message(now=datetime(2026, 9, 9, 9, 15), scope="day", reason="day kill", day="tuesday")
    assert d == "🛑 09:15 IST — Trading is OFF today: day kill. No zones will be evaluated."
    z = kill_message(now=datetime(2026, 9, 9, 10, 31), scope="zone", reason="zone kill switch",
                     day="tuesday", zone="Z2")
    assert "10:31 IST" in z and "tuesday Z2 is killed (zone kill switch)" in z


def test_notify_is_inert_when_unconfigured(monkeypatch):
    from app.core import notify as n

    monkeypatch.setattr(n.settings, "telegram_bot_token", "")
    monkeypatch.setattr(n.settings, "telegram_chat_id", "")
    assert not n.is_configured()
    n.notify("x", "y")   # must not raise, must not schedule


async def test_probe_and_send_now_unconfigured(monkeypatch):
    from app.core import notify as n

    monkeypatch.setattr(n.settings, "telegram_bot_token", "")
    monkeypatch.setattr(n.settings, "telegram_chat_id", "")
    r = await n.probe(force=True)
    assert r["configured"] is False and r["ok"] is None
    ok, detail = await n.send_now("hi")
    assert not ok and detail == "not configured"


def test_mask_chat_never_reveals_more_than_4():
    from app.core.notify import _mask_chat

    assert _mask_chat("123456789") == "…6789"
    assert _mask_chat("12") == "…"
