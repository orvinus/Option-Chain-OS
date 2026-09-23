"""Kill switches = stop trading AND flatten.

User rule (2026-09-23, extending the 2026-09-17 Master Kill decision to
every switch): a kill switched ON while a trade runs exits the trade; while
it stays ON no new trade is taken; switched OFF, trading resumes.

* Master, day and zone kills square off the open position at market on the
  next evaluated minute, in PAPER and on the LIVE route (simulated broker).
* ``kill_square_off`` (called by config Save/Restore) exits it immediately.
* A day kill is TODAY's switch, so it also exits a trade carried overnight;
  a zone kill covers only the trade opened in that zone.
* End-Exit stays inert while overnight carry is ON (locked decision kept).

Run:  cd backend && PYTHONPATH=. python tests/test_master_kill_squareoff.py
"""
from __future__ import annotations

import asyncio
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


def test_day_kill_and_zone_kill_close_the_open_trade() -> None:
    """The rule the user set on 2026-09-23 (was: they only blocked entries)."""
    for scope, reason in (("day", "DAY_KILL"), ("zone", "ZONE_KILL")):
        for live in (False, True):
            fake, orch = _open_trade(live=live)
            if scope == "day":
                fake.config.days["monday"].day_kill = True
            else:
                fake.config.days["monday"].zones["Z1"].zone_kill = True
            fake.bars = [FLAT_BAR]
            run(orch, NEXT)
            assert orch.position is None, f"{scope} kill must flatten (live={live})"
            assert fake.exit_calls == 1
            assert fake.closed_trades[-1]["exit_reason"] == reason


def test_zone_kill_on_another_zone_leaves_the_trade_alone() -> None:
    fake, orch = _open_trade()                      # opened in Monday Z1
    fake.config.days["monday"].zones["Z2"].zone_kill = True
    fake.bars = [FLAT_BAR]
    run(orch, NEXT)
    assert orch.position is not None and fake.exit_calls == 0


def test_todays_day_kill_exits_a_trade_carried_overnight() -> None:
    fake, orch = _open_trade()                      # opened Monday, carry ON
    assert fake.config.global_.overnight_carry is True
    tuesday = datetime(2026, 8, 18, 9, 31)
    fake.config.days["tuesday"].day_kill = True
    fake.bars = [MinuteBar(ts=tuesday, o=100.6, h=100.8, l=100.4, c=100.7)]
    run(orch, tuesday)
    assert orch.position is None
    assert fake.closed_trades[-1]["exit_reason"] == "DAY_KILL"


def test_kill_blocks_entries_while_on_and_trading_resumes_when_off() -> None:
    for scope in ("master", "day", "zone"):
        fake = Fake()
        orch = ZoneOrchestrator(fake.deps())
        if scope == "master":
            fake.config.global_.master_kill = True
        elif scope == "day":
            fake.config.days["monday"].day_kill = True
        else:
            fake.config.days["monday"].zones["Z1"].zone_kill = True
        fake.bars = [MinuteBar(ts=MONDAY, o=101.2, h=101.3, l=99.8, c=100.6)]   # an R1 bar
        run(orch, MONDAY)
        assert orch.position is None and fake.entry_calls == 0, f"{scope} kill ON must block"
        # Switched OFF: the SAME orchestrator trades again on the next pass.
        fake.config.global_.master_kill = False
        fake.config.days["monday"].day_kill = False
        fake.config.days["monday"].zones["Z1"].zone_kill = False
        run(orch, MONDAY)
        assert orch.position is not None and fake.entry_calls == 1, f"{scope} kill OFF must resume"


def test_kill_square_off_exits_immediately_on_save() -> None:
    """What config Save/Restore calls: no waiting for the next minute."""
    for scope, reason in (("master", "MASTER_KILL"), ("day", "DAY_KILL"), ("zone", "ZONE_KILL")):
        fake, orch = _open_trade()
        if scope == "master":
            fake.config.global_.master_kill = True
        elif scope == "day":
            fake.config.days["monday"].day_kill = True
        else:
            fake.config.days["monday"].zones["Z1"].zone_kill = True
        fake.bars = [FLAT_BAR]
        result = asyncio.run(orch.kill_square_off(NEXT))
        assert result is not None and result["closed"] is True
        assert result["exit_reason"] == reason
        assert orch.position is None and fake.closed_trades[-1]["exit_reason"] == reason


def test_kill_square_off_does_nothing_without_a_covering_switch() -> None:
    fake, orch = _open_trade()
    fake.config.days["monday"].zones["Z2"].zone_kill = True   # not the trade's zone
    assert asyncio.run(orch.kill_square_off(NEXT)) is None
    assert orch.position is not None and fake.exit_calls == 0
    idle = ZoneOrchestrator(Fake().deps())                     # no position at all
    assert asyncio.run(idle.kill_square_off(NEXT)) is None


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
