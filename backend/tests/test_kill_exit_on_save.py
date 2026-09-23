"""Config Save/Restore → immediate kill exit (user rule 2026-09-23).

Switching a kill ON while a trade runs must exit it at once, not at the
orchestrator's next minute. ``_kill_exit_now`` is what both endpoints call
after a successful save; it must never fail the save itself.

Run:  cd backend && PYTHONPATH=. python -m pytest -q tests/test_kill_exit_on_save.py
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

import app.algo.orchestrator as orch_mod
import app.core.time_utils as tu
from app.api.algo_config import _kill_exit_now


class _StubOrch:
    def __init__(self, position: Any, result: Optional[dict[str, Any]], boom: bool = False):
        self.position = position
        self._result = result
        self._boom = boom
        self.calls = 0

    async def kill_square_off(self, now):  # noqa: ANN001
        self.calls += 1
        if self._boom:
            raise RuntimeError("broker down")
        return self._result


def _with(monkeypatch, orch: Optional[_StubOrch], session_open: bool) -> None:
    monkeypatch.setattr(orch_mod, "get_orchestrator", lambda: orch)
    monkeypatch.setattr(tu, "is_nse_regular_session_open", lambda *a, **k: session_open)


RESULT = {"trade_id": 12, "ledger": "paper", "closed": True, "exit_reason": "DAY_KILL", "extra": 1}


def test_open_trade_during_session_is_exited_and_reported(monkeypatch) -> None:
    stub = _StubOrch(position=object(), result=RESULT)
    _with(monkeypatch, stub, session_open=True)
    out = asyncio.run(_kill_exit_now())
    assert stub.calls == 1
    assert out == {"trade_id": 12, "ledger": "paper", "closed": True, "exit_reason": "DAY_KILL"}


def test_no_covering_switch_reports_nothing(monkeypatch) -> None:
    stub = _StubOrch(position=object(), result=None)
    _with(monkeypatch, stub, session_open=True)
    assert asyncio.run(_kill_exit_now()) is None and stub.calls == 1


def test_no_position_or_outside_session_does_not_call_the_engine(monkeypatch) -> None:
    flat = _StubOrch(position=None, result=RESULT)
    _with(monkeypatch, flat, session_open=True)
    assert asyncio.run(_kill_exit_now()) is None and flat.calls == 0

    closed_mkt = _StubOrch(position=object(), result=RESULT)
    _with(monkeypatch, closed_mkt, session_open=False)
    assert asyncio.run(_kill_exit_now()) is None and closed_mkt.calls == 0

    _with(monkeypatch, None, session_open=True)          # no orchestrator (replay box)
    assert asyncio.run(_kill_exit_now()) is None


def test_an_exit_failure_never_fails_the_save(monkeypatch) -> None:
    stub = _StubOrch(position=object(), result=None, boom=True)
    _with(monkeypatch, stub, session_open=True)
    assert asyncio.run(_kill_exit_now()) is None and stub.calls == 1
