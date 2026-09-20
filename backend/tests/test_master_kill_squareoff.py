"""Master kill = stop trading AND flatten (user decision 2026-09-17).

* Master kill with an open position squares it off at market on the next
  evaluated minute, in PAPER and on the LIVE route (simulated broker), and
  blocks new entries.
* Day kill and zone kill deliberately do NOT close an open trade.
* End-Exit stays inert while overnight carry is ON (locked decision kept).

Run:  cd backend && PYTHONPATH=. python tests/test_master_kill_squareoff.py
"""
from __future__ import annotations

from datetime import datetime

from tests.test_algo_orchestrator import (
    MONDAY,
    Fake,
    MinuteBar,
    ZoneOrchestrator,
    enter_position,
    run,
)

NEXT = datetime(2026, 8, 17, 9, 31)
FLAT_BAR = MinuteBar(ts=NEXT, o=100.6, h=100.8, l=100.4, c=100.7)   # no engine exit


def _open_trade(live: bool = False) -> tuple[Fake, ZoneOrchestrator]:
    fake = Fake()
    fake.live_broker = live
    if live:
        fake.config.global_.paper.paper_mode = False
    orch = ZoneOrchestrator(fake.deps())
    enter_position(fake, orch)
    assert orch.position is not None
    return fake, orch


def test_master_kill_squares_off_the_open_paper_position() -> None:
    fake, orch = _open_trade()
    fake.config.global_.master_kill = True
    fake.bars = [FLAT_BAR]
    run(orch, NEXT)
    assert orch.position is None, "master kill must close the open position"
    assert fake.exit_calls == 1
    assert fake.closed_trades[-1]["exit_reason"] == "MASTER_KILL"
    assert orch.status.state == "killed"
    assert any("master kill" in b for b in orch.status.gate_blocks)


def test_master_kill_squares_off_on_the_live_route() -> None:
    """Same path with paper mode OFF — the exit goes through execute_exit,
    which is the broker adapter in production (simulated here)."""
    fake, orch = _open_trade(live=True)
    assert fake.inserted[-1]["ledger"] == "live"
    fake.config.global_.master_kill = True
    fake.bars = [FLAT_BAR]
    run(orch, NEXT)
    assert orch.position is None and fake.exit_calls == 1
    assert fake.closed_trades[-1]["exit_reason"] == "MASTER_KILL"


def test_master_kill_still_blocks_new_entries() -> None:
    fake = Fake()
    fake.config.global_.master_kill = True
    orch = ZoneOrchestrator(fake.deps())
    fake.bars = [MinuteBar(ts=MONDAY, o=101.2, h=101.3, l=99.8, c=100.6)]   # an R1 bar
    run(orch, MONDAY)
    assert orch.position is None and fake.entry_calls == 0
    assert orch.hunt is None and orch.status.state == "killed"


def test_day_kill_and_zone_kill_do_not_close_an_open_trade() -> None:
    for scope in ("day", "zone"):
        fake, orch = _open_trade()
        if scope == "day":
            fake.config.days["monday"].day_kill = True
        else:
            fake.config.days["monday"].zones["Z1"].zone_kill = True
        fake.bars = [FLAT_BAR]
        run(orch, NEXT)
        assert orch.position is not None, f"{scope} kill must not flatten"
        assert fake.exit_calls == 0


def test_master_kill_close_survives_a_missing_bar() -> None:
    """No premium bar this minute (feed gap): the engine's last close is used,
    exactly like the expiry force-close."""
    fake, orch = _open_trade()
    fake.config.global_.master_kill = True
    fake.bars = []
    run(orch, NEXT)
    assert orch.position is None and fake.closed_trades[-1]["exit_reason"] == "MASTER_KILL"


def test_end_exit_stays_inert_while_overnight_carry_is_on() -> None:
    """Locked decision kept: carry wins; the UI now says the switch is ignored."""
    fake, orch = _open_trade()
    fake.config.global_.overnight_carry = True
    fake.config.days["monday"].end_exit_enabled = True
    fake.bars = [MinuteBar(ts=datetime(2026, 8, 17, 15, 25), o=100.0, h=100.2, l=99.8, c=100.1)]
    run(orch, datetime(2026, 8, 17, 15, 26))
    assert orch.position is not None and fake.exit_calls == 0


def _run_all() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run_all()
    print("\nall master-kill square-off tests passed")
