"""§8 paper completeness — the orchestrator's position status carries a
mark-to-market, ``GET /api/algo/paper/session`` exposes it, and
``POST /api/algo/paper/reset`` refuses while a paper position is open.
Plus the §7 drawdown fields of ``/api/algo/pnl/summary``.

Run:  cd backend && PYTHONPATH=. python -m pytest tests/test_paper_session_api.py -q

No database: the handlers are called directly with the store reads, the
config store and the orchestrator lookup monkeypatched; the position status
test drives a REAL ZoneOrchestrator through the M4 fake world.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from fastapi import HTTPException

from app.algo.config_models import AlgoConfig, default_config
from app.algo.orchestrator import Contract, ZoneOrchestrator, _Position
from app.api import algo_trades as at
from test_algo_orchestrator import MONDAY, Fake, MinuteBar, enter_position, run


# ── orchestrator status: mark-to-market fields ────────────────────────────

def test_position_status_carries_mark_to_market():
    fake = Fake()
    orch = ZoneOrchestrator(fake.deps())
    enter_position(fake, orch)
    pos = orch.status.position
    assert pos is not None and pos["ledger"] == "paper"
    p = orch.position
    assert pos["strike"] == 24500 and pos["option_type"] == "CE"
    assert pos["expiry"] == "2026-08-20"
    assert pos["lot_size"] == fake.lot == 75
    assert pos["last_close"] == p.engine.last_close == 100.6
    exp_gross = round((100.6 - p.entry_fill) * 75 * p.lots, 2)
    assert pos["unrealized_gross"] == exp_gross
    assert pos["unrealized_pct"] == round((100.6 - p.entry_fill) / p.entry_fill * 100, 2)
    assert "entry_ts" in pos

    # The next managed minute re-marks off the new close.
    fake.bars = [MinuteBar(ts=datetime(2026, 8, 17, 9, 31), o=100.6, h=103.0, l=100.5, c=102.4)]
    run(orch, datetime(2026, 8, 17, 9, 31))
    if orch.position is not None:
        pos2 = orch.status.position
        assert pos2["last_close"] == 102.4
        assert pos2["unrealized_gross"] == round((102.4 - p.entry_fill) * 75 * p.lots, 2)


def test_position_status_without_close_reports_null_unrealized():
    fake = Fake()
    orch = ZoneOrchestrator(fake.deps())
    eng = SimpleNamespace(sub="ADOPTED", trail_sl=None, max_sl=None, last_close=None)
    orch.position = _Position(
        trade_id=41, contract=Contract("NIFTY", date(2026, 8, 20), 24600, "PE"),
        zone_id="Z1", day="monday", side="PUT", entry_fill=88.0, lots=2,
        ledger="paper", engine=eng,  # type: ignore[arg-type]
    )
    orch._fill_position_status()
    pos = orch.status.position
    assert pos["last_close"] is None
    assert pos["unrealized_gross"] is None and pos["unrealized_pct"] is None
    assert pos["strike"] == 24600 and pos["option_type"] == "PE" and pos["lot_size"] == 75


# ── /paper/session and /paper/reset ───────────────────────────────────────

class _FakeConfigStore:
    def __init__(self, cfg: AlgoConfig) -> None:
        self.cfg = cfg
        self.saved: list[AlgoConfig] = []

    async def get_live(self):
        return SimpleNamespace(config=self.cfg)

    async def save(self, cfg, **kw):
        self.saved.append(cfg)


def _fake_orch(position: Optional[dict[str, Any]], live_pos: Any = None):
    return SimpleNamespace(status=SimpleNamespace(position=position), position=live_pos)


def _patch_common(monkeypatch: pytest.MonkeyPatch, cfg: AlgoConfig, *, realized: float,
                  closed_today: list[tuple[str, float]]) -> _FakeConfigStore:
    cs = _FakeConfigStore(cfg)
    monkeypatch.setattr(at, "get_config_store", lambda: cs)

    async def paper_realized_since(since):
        return realized

    async def today_closed(trade_date, ledger):
        assert ledger == "paper"
        return closed_today

    monkeypatch.setattr(at.store, "paper_realized_since", paper_realized_since)
    monkeypatch.setattr(at.store, "today_closed", today_closed)
    return cs


PAPER_POS = {
    "trade_id": 12, "side": "CALL", "contract": "NIFTY:260820:24500:CE",
    "entry": 101.5, "lots": 3, "zone": "Z1", "ledger": "paper", "sub_scenario": "R1",
    "trail_sl": None, "max_sl": 93.4, "last_close": 104.0, "lot_size": 65,
    "unrealized_gross": 487.5, "unrealized_pct": 2.46,
    "entry_ts": "2026-08-17T09:31:00", "strike": 24500, "option_type": "CE",
    "expiry": "2026-08-20",
}


def test_paper_session_exposes_open_position_and_equity(monkeypatch: pytest.MonkeyPatch):
    cfg = default_config(today=date(2026, 8, 10))
    cfg.global_.paper.session_started = "2026-08-10"
    _patch_common(monkeypatch, cfg, realized=-560.0,
                  closed_today=[("Z1", 300.0), ("Z2", -860.0)])
    monkeypatch.setattr(at, "get_orchestrator", lambda: _fake_orch(PAPER_POS))

    s = asyncio.run(at.paper_session(_=None))
    assert s["session_started"] == "2026-08-10"
    assert s["starting_balance"] == 30000.0
    assert s["realized_pnl"] == -560.0 and s["current_balance"] == 29440.0
    assert s["open_position"] == {
        "trade_id": 12, "strike": 24500, "option_type": "CE", "side": "CALL",
        "lots": 3, "entry_fill": 101.5, "entry_ts": "2026-08-17T09:31:00",
        "last_close": 104.0, "unrealized_gross": 487.5, "unrealized_pct": 2.46,
    }
    assert s["unrealized_pnl"] == 487.5
    assert s["equity"] == 29440.0 + 487.5
    assert s["trades_today"] == 2 and s["realized_today"] == -560.0


def test_paper_session_ignores_live_position_and_missing_orchestrator(monkeypatch: pytest.MonkeyPatch):
    cfg = default_config(today=date(2026, 8, 10))
    _patch_common(monkeypatch, cfg, realized=0.0, closed_today=[])

    live_pos = dict(PAPER_POS, ledger="live")
    monkeypatch.setattr(at, "get_orchestrator", lambda: _fake_orch(live_pos))
    s = asyncio.run(at.paper_session(_=None))
    assert s["open_position"] is None
    assert s["unrealized_pnl"] == 0.0 and s["equity"] == s["current_balance"] == 30000.0
    assert s["trades_today"] == 0 and s["realized_today"] == 0.0

    monkeypatch.setattr(at, "get_orchestrator", lambda: None)
    s = asyncio.run(at.paper_session(_=None))
    assert s["open_position"] is None and s["equity"] == 30000.0


def test_paper_session_unmarked_position_contributes_zero(monkeypatch: pytest.MonkeyPatch):
    cfg = default_config(today=date(2026, 8, 10))
    _patch_common(monkeypatch, cfg, realized=100.0, closed_today=[("Z1", 100.0)])
    pos = dict(PAPER_POS, last_close=None, unrealized_gross=None, unrealized_pct=None)
    monkeypatch.setattr(at, "get_orchestrator", lambda: _fake_orch(pos))
    s = asyncio.run(at.paper_session(_=None))
    assert s["open_position"]["unrealized_gross"] is None
    assert s["unrealized_pnl"] == 0.0 and s["equity"] == 30100.0


def _ident():
    return SimpleNamespace(username="admin", user_id=1)


def test_paper_reset_refused_while_paper_position_open(monkeypatch: pytest.MonkeyPatch):
    cfg = default_config(today=date(2026, 8, 10))
    cs = _FakeConfigStore(cfg)
    monkeypatch.setattr(at, "get_config_store", lambda: cs)
    calls = {"reset": 0}

    async def reset_paper_ledger():
        calls["reset"] += 1
        return 3

    monkeypatch.setattr(at.store, "reset_paper_ledger", reset_paper_ledger)
    live_pos = SimpleNamespace(trade_id=12, ledger="paper")
    monkeypatch.setattr(at, "get_orchestrator", lambda: _fake_orch(PAPER_POS, live_pos))

    with pytest.raises(HTTPException) as ei:
        asyncio.run(at.paper_reset(ident=_ident()))
    assert ei.value.status_code == 409
    assert ei.value.detail == "a paper position is open (trade #12) — wait for its exit before resetting"
    assert calls["reset"] == 0 and cs.saved == [], "nothing may be touched on refusal"


def test_paper_reset_proceeds_with_live_position_or_none(monkeypatch: pytest.MonkeyPatch):
    cfg = default_config(today=date(2026, 8, 10))
    cs = _FakeConfigStore(cfg)
    monkeypatch.setattr(at, "get_config_store", lambda: cs)

    async def reset_paper_ledger():
        return 3

    async def audit(*a, **k):
        return None

    monkeypatch.setattr(at.store, "reset_paper_ledger", reset_paper_ledger)
    monkeypatch.setattr(at, "audit", audit)

    live_pos = SimpleNamespace(trade_id=9, ledger="live")
    monkeypatch.setattr(at, "get_orchestrator", lambda: _fake_orch(None, live_pos))
    out = asyncio.run(at.paper_reset(ident=_ident()))
    assert out == {"status": "ok", "deleted_rows": 3}
    assert len(cs.saved) == 1 and cs.saved[0].global_.paper.session_started

    monkeypatch.setattr(at, "get_orchestrator", lambda: None)
    out = asyncio.run(at.paper_reset(ident=_ident()))
    assert out["status"] == "ok"


# ── /pnl/summary drawdown fields ──────────────────────────────────────────

def test_pnl_summary_drawdown_fields(monkeypatch: pytest.MonkeyPatch):
    cfg = default_config(today=date(2026, 8, 10))
    cfg.global_.demat_balance = 100000.0
    cs = _FakeConfigStore(cfg)
    monkeypatch.setattr(at, "get_config_store", lambda: cs)
    seen: dict[str, Any] = {}

    async def closed_points(ledger, from_date, to_date):
        seen.update(ledger=ledger, from_date=from_date, to_date=to_date)
        return [
            ("2026-08-17T09:58:00", 300.0),
            ("2026-08-17T11:20:00", -860.0),
            ("2026-08-18T10:05:00", 200.0),
        ]

    monkeypatch.setattr(at.store, "closed_points", closed_points)

    f = asyncio.run(at._drawdown_fields("paper", "2026-08-17", "2026-08-21"))
    assert seen == {"ledger": "paper", "from_date": "2026-08-17", "to_date": "2026-08-21"}
    # equity 30000 → 30300 (peak) → 29440 → 29640: trough 860 below the peak
    assert f["max_drawdown"] == 860.0
    assert f["max_drawdown_pct"] == round(860 / 30300 * 100, 2)
    assert f["max_drawdown_from"] == "2026-08-17T09:58:00"
    assert f["max_drawdown_to"] == "2026-08-17T11:20:00"
    assert f["drawdown_basis"] == at.DRAWDOWN_BASIS

    # Live ledger anchors on the demat balance → a different percentage.
    f = asyncio.run(at._drawdown_fields("live", "2026-08-17", "2026-08-21"))
    assert f["max_drawdown"] == 860.0
    assert f["max_drawdown_pct"] == round(860 / 100300 * 100, 2)

    async def none_closed(ledger, from_date, to_date):
        return []

    monkeypatch.setattr(at.store, "closed_points", none_closed)
    f = asyncio.run(at._drawdown_fields("paper", "2026-08-17", "2026-08-21"))
    assert (f["max_drawdown"], f["max_drawdown_pct"], f["max_drawdown_from"],
            f["max_drawdown_to"]) == (0.0, 0.0, None, None)
