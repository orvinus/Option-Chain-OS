"""Backtest deps — ledger accounting, collector dedupe, deps mapping.

Run:  cd backend && PYTHONPATH=. python tests/test_algo_backtest_deps.py
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

from app.algo.backtest.data import build_day_frame
from app.algo.backtest.deps import BacktestDeps, EventCollector, InMemoryLedger
from app.algo.config_models import default_config
from app.algo.orchestrator import Contract

D1 = date(2026, 3, 3)
D2 = date(2026, 3, 4)


def _trade(seq_day: date, zone: str, pnl: float, exit_min: int):
    return {
        "trade_date": seq_day, "day": "tuesday", "zone_id": zone,
        "index_symbol": "NIFTY", "side": "CALL", "token": "t", "strike": 22500,
        "expiry": seq_day, "entry_ts": datetime.combine(seq_day, datetime.min.time()),
        "entry_price": 100.0, "lots": 1, "ledger": "paper", "sub_scenario": "S1A",
        "pnl_rupees": pnl,
        "exit_ts": datetime.combine(seq_day, datetime.min.time()) + timedelta(minutes=exit_min),
    }


# ── InMemoryLedger ──────────────────────────────────────────────────────────

def test_ledger_compounding_vs_fixed():
    for mode, d2_expected in (("compounding", 30000 + 500 - 200), ("fixed_per_day", 30000)):
        led = InMemoryLedger(30000.0, mode)  # type: ignore[arg-type]
        tid = led.insert(**{k: v for k, v in _trade(D1, "Z1", 0, 0).items()
                            if k not in ("pnl_rupees", "exit_ts")})
        led.close(tid, exit_ts=datetime(2026, 3, 3, 10, 0), pnl_rupees=500.0)
        tid2 = led.insert(**{k: v for k, v in _trade(D1, "Z2", 0, 0).items()
                             if k not in ("pnl_rupees", "exit_ts")})
        led.close(tid2, exit_ts=datetime(2026, 3, 3, 11, 0), pnl_rupees=-200.0)
        assert led.balance_for(D1) == 30000 + 500 - 200, f"{mode}: intraday realized counts"
        assert led.balance_for(D2) == d2_expected, f"{mode}: next-day carry"


def test_ledger_today_closed_is_exit_ordered():
    led = InMemoryLedger(30000.0, "compounding")
    a = led.insert(**{k: v for k, v in _trade(D1, "Z1", 0, 0).items()
                      if k not in ("pnl_rupees", "exit_ts")})
    b = led.insert(**{k: v for k, v in _trade(D1, "Z2", 0, 0).items()
                      if k not in ("pnl_rupees", "exit_ts")})
    # b exits FIRST — the streak logic depends on exit order, not entry order.
    led.close(b, exit_ts=datetime(2026, 3, 3, 10, 0), pnl_rupees=-50.0)
    led.close(a, exit_ts=datetime(2026, 3, 3, 11, 0), pnl_rupees=80.0)
    assert led.today_closed(D1) == [("Z2", -50.0), ("Z1", 80.0)]
    assert led.today_closed(D2) == []


def test_ledger_open_trade_and_resume_seed():
    led = InMemoryLedger(30000.0, "compounding")
    led.seed_closed([_trade(D1, "Z1", 350.0, 30) | {"seq": 7}])
    assert led.balance_for(D2) == 30350.0, "resume seeding restores compounding"
    tid = led.insert(**{k: v for k, v in _trade(D2, "Z1", 0, 0).items()
                        if k not in ("pnl_rupees", "exit_ts")})
    assert tid == 8, "seq continues after the seeded rows (deterministic ids on resume)"
    assert led.open_trade() is not None


# ── EventCollector ──────────────────────────────────────────────────────────

def test_collector_signal_transition_dedupe():
    c = EventCollector()
    kw = dict(ts=datetime(2026, 3, 3, 9, 20), trade_date=D1, day="tuesday",
              zone_id="Z1", indicator="oi_change")
    assert c.record_signal(**kw, reading="CALL") is True
    assert c.record_signal(**kw, reading="CALL") is False, "same reading → no new row"
    assert c.record_signal(**kw, reading="PUT") is True, "transition → new row"
    assert c.record_signal(**dict(kw, zone_id="Z2"), reading="PUT") is True, "per-zone key"
    assert len(c.signals) == 3


def test_collector_status_timeline_dedupe():
    c = EventCollector()
    c.status_transition(datetime(2026, 3, 3, 9, 15), "idle", "", "")
    c.status_transition(datetime(2026, 3, 3, 9, 16), "idle", "", "")
    c.status_transition(datetime(2026, 3, 3, 9, 20), "hunting", "tuesday/Z1", "CALL")
    assert len(c.status_timeline) == 2


# ── BacktestDeps mapping ────────────────────────────────────────────────────

def _tiny_frame():
    open_utc = datetime(2026, 3, 3, 3, 45, tzinfo=timezone.utc)
    chain = [{"strike": 22500, "option_type": "CE", "bucket": open_utc,
              "oi": 100, "o": 100.0, "h": 100.5, "l": 99.5, "c": 100.0}]
    return build_day_frame(
        trade_date=D1, symbol="NIFTY", expiry=D1, open_utc=open_utc,
        chain=chain, spot_rows=[], preopen=22500.0,
        bounds=(22000, 23000), strike_step=50,
    )


def test_deps_mapping_basics():
    cfg = default_config(today=D1)
    frame = _tiny_frame()
    led = InMemoryLedger(30000.0, "compounding")
    col = EventCollector()
    bd = BacktestDeps(cfg=cfg, frame=frame, ledger=led, collector=col,
                      lot_sizes={"NIFTY": 65})
    deps = bd.as_orchestrator_deps()

    assert deps.lot_size("NIFTY") == 65, "snapshotted lot size"
    assert deps.lot_size("SENSEX") == 0, (
        "unknown symbol → 0 → the orchestrator refuses the entry loudly "
        "(the old silent 75 fallback sized trades with wrong contract math)"
    )
    assert deps.reconcile_live is None, "never consulted in a backtest"

    async def go():
        assert (await deps.get_config()) is cfg
        bd.set_now(datetime(2026, 3, 3, 9, 16))
        bal = await deps.current_balance(cfg)
        assert bal == 30000.0
        bar = await deps.latest_minute(
            Contract(symbol="NIFTY", expiry=D1, strike=22500, option_type="CE"),
            datetime(2026, 3, 3, 9, 16),
        )
        assert bar is not None and bar.o == 100.0, "minute 09:15 closed at 09:16"
        bar2 = await deps.latest_minute(
            Contract(symbol="NIFTY", expiry=D1, strike=22500, option_type="CE"),
            datetime(2026, 3, 3, 9, 17),
        )
        assert bar2 is None, "silent minute → None"
        tid = await deps.insert_trade(
            trade_date=D1, day="tuesday", zone_id="Z1", index_symbol="NIFTY",
            side="CALL", token="t", strike=22500, expiry=D1,
            entry_ts=datetime(2026, 3, 3, 9, 20), entry_price=100.0,
            lots=1, ledger="paper", sub_scenario="S1A",
        )
        await deps.close_trade(
            tid, exit_ts=datetime(2026, 3, 3, 9, 40), exit_price=105.0,
            pnl_rupees=250.0, pnl_pct=1.0, exit_reason="TARGET", fees={"total": 40.0},
        )
        closed = await deps.today_closed(D1, "paper")
        assert closed == [("Z1", 250.0)]
        assert (await deps.current_balance(cfg)) == 30250.0

    asyncio.run(go())


def test_deps_paper_execution_route():
    """execute_entry/execute_exit are the REAL broker.execution functions;
    with paper_mode pinned they fill via the paper simulator (slippage) and
    never touch a broker client."""
    cfg = default_config(today=D1)
    assert cfg.global_.paper.paper_mode is True
    frame = _tiny_frame()
    bd = BacktestDeps(cfg=cfg, frame=frame,
                      ledger=InMemoryLedger(30000.0, "compounding"),
                      collector=EventCollector(), lot_sizes={"NIFTY": 65})
    deps = bd.as_orchestrator_deps()

    async def go():
        res = await deps.execute_entry(
            cfg, symbol="NIFTY", expiry=D1, strike=22500, option_type="CE",
            raw_price=100.0, lots=1, lot_size=65, unique_id="t",
        )
        slip = cfg.global_.paper.slippage_pct / 100
        assert res is not None and abs(res.fill_price - 100.0 * (1 + slip)) < 1e-9
        assert res.broker_order_id == "", "paper fill — no broker order"
        res2 = await deps.execute_exit(
            cfg, symbol="NIFTY", expiry=D1, strike=22500, option_type="CE",
            raw_price=100.0, lots=1, lot_size=65, unique_id="t", entry_order_id="",
        )
        assert res2 is not None and abs(res2.fill_price - 100.0 * (1 - slip)) < 1e-9

    asyncio.run(go())


def _run_all() -> None:
    import traceback

    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except Exception:
                failed += 1
                print(f"FAIL  {name}")
                traceback.print_exc()
    if failed:
        raise SystemExit(f"{failed} test(s) failed")
    print("all backtest deps tests passed")


if __name__ == "__main__":
    _run_all()
