"""Zone orchestrator — decision-path tests with fully injected providers.

Run:  cd backend && PYTHONPATH=. python tests/test_algo_orchestrator.py

No database, no feed: every provider is a fake, the Ultra Master Pro engine
instances are real (with injected levels, as in the M4 suite), and the config
is the Appendix-A seed mutated per scenario. Monday 2026-08-17 09:30 falls
inside seeded Z1 (09:20–10:30, premium band 75–125, max 2 trades, Z1 UMP tier
max SL 8%).
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Any, Optional

from app.algo.config_models import AlgoConfig, default_config
from app.algo.engines.ump.engine import UmpEngine
from app.algo.engines.ump.levels import LEVEL_TYPE_NAMES, Level
from app.algo.orchestrator import Contract, MinuteBar, OrchestratorDeps, ZoneOrchestrator
from app.algo.paper import buy_fill, sell_fill

MONDAY = datetime(2026, 8, 17, 9, 30)   # inside seeded Monday Z1
TODAY = MONDAY.date()


class Fake:
    """Mutable provider world for one scenario."""

    def __init__(self) -> None:
        self.config: AlgoConfig = default_config(today=date(2026, 8, 10))
        self.readings: dict[str, str] = {
            "oi_change": "CALL", "multi_tf": "CALL", "ratio": "CALL"
        }
        self.indicator_calls = 0
        self.closed: list[tuple[str, float]] = []
        # Multi-strike: the selector returns this list verbatim (already
        # band-filtered + trimmed to strike_scan_count, as the real selector
        # does). One candidate = the original single-strike behavior.
        self.strikes: list[tuple[Contract, float]] = [
            (Contract("NIFTY", date(2026, 8, 20), 24500, "CE"), 101.0)
        ]
        self.bars: list[MinuteBar] = []
        # Per-strike bar queues for multi-strike scenarios; when a strike has
        # a queue here it wins over the shared self.bars list.
        self.bars_by_strike: dict[int, list[MinuteBar]] = {}
        self.engine_levels: list[tuple[float, int]] = [(100.0, 1), (130.0, 1)]
        self.inserted: list[dict[str, Any]] = []
        self.closed_trades: list[dict[str, Any]] = []
        self.signals: list[dict[str, Any]] = []
        self.notifications: list[str] = []
        self.balance = 30000.0
        self._next_id = 1
        self.live_broker = False       # execute_entry returns a broker order id
        self.reconcile_result: Optional[tuple[Optional[bool], str]] = None
        self.reconcile_calls = 0
        self.lot = 75
        self.platform_holiday = False
        self.audits: list[tuple[str, str, dict]] = []
        self.entry_calls = 0
        self.exit_calls = 0
        self.entry_raises = False       # transport-style unknown-state failure
        self.exit_result_none = False   # live exit refused → retry semantics
        self.closed_raises = False      # ledger read outage
        # Overnight-carry extras — wired into deps ONLY when set, so every
        # pre-carry test keeps its exact dep surface:
        self.open_latest: Optional[Any] = None    # OpenTrade for adoption
        self.entered: Optional[list[str]] = None  # today_entries zone_ids
        self.data_age: Optional[float] = None     # activates the stall alarm

    def deps(self) -> OrchestratorDeps:
        async def get_config() -> AlgoConfig:
            return self.config

        async def evaluate_indicator(ind, day, zone_id, zone_cfg, symbol) -> str:
            self.indicator_calls += 1
            return self.readings.get(ind, "NO_TRADE")

        async def select_strikes(symbol, side, zone_cfg):
            return list(self.strikes)

        async def build_engine(contract, zone_cfg, entries_live):
            lvls = [
                Level(price=p, type=t, name=LEVEL_TYPE_NAMES[t])
                for p, t in self.engine_levels
            ]
            return UmpEngine(zone_cfg.ump, level_provider=lambda: list(lvls))

        async def latest_minute(contract, now):
            q = self.bars_by_strike.get(contract.strike)
            if q is not None:
                return q.pop(0) if q else None
            return self.bars.pop(0) if self.bars else None

        def lot_size(symbol) -> int:
            return self.lot

        async def current_balance(cfg) -> float:
            return self.balance

        async def today_closed(trade_date, ledger):
            if self.closed_raises:
                raise RuntimeError("timescaledb: connection refused")
            return list(self.closed)

        async def insert_trade(**kw) -> int:
            kw["id"] = self._next_id
            self._next_id += 1
            self.inserted.append(kw)
            return kw["id"]

        async def close_trade(trade_id, **kw) -> None:
            kw["id"] = trade_id
            self.closed_trades.append(kw)

        async def record_signal(**kw) -> bool:
            self.signals.append(kw)
            return True

        def notify(key: str, msg: str) -> None:
            self.notifications.append(msg)

        async def execute_entry(cfg, **kw):
            from app.algo.broker.execution import ExecutionResult
            from app.algo.paper import buy_fill

            self.entry_calls += 1
            if self.entry_raises:
                raise RuntimeError("transport failure: ReadTimeout")
            return ExecutionResult(
                fill_price=buy_fill(cfg.global_.paper, kw["raw_price"]).price,
                broker_order_id="B1" if self.live_broker else "",
                note="paper",
            )

        async def execute_exit(cfg, **kw):
            from app.algo.broker.execution import ExecutionResult
            from app.algo.paper import sell_fill

            self.exit_calls += 1
            if self.exit_result_none:
                return None
            return ExecutionResult(
                fill_price=sell_fill(cfg.global_.paper, kw["raw_price"]).price,
                note="paper",
            )

        async def reconcile_live(**kw):
            self.reconcile_calls += 1
            assert self.reconcile_result is not None
            return self.reconcile_result

        def audit_event(event_type: str, detail: str, extra: dict) -> None:
            self.audits.append((event_type, detail, extra))

        async def open_trade_latest(ledger):
            row = self.open_latest
            return row if (row is not None and row.ledger == ledger) else None

        async def today_entries(trade_date, ledger):
            return list(self.entered or [])

        async def data_age_s(symbol):
            return self.data_age

        return OrchestratorDeps(
            get_config=get_config,
            evaluate_indicator=evaluate_indicator,
            select_strikes=select_strikes,
            build_engine=build_engine,
            latest_minute=latest_minute,
            lot_size=lot_size,
            current_balance=current_balance,
            today_closed=today_closed,
            insert_trade=insert_trade,
            close_trade=close_trade,
            record_signal=record_signal,
            notify=notify,
            execute_entry=execute_entry,
            execute_exit=execute_exit,
            reconcile_live=reconcile_live if self.reconcile_result is not None else None,
            open_trade_latest=open_trade_latest if self.open_latest is not None else None,
            today_entries=today_entries if self.entered is not None else None,
            data_age_s=data_age_s if self.data_age is not None else None,
            is_platform_holiday=lambda d: self.platform_holiday,
            audit_event=audit_event,
        )


def run(orch: ZoneOrchestrator, now: datetime) -> None:
    asyncio.run(orch.evaluate_minute(now))


R1_BAR = MinuteBar(ts=MONDAY, o=101.2, h=101.3, l=99.8, c=100.6)  # enters R1 @ UM


def enter_position(fake: Fake, orch: ZoneOrchestrator) -> None:
    fake.bars = [R1_BAR]
    run(orch, MONDAY)
    assert orch.position is not None, "scenario setup: entry expected"


# ── unanimous-among-enabled rule ──────────────────────────────────────────

def test_unanimous_all_three_agree_hunts():
    fake = Fake()
    orch = ZoneOrchestrator(fake.deps())
    fake.bars = [MinuteBar(ts=MONDAY, o=99.0, h=99.2, l=98.9, c=99.1)]  # neutral bar
    run(orch, MONDAY)
    assert orch.status.direction == "CALL"
    assert orch.status.state == "hunting" and orch.hunt is not None


def test_disagreement_means_no_trade():
    fake = Fake()
    fake.readings["ratio"] = "PUT"
    orch = ZoneOrchestrator(fake.deps())
    run(orch, MONDAY)
    assert orch.status.direction == "NO_TRADE"
    assert orch.status.state == "no_trade" and orch.hunt is None


def test_single_enabled_indicator_decides_directly():
    fake = Fake()
    fake.config.days["monday"].zones["Z1"].enabled_indicators = ["ratio"]
    fake.readings = {"ratio": "PUT"}
    orch = ZoneOrchestrator(fake.deps())
    run(orch, MONDAY)
    assert orch.status.direction == "PUT"
    assert orch.hunt is not None and orch.hunt.side == "PUT"


def test_zero_indicators_is_gated_not_silent():
    fake = Fake()
    fake.config.days["monday"].zones["Z1"].enabled_indicators = []
    orch = ZoneOrchestrator(fake.deps())
    run(orch, MONDAY)
    assert orch.status.state == "gated"
    assert any("incomplete" in b for b in orch.status.gate_blocks)
    assert any("blocked from trading" in n for n in fake.notifications)


# ── kill switches / holiday / clock ───────────────────────────────────────

def test_master_kill_blocks_everything():
    fake = Fake()
    fake.config.global_.master_kill = True
    orch = ZoneOrchestrator(fake.deps())
    run(orch, MONDAY)
    assert orch.status.state == "killed"
    assert fake.indicator_calls == 0


def test_holiday_auto_skips_the_day():
    fake = Fake()
    fake.config.global_.holidays.append(
        type(fake.config.global_.holidays[0] if fake.config.global_.holidays else None)
        if False else __import__("app.algo.config_models", fromlist=["HolidayEntry"]).HolidayEntry(
            date=TODAY.isoformat(), occasion="test holiday"
        )
    )
    orch = ZoneOrchestrator(fake.deps())
    run(orch, MONDAY)
    assert orch.status.state == "killed" and fake.indicator_calls == 0


def test_day_and_zone_kills():
    fake = Fake()
    fake.config.days["monday"].day_kill = True
    orch = ZoneOrchestrator(fake.deps())
    run(orch, MONDAY)
    assert orch.status.state == "killed"

    fake2 = Fake()
    fake2.config.days["monday"].zones["Z1"].zone_kill = True
    orch2 = ZoneOrchestrator(fake2.deps())
    run(orch2, MONDAY)
    assert orch2.status.state == "gated"
    assert any("zone kill" in b for b in orch2.status.gate_blocks)


def test_outside_all_zones_is_idle():
    fake = Fake()
    orch = ZoneOrchestrator(fake.deps())
    run(orch, datetime(2026, 8, 17, 11, 0))   # between Z1 (ends 10:30) and Z2 (11:45)
    assert orch.status.state == "idle" and fake.indicator_calls == 0


def test_max_trades_gate():
    fake = Fake()
    fake.closed = [("Z1", 200.0), ("Z1", -100.0)]   # seeded Z1 max_trades = 2
    orch = ZoneOrchestrator(fake.deps())
    run(orch, MONDAY)
    assert orch.status.state == "gated"
    assert any("max trades" in b for b in orch.status.gate_blocks)


# ── strike selection / sizing / entry ─────────────────────────────────────

def test_no_strike_in_band_blocks_and_alerts():
    fake = Fake()
    fake.strikes = []
    orch = ZoneOrchestrator(fake.deps())
    run(orch, MONDAY)
    assert orch.status.state == "no_strike_in_band"
    assert any("no CALL strike" in n for n in fake.notifications)


def test_entry_sizing_fill_and_ledger_row():
    fake = Fake()
    orch = ZoneOrchestrator(fake.deps())
    enter_position(fake, orch)
    row = fake.inserted[-1]
    # Monday Z1's UMP tier has zone width 2% → R1 enters at UM = 101.0.
    # Allocation 50% of 30000 = 15000 → floor(15000 / (101 × 75)) = 1 lot;
    # the buy fill carries +0.5% slippage.
    expected_fill = buy_fill(fake.config.global_.paper, 101.0).price
    assert row["lots"] == 1
    assert abs(row["entry_price"] - expected_fill) < 0.02
    assert row["ledger"] == "paper" and row["side"] == "CALL"
    assert row["sub_scenario"] == "R1" and row["zone_id"] == "Z1"
    assert any("ENTRY CALL" in n for n in fake.notifications)


def test_lots_zero_skips_entry_and_alerts():
    fake = Fake()
    fake.balance = 5000.0   # 50% = 2500 < one lot at ~101.5 × 75
    orch = ZoneOrchestrator(fake.deps())
    fake.bars = [R1_BAR]
    run(orch, MONDAY)
    assert orch.position is None and not fake.inserted
    assert any("cannot buy" in n for n in fake.notifications)


def test_band_guard_suppresses_out_of_band_entry():
    fake = Fake()
    fake.config.days["monday"].zones["Z1"].premium_min = 105.0  # band above premium
    fake.config.days["monday"].zones["Z1"].premium_max = 125.0
    orch = ZoneOrchestrator(fake.deps())
    fake.bars = [R1_BAR]                       # close 100.6 < band min 105
    run(orch, MONDAY)
    assert orch.position is None and not fake.inserted, (
        "the engine's entry must be suppressed while the premium is out of band"
    )


def test_direction_flip_discards_hunt():
    fake = Fake()
    orch = ZoneOrchestrator(fake.deps())
    fake.bars = [MinuteBar(ts=MONDAY, o=99.0, h=99.2, l=98.9, c=99.1)]
    run(orch, MONDAY)
    first_engine = orch.hunt.engine
    fake.readings = {"oi_change": "PUT", "multi_tf": "PUT", "ratio": "PUT"}
    fake.strikes = [(Contract("NIFTY", date(2026, 8, 20), 24500, "PE"), 99.0)]
    fake.bars = [MinuteBar(ts=MONDAY, o=99.0, h=99.2, l=98.9, c=99.1)]
    run(orch, datetime(2026, 8, 17, 9, 31))
    assert orch.hunt is not None and orch.hunt.engine is not first_engine
    assert orch.hunt.side == "PUT"


def test_no_trade_discards_hunt_entirely():
    fake = Fake()
    orch = ZoneOrchestrator(fake.deps())
    fake.bars = [MinuteBar(ts=MONDAY, o=99.0, h=99.2, l=98.9, c=99.1)]
    run(orch, MONDAY)
    assert orch.hunt is not None
    fake.readings["multi_tf"] = "NO_TRADE"
    run(orch, datetime(2026, 8, 17, 9, 31))
    assert orch.hunt is None and orch.status.state == "no_trade"


# ── in-trade management / exits / sequencing ──────────────────────────────

def test_open_trade_suppresses_signal_evaluation():
    fake = Fake()
    orch = ZoneOrchestrator(fake.deps())
    enter_position(fake, orch)
    calls_before = fake.indicator_calls
    fake.bars = [MinuteBar(ts=datetime(2026, 8, 17, 9, 31), o=100.6, h=100.8, l=100.4, c=100.7)]
    run(orch, datetime(2026, 8, 17, 9, 32))
    assert orch.position is not None
    assert fake.indicator_calls == calls_before, (
        "§6: once in a trade the filter layer is never consulted"
    )


def test_exit_closes_ledger_with_fees_and_alert():
    fake = Fake()
    orch = ZoneOrchestrator(fake.deps())
    enter_position(fake, orch)
    # Max SL for Z1 tier (8%) on the R1 entry at UM = 101.0 → 92.92; drive
    # the low through it.
    fake.bars = [MinuteBar(ts=datetime(2026, 8, 17, 9, 31), o=100.0, h=100.1, l=90.0, c=92.0)]
    run(orch, datetime(2026, 8, 17, 9, 32))
    assert orch.position is None
    closed = fake.closed_trades[-1]
    assert closed["exit_reason"] == "MAX_SL"
    raw_exit = 101.0 * 0.92
    expected = sell_fill(fake.config.global_.paper, raw_exit).price
    assert abs(closed["exit_price"] - expected) < 0.02
    assert closed["pnl_rupees"] < 0
    assert closed["fees"]["total"] > 0
    assert any("EXIT CALL" in n for n in fake.notifications)


def test_end_exit_fires_on_last_zone_end_only_when_enabled():
    fake = Fake()
    fake.config.global_.overnight_carry = False   # carry makes End-Exit inert
    orch = ZoneOrchestrator(fake.deps())
    enter_position(fake, orch)
    # 15:26 — past Monday Z3's end (15:25); trade still open, End-Exit ON.
    fake.bars = [MinuteBar(ts=datetime(2026, 8, 17, 15, 25), o=100.0, h=100.2, l=99.8, c=100.1)]
    run(orch, datetime(2026, 8, 17, 15, 26))
    assert orch.position is None
    assert fake.closed_trades[-1]["exit_reason"] == "ZONE_END_EXIT"


# ── overnight carry (Pine parity — locked user decision 2026-08-19) ──────

def test_end_exit_inert_while_overnight_carry_on():
    fake = Fake()
    assert fake.config.global_.overnight_carry is True, "carry ships ON"
    orch = ZoneOrchestrator(fake.deps())
    enter_position(fake, orch)
    fake.bars = [MinuteBar(ts=datetime(2026, 8, 17, 15, 25), o=100.0, h=100.2, l=99.8, c=100.1)]
    run(orch, datetime(2026, 8, 17, 15, 26))
    assert orch.position is not None, (
        "§2.3 override: End-Exit is IGNORED under carry — only the Pine "
        "ladder (or the expiry-day close) may exit"
    )
    assert fake.closed_trades == []


def test_expiry_force_close_at_1525_unconditional():
    fake = Fake()
    # The hunted contract expires TODAY (Monday 2026-08-17).
    fake.strikes = [(Contract("NIFTY", date(2026, 8, 17), 24500, "CE"), 101.0)]
    orch = ZoneOrchestrator(fake.deps())
    enter_position(fake, orch)
    # 15:24 — no exit yet (carry ON, End-Exit inert, engine holds).
    fake.bars = [MinuteBar(ts=datetime(2026, 8, 17, 15, 23), o=100.0, h=100.2, l=99.8, c=100.1)]
    run(orch, datetime(2026, 8, 17, 15, 24))
    assert orch.position is not None
    # 15:25 — the unconditional expiry-day force-close.
    fake.bars = [MinuteBar(ts=datetime(2026, 8, 17, 15, 24), o=100.1, h=100.3, l=99.9, c=100.2)]
    run(orch, datetime(2026, 8, 17, 15, 25))
    assert orch.position is None
    assert fake.closed_trades[-1]["exit_reason"] == "EXPIRY_FORCE_CLOSE"
    assert any(a[0] == "runtime_expiry_force_close" for a in fake.audits)


def test_engine_exit_same_minute_outranks_expiry_close():
    fake = Fake()
    fake.strikes = [(Contract("NIFTY", date(2026, 8, 17), 24500, "CE"), 101.0)]
    orch = ZoneOrchestrator(fake.deps())
    enter_position(fake, orch)
    # 15:25 bar smashes through the Max SL (90) — the Pine exit wins the minute.
    fake.bars = [MinuteBar(ts=datetime(2026, 8, 17, 15, 24), o=95.0, h=95.1, l=88.0, c=89.0)]
    run(orch, datetime(2026, 8, 17, 15, 25))
    assert orch.position is None
    assert fake.closed_trades[-1]["exit_reason"] == "MAX_SL"


def test_overnight_adoption_of_a_carried_position():
    from app.algo.trade_store import OpenTrade

    fake = Fake()
    fake.open_latest = OpenTrade(
        id=7, trade_date=date(2026, 8, 14), day="friday", zone_id="Z2",
        index_symbol="NIFTY", side="CALL", token="td:x", strike=24500,
        expiry=date(2026, 8, 20), entry_ts=datetime(2026, 8, 14, 14, 3),
        entry_price=101.0, lots=2, ledger="paper", sub_scenario="S1A",
    )
    orch = ZoneOrchestrator(fake.deps())
    asyncio.run(orch.adopt_open_trade(today=date(2026, 8, 17)))
    assert orch.position is not None and orch.position.trade_id == 7
    assert any("OVERNIGHT RE-ADOPTION" in n for n in fake.notifications)


def test_adoption_with_carry_off_pages_critical():
    from app.algo.trade_store import OpenTrade

    fake = Fake()
    fake.config.global_.overnight_carry = False
    fake.open_latest = OpenTrade(
        id=8, trade_date=date(2026, 8, 14), day="friday", zone_id="Z2",
        index_symbol="NIFTY", side="CALL", token="td:x", strike=24500,
        expiry=date(2026, 8, 20), entry_ts=datetime(2026, 8, 14, 14, 3),
        entry_price=101.0, lots=2, ledger="paper", sub_scenario="S1A",
    )
    orch = ZoneOrchestrator(fake.deps())
    asyncio.run(orch.adopt_open_trade(today=date(2026, 8, 17)))
    assert orch.position is not None, "never abandon it — adopt AND page"
    assert any("overnight carry OFF" in n for n in fake.notifications)


def test_max_trades_counts_entry_day_not_exit_day():
    fake = Fake()
    # A carried trade CLOSED today in Z1 (exit-day list) but ENTERED Friday —
    # today's Z1 allowance (max_trades=1) must still be free.
    fake.closed = [("Z1", -500.0)]
    fake.entered = []                       # nothing entered TODAY
    fake.config.days["monday"].zones["Z1"].max_trades = 1
    orch = ZoneOrchestrator(fake.deps())
    enter_position(fake, orch)              # Monday 09:30, zone Z1
    assert orch.position is not None, (
        "exit-day P&L must not consume the entry-day max_trades allowance"
    )


def test_holiday_with_carried_position_never_stalls_critical():
    fake = Fake()
    fake.data_age = 30.0                    # stall alarm armed (live runtime)
    orch = ZoneOrchestrator(fake.deps())
    enter_position(fake, orch)
    fake.platform_holiday = True            # next day is a market holiday
    fake.bars = []                          # no bars — by definition
    for minute in range(3, 7):              # > _POSITION_STALL_ALERT_MIN
        run(orch, datetime(2026, 8, 18, 9, 30 + minute))
    assert not any("no market data for the OPEN position" in n for n in fake.notifications), (
        "a holiday session has no bars by definition — no stall CRITICAL"
    )


# ── M7: Shadow Mode + broker reconciliation ───────────────────────────────

def test_shadow_mode_mirrors_live_trade_into_paper_ledger():
    fake = Fake()
    fake.config.global_.paper.paper_mode = False
    fake.config.global_.paper.shadow_mode = True
    fake.config.global_.demat_balance = 30000.0
    fake.live_broker = True
    orch = ZoneOrchestrator(fake.deps())
    enter_position(fake, orch)
    assert [t["ledger"] for t in fake.inserted] == ["live", "paper"], (
        "§8 Shadow Mode: a live entry must insert its paper twin"
    )
    live_row, shadow_row = fake.inserted
    assert shadow_row["strike"] == live_row["strike"]
    assert shadow_row["lots"] == live_row["lots"]
    expected_fill = buy_fill(fake.config.global_.paper, 101.0).price
    assert abs(shadow_row["entry_price"] - expected_fill) < 0.02

    # The exit closes BOTH rows with the same reason.
    fake.bars = [MinuteBar(ts=datetime(2026, 8, 17, 9, 31), o=100.0, h=100.1, l=90.0, c=92.0)]
    run(orch, datetime(2026, 8, 17, 9, 32))
    assert orch.position is None
    reasons = {c["id"]: c["exit_reason"] for c in fake.closed_trades}
    assert reasons == {live_row["id"]: "MAX_SL", shadow_row["id"]: "MAX_SL"}


def test_shadow_mode_inert_while_paper_mode_on():
    fake = Fake()
    fake.config.global_.paper.shadow_mode = True   # paper_mode stays ON
    orch = ZoneOrchestrator(fake.deps())
    enter_position(fake, orch)
    assert [t["ledger"] for t in fake.inserted] == ["paper"], (
        "shadow mirrors LIVE trades only — paper trades have no twin"
    )


def test_reconcile_mismatch_fires_critical_alert_once():
    fake = Fake()
    fake.config.global_.paper.paper_mode = False
    fake.config.global_.demat_balance = 30000.0
    fake.live_broker = True
    fake.reconcile_result = (False, "broker holds 0, engine expects 150")
    orch = ZoneOrchestrator(fake.deps())
    enter_position(fake, orch)
    # Managed minutes on :35 and :40 — the %5 boundary runs the audit each
    # time, but the CRITICAL alert fires once per trade.
    fake.bars = [MinuteBar(ts=datetime(2026, 8, 17, 9, 34), o=100.6, h=100.8, l=100.4, c=100.7)]
    run(orch, datetime(2026, 8, 17, 9, 35))
    assert fake.reconcile_calls == 1
    assert any("broker position differs" in n for n in fake.notifications)
    fake.bars = [MinuteBar(ts=datetime(2026, 8, 17, 9, 39), o=100.6, h=100.8, l=100.4, c=100.7)]
    run(orch, datetime(2026, 8, 17, 9, 40))
    assert fake.reconcile_calls == 2
    assert len([n for n in fake.notifications if "broker position differs" in n]) == 1


def test_reconcile_skipped_for_paper_positions():
    fake = Fake()
    fake.reconcile_result = (False, "must never be consulted")
    orch = ZoneOrchestrator(fake.deps())
    enter_position(fake, orch)     # paper mode → no broker_order_id
    fake.bars = [MinuteBar(ts=datetime(2026, 8, 17, 9, 34), o=100.6, h=100.8, l=100.4, c=100.7)]
    run(orch, datetime(2026, 8, 17, 9, 35))
    assert fake.reconcile_calls == 0


def test_end_exit_disabled_keeps_trade_open():
    fake = Fake()
    fake.config.days["monday"].end_exit_enabled = False
    orch = ZoneOrchestrator(fake.deps())
    enter_position(fake, orch)
    fake.bars = [MinuteBar(ts=datetime(2026, 8, 17, 15, 25), o=100.0, h=100.2, l=99.8, c=100.1)]
    run(orch, datetime(2026, 8, 17, 15, 26))
    assert orch.position is not None, "End-Exit OFF → the open trade keeps running"


# ── risk counters ─────────────────────────────────────────────────────────

def test_daily_loss_cap_auto_kills_entries():
    fake = Fake()
    fake.closed = [("Z1", -3200.0)]   # cap: 20% of 15000 = 3000
    orch = ZoneOrchestrator(fake.deps())
    run(orch, MONDAY)
    assert orch.status.state == "gated"
    assert any("max daily loss" in b for b in orch.status.gate_blocks)
    assert any("Max daily loss breached" in n for n in fake.notifications)
    assert fake.indicator_calls == 0


def test_profit_lock_stops_new_entries():
    fake = Fake()
    fake.closed = [("Z1", 4600.0)]    # lock: 30% of 15000 = 4500
    orch = ZoneOrchestrator(fake.deps())
    run(orch, MONDAY)
    assert any("profit lock" in b for b in orch.status.gate_blocks)


def test_consecutive_losses_pause_and_manual_resume():
    fake = Fake()
    fake.closed = [("Z1", -500.0), ("Z1", -500.0), ("Z2", -500.0)]  # streak 3
    orch = ZoneOrchestrator(fake.deps())
    run(orch, MONDAY)
    assert orch.paused_reason and "consecutive losses" in orch.paused_reason
    assert any("auto-paused" in n for n in fake.notifications)
    orch.resume()
    assert orch.paused_reason == ""
    # Same ledger state still re-pauses (restart-safe recomputation).
    run(orch, datetime(2026, 8, 17, 9, 31))
    assert orch.paused_reason != ""


# ── cadence ───────────────────────────────────────────────────────────────

def test_zone_start_cadence_evaluates_once():
    fake = Fake()
    fake.config.days["monday"].zones["Z1"].reeval_cadence = "zone_start"
    orch = ZoneOrchestrator(fake.deps())
    fake.bars = [MinuteBar(ts=MONDAY, o=99.0, h=99.2, l=98.9, c=99.1)]
    run(orch, MONDAY)
    first = fake.indicator_calls
    assert first == 3
    fake.bars = [MinuteBar(ts=MONDAY, o=99.0, h=99.2, l=98.9, c=99.1)]
    run(orch, datetime(2026, 8, 17, 9, 31))
    assert fake.indicator_calls == first, "zone_start cadence caches the first reading"



# ── EXPERIMENTAL direction-hold (§5.3 relaxation, default OFF) ────────────

def _set_hold(fake: Fake, minutes: int) -> None:
    for d in fake.config.days.values():
        for z in d.zones.values():
            z.direction_hold_min = minutes


def test_direction_hold_zero_is_strict_spec() -> None:
    """Default 0: a NO_TRADE flicker discards the hunt instantly (regression
    lock on the pre-hold behavior)."""
    fake = Fake()
    orch = ZoneOrchestrator(fake.deps())
    run(orch, MONDAY)
    assert orch.hunt is not None
    fake.readings = {k: "NO_TRADE" for k in fake.readings}
    run(orch, MONDAY.replace(minute=31))
    assert orch.hunt is None, "hold=0 must keep §5.3's instant discard"
    assert orch.status.state == "no_trade"


def test_direction_hold_keeps_hunt_through_flicker() -> None:
    fake = Fake()
    _set_hold(fake, 15)
    orch = ZoneOrchestrator(fake.deps())
    run(orch, MONDAY)
    hunt = orch.hunt
    assert hunt is not None
    fake.readings = {k: "NO_TRADE" for k in fake.readings}
    run(orch, MONDAY.replace(minute=31))
    assert orch.hunt is hunt, "hunt must survive the NO_TRADE flicker"
    assert orch.status.state == "hunting_hold"
    # And the held hunt still ENTERS when the engine fires.
    fake.bars = [MinuteBar(ts=MONDAY.replace(minute=32), o=101.2, h=101.3, l=99.8, c=100.6)]
    run(orch, MONDAY.replace(minute=32))
    assert orch.position is not None, "a held hunt must still be able to enter"


def test_direction_hold_opposite_side_discards() -> None:
    fake = Fake()
    _set_hold(fake, 15)
    orch = ZoneOrchestrator(fake.deps())
    run(orch, MONDAY)
    assert orch.hunt is not None and orch.hunt.side == "CALL"
    fake.readings = {k: "PUT" for k in fake.readings}
    run(orch, MONDAY.replace(minute=31))
    assert orch.hunt is not None and orch.hunt.side == "PUT", (
        "an OPPOSITE unanimous side must discard the old hunt and start fresh"
    )


def test_direction_hold_expires() -> None:
    fake = Fake()
    _set_hold(fake, 5)
    orch = ZoneOrchestrator(fake.deps())
    run(orch, MONDAY)
    assert orch.hunt is not None
    fake.readings = {k: "NO_TRADE" for k in fake.readings}
    run(orch, MONDAY.replace(minute=33))
    assert orch.hunt is not None, "minute 3 of 5 — still held"
    run(orch, MONDAY.replace(minute=36))
    assert orch.hunt is None, "past the 5-minute hold the hunt must discard"
    assert orch.status.state == "no_trade"


# ── multi-strike hunting (Top-N, user decision 2026-08-18) ────────────────

NEUTRAL = dict(o=99.0, h=99.2, l=98.9, c=99.1)


def _three_candidates(fake: Fake) -> None:
    fake.strikes = [
        (Contract("NIFTY", date(2026, 8, 20), 24500, "CE"), 101.0),
        (Contract("NIFTY", date(2026, 8, 20), 24450, "CE"), 96.0),
        (Contract("NIFTY", date(2026, 8, 20), 24550, "CE"), 107.0),
    ]


def test_multi_strike_hunts_all_candidates():
    fake = Fake()
    _three_candidates(fake)
    for s, _ in fake.strikes:
        fake.bars_by_strike[s.strike] = [MinuteBar(ts=MONDAY, **NEUTRAL)]
    orch = ZoneOrchestrator(fake.deps())
    run(orch, MONDAY)
    assert len(orch.hunts) == 3, "every band candidate hunts in parallel"
    assert orch.status.state == "hunting"
    assert orch.status.hunting_strikes == 3
    assert orch.hunt is orch.hunts[0], "primary hunt = first (nearest band-mid)"


def test_multi_fire_first_priority_wins():
    fake = Fake()
    fake.strikes = [
        (Contract("NIFTY", date(2026, 8, 20), 24500, "CE"), 101.0),
        (Contract("NIFTY", date(2026, 8, 20), 24450, "CE"), 96.0),
    ]
    # BOTH engines get an R1-firing bar the same minute — the tie-break must
    # take list order (nearest band-mid, then lowest strike).
    fake.bars_by_strike[24500] = [R1_BAR]
    fake.bars_by_strike[24450] = [MinuteBar(ts=MONDAY, o=101.2, h=101.3, l=99.8, c=100.6)]
    orch = ZoneOrchestrator(fake.deps())
    run(orch, MONDAY)
    assert orch.position is not None
    assert len(fake.inserted) == 1, "exactly ONE order despite two fired engines"
    assert fake.inserted[0]["strike"] == 24500, "priority candidate wins the tie"
    assert orch.hunts == [], "all hunts discarded wholesale on entry"


def test_per_hunt_band_guard_blocks_only_its_engine():
    fake = Fake()
    # Band 102–125: candidate A's close (100.6) is OUT of band at fire time,
    # candidate B's close (103.0) is IN band — only B may enter, even though
    # A is the priority candidate.
    fake.config.days["monday"].zones["Z1"].premium_min = 102.0
    fake.config.days["monday"].zones["Z1"].premium_max = 125.0
    fake.strikes = [
        (Contract("NIFTY", date(2026, 8, 20), 24500, "CE"), 103.0),
        (Contract("NIFTY", date(2026, 8, 20), 24450, "CE"), 104.0),
    ]
    fake.bars_by_strike[24500] = [MinuteBar(ts=MONDAY, o=101.2, h=101.3, l=99.8, c=100.6)]
    fake.bars_by_strike[24450] = [MinuteBar(ts=MONDAY, o=103.2, h=103.4, l=99.8, c=103.0)]
    orch = ZoneOrchestrator(fake.deps())
    run(orch, MONDAY)
    assert orch.position is not None, (
        "the in-band engine must be able to enter — each guard checks ITS OWN "
        "engine's close, never the primary hunt's"
    )
    assert fake.inserted[0]["strike"] == 24450


def test_multi_strike_flip_discards_all_hunts():
    fake = Fake()
    _three_candidates(fake)
    for s, _ in fake.strikes:
        fake.bars_by_strike[s.strike] = [MinuteBar(ts=MONDAY, **NEUTRAL)]
    orch = ZoneOrchestrator(fake.deps())
    run(orch, MONDAY)
    old = list(orch.hunts)
    assert len(old) == 3
    fake.readings = {k: "PUT" for k in fake.readings}
    fake.strikes = [(Contract("NIFTY", date(2026, 8, 20), 24400, "PE"), 99.0)]
    run(orch, datetime(2026, 8, 17, 9, 31))
    assert len(orch.hunts) == 1 and orch.hunts[0].side == "PUT"
    assert all(h not in orch.hunts for h in old), "flip discards EVERY old hunt"


# ── §2.4 Strategy Active (observation without execution) ──────────────────

def test_strategy_active_off_locks_execution():
    fake = Fake()
    fake.config.days["monday"].zones["Z1"].strategy_active = False
    orch = ZoneOrchestrator(fake.deps())
    fake.bars = [R1_BAR]          # would enter if execution were allowed
    run(orch, MONDAY)
    assert orch.status.state == "strategy_inactive"
    assert orch.hunts == [] and orch.position is None and not fake.inserted
    assert fake.indicator_calls == 3, (
        "§2.4: the filter layer still evaluates (observation) — only the "
        "execution engine is locked"
    )
    assert any(s.get("indicator") == "combined" for s in fake.signals), (
        "the combined signal is still recorded for observation"
    )


# ── platform holiday union / kill alerting / audit rows ───────────────────

def test_platform_holiday_union_kills_day():
    fake = Fake()
    fake.platform_holiday = True   # NSE file knows it; the config list doesn't
    orch = ZoneOrchestrator(fake.deps())
    run(orch, MONDAY)
    assert orch.status.state == "killed"
    assert "holiday" in orch.status.gate_blocks
    assert fake.indicator_calls == 0


def test_kill_alert_gated_by_day_toggle():
    fake = Fake()
    fake.config.days["monday"].day_kill = True
    orch = ZoneOrchestrator(fake.deps())
    run(orch, MONDAY)
    assert any("Trading is OFF today" in n for n in fake.notifications)

    fake2 = Fake()
    fake2.config.days["monday"].day_kill = True
    fake2.config.days["monday"].alerts.telegram_trade_entry_exit = False
    orch2 = ZoneOrchestrator(fake2.deps())
    run(orch2, MONDAY)
    assert not any("Trading is OFF today" in n for n in fake2.notifications)


def test_lot_size_unknown_refuses_entry():
    fake = Fake()
    fake.lot = 0                   # registry could not resolve the symbol
    orch = ZoneOrchestrator(fake.deps())
    fake.bars = [R1_BAR]
    run(orch, MONDAY)
    assert orch.position is None and not fake.inserted
    assert any("lot size unknown" in n for n in fake.notifications)


# ── order-state safety (2026-08-18 adversarial-audit regressions) ─────────

def test_entry_transport_failure_pauses_never_reorders():
    """A raised (not refused) entry means the order MAY be live at the
    exchange. The engine must pause and page — never retry the BUY next
    minute (the repeat-order hazard)."""
    fake = Fake()
    fake.entry_raises = True
    orch = ZoneOrchestrator(fake.deps())
    fake.bars = [R1_BAR]
    run(orch, MONDAY)
    assert fake.entry_calls == 1
    assert orch.position is None and orch.hunts == []
    assert orch.paused_reason and "UNKNOWN" in orch.paused_reason
    assert any("UNKNOWN state" in n for n in fake.notifications)
    # Next minute: still unanimous CALL, bars ready — but the pause holds.
    fake.entry_raises = False
    fake.bars = [R1_BAR]
    run(orch, MONDAY.replace(minute=31))
    assert fake.entry_calls == 1, "no second order while paused"
    assert orch.status.state == "gated"


def test_failed_exit_retries_and_engine_cannot_reenter():
    """After a refused exit the position must stay managed and RETRY — and
    the position engine must be disarmed so an internal re-entry can never
    repoint trades[-1] and cancel the retry."""
    fake = Fake()
    orch = ZoneOrchestrator(fake.deps())
    enter_position(fake, orch)
    assert orch.position.engine.entry_guard() is False, "position engine disarmed"
    fake.exit_result_none = True
    # MAX_SL breach → engine exits internally → exit order refused.
    fake.bars = [MinuteBar(ts=datetime(2026, 8, 17, 9, 31), o=100.0, h=100.1, l=90.0, c=92.0)]
    run(orch, datetime(2026, 8, 17, 9, 32))
    assert orch.position is not None, "refused exit keeps the position managed"
    assert fake.exit_calls == 1
    assert orch.position.pending_exit is not None, "exit decision latched"
    # Next minute: a bar that would have re-triggered an R1 entry on an armed
    # engine. The retry must fire the SAME latched exit instead.
    fake.exit_result_none = False
    fake.bars = [MinuteBar(ts=datetime(2026, 8, 17, 9, 32), o=101.2, h=101.3, l=99.8, c=100.6)]
    run(orch, datetime(2026, 8, 17, 9, 33))
    assert fake.exit_calls == 2, "exit retried next minute"
    assert orch.position is None
    assert fake.closed_trades[-1]["exit_reason"] == "MAX_SL"
    assert len(fake.inserted) == 1, "no phantom second trade was ever recorded"


def test_ledger_outage_never_skips_position_management():
    """The docstring promise: an open trade keeps being MANAGED even while
    the ledger reads fail (they used to abort the pass before management)."""
    fake = Fake()
    orch = ZoneOrchestrator(fake.deps())
    enter_position(fake, orch)
    fake.closed_raises = True
    fake.bars = [MinuteBar(ts=datetime(2026, 8, 17, 9, 31), o=100.0, h=100.1, l=90.0, c=92.0)]
    run(orch, datetime(2026, 8, 17, 9, 32))   # must not raise
    assert orch.position is None, "MAX_SL exit executed during the DB outage"
    assert fake.closed_trades[-1]["exit_reason"] == "MAX_SL"
    assert any("counters degraded" in n or "entries blocked" in n
               for n in fake.notifications)
    # And with no position, the outage blocks NEW entries instead of crashing.
    fake.bars = [R1_BAR]
    run(orch, datetime(2026, 8, 17, 9, 33))
    assert orch.position is None and not orch.hunts


# ── live-data staleness gate (fail-safe, symbol-scoped) ───────────────────

def _deps_with_age(fake: Fake, age_fn) -> Any:
    d = fake.deps()
    from dataclasses import replace
    return replace(d, data_age_s=age_fn)


def test_stale_data_blocks_entries_and_alerts():
    fake = Fake()

    async def age(symbol: str):
        assert symbol == "NIFTY", "the gate must probe the TRADED symbol"
        return 999.0

    orch = ZoneOrchestrator(_deps_with_age(fake, age))
    fake.bars = [R1_BAR]
    run(orch, MONDAY)
    assert orch.status.state == "stale_data"
    assert orch.position is None and orch.hunts == []
    assert any("entries are" in n and "blocked" in n for n in fake.notifications)


def test_fresh_data_passes_the_gate():
    fake = Fake()

    async def age(symbol: str):
        return 5.0

    orch = ZoneOrchestrator(_deps_with_age(fake, age))
    fake.bars = [R1_BAR]
    run(orch, MONDAY)
    assert orch.position is not None, "fresh data must not block the entry"


def test_data_age_exception_blocks_instead_of_crashing():
    """Regression for 2026-08-18: a crash inside the freshness probe killed
    EVERY evaluation pass silently for a whole session. Any failure must mean
    'blocked' — never an aborted pass."""
    fake = Fake()

    async def age(symbol: str):
        raise ImportError("cannot import name 'rt'")

    orch = ZoneOrchestrator(_deps_with_age(fake, age))
    fake.bars = [R1_BAR]
    run(orch, MONDAY)   # must not raise
    assert orch.status.state == "stale_data", "probe failure = fail-safe block"
    assert orch.position is None


def test_runtime_audit_rows_for_trades():
    fake = Fake()
    orch = ZoneOrchestrator(fake.deps())
    enter_position(fake, orch)
    fake.bars = [MinuteBar(ts=datetime(2026, 8, 17, 9, 31), o=100.0, h=100.1, l=90.0, c=92.0)]
    run(orch, datetime(2026, 8, 17, 9, 32))
    kinds = [k for k, _, _ in fake.audits]
    assert "runtime_trade_entry" in kinds and "runtime_trade_exit" in kinds, (
        "§11.3: entry and exit fills append audit rows"
    )


def _run_all() -> None:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL  {name}: {e}")
    if failures:
        raise SystemExit(f"{failures} test(s) failed")
    print("all orchestrator tests passed")


if __name__ == "__main__":
    _run_all()
