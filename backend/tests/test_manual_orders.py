"""Manual test entry / square-off and the position-price fixes (2026-09-15).

Found in the live Lakshmishree burn-in:
  * the strip marked an open position against the strip's OWN scoped strike,
    not the position's contract (23600CE @ ₹1.15 marked at 23150CE ₹84.70 → a
    phantom +₹70,000);
  * the square-off "reference price" was the last CLOSED minute (₹1.15 shown
    while the market and the fill were ₹0.95);
  * a manual square-off left ``status.position`` on screen until the next
    per-minute pass;
  * the broker card had no usable MTM (the broker's fields read 0.00).
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from app.algo import orchestrator as orch_mod
from app.algo.config_models import default_config
from app.algo.orchestrator import Contract, ManualOrderError, OrchestratorDeps, ZoneOrchestrator

EXPIRY = date(2026, 9, 15)
NOW = datetime(2026, 9, 15, 14, 44, 53)  # a Tuesday, inside Z3


def _deps(cfg, *, premium=1.10, fill=1.15, quote=(0.95, datetime(2026, 9, 15, 9, 19, 40, tzinfo=timezone.utc))):
    calls: dict = {"entries": [], "exits": [], "inserted": [], "closed": []}

    async def get_config():
        return cfg

    async def select_strikes(symbol, side, zone_cfg):
        calls["band"] = (zone_cfg.premium_min, zone_cfg.premium_max, zone_cfg.strike_scan_count)
        return [(Contract(symbol, EXPIRY, 23600, "CE"), premium)]

    async def execute_entry(cfg_, **kw):
        calls["entries"].append(kw)
        return SimpleNamespace(fill_price=fill, broker_order_id="1210407205", note="Filled")

    async def execute_exit(cfg_, **kw):
        calls["exits"].append(kw)
        return SimpleNamespace(fill_price=0.95, broker_order_id="1210407206", note="Filled")

    async def insert_trade(**kw):
        calls["inserted"].append(kw)
        return 6

    async def close_trade(trade_id, **kw):
        calls["closed"].append((trade_id, kw))

    async def latest_minute(contract, now):
        return SimpleNamespace(ts=now, o=1.15, h=1.15, l=1.15, c=1.15)  # the stale closed-minute close

    async def latest_quote(contract):
        return quote

    async def noop(*a, **k):
        return []

    async def balance(cfg_):
        return 1000.0

    deps = OrchestratorDeps(
        get_config=get_config,
        evaluate_indicator=noop,
        select_strikes=select_strikes,
        build_engine=noop,
        latest_minute=latest_minute,
        lot_size=lambda s: 65,
        current_balance=balance,
        today_closed=noop,
        insert_trade=insert_trade,
        close_trade=close_trade,
        record_signal=noop,
        notify=lambda k, m: None,
        execute_entry=execute_entry,
        execute_exit=execute_exit,
        latest_quote=latest_quote,
    )
    return deps, calls


def _cfg(paper=False):
    cfg = default_config()
    cfg.global_.paper.paper_mode = paper
    return cfg


def test_test_entry_is_capped_and_routed_through_execute_entry() -> None:
    cfg = _cfg()
    deps, calls = _deps(cfg)
    o = ZoneOrchestrator(deps)
    out = asyncio.run(o.manual_test_entry(NOW, side="CALL", max_notional=1000, premium_min=0.5, premium_max=2.0))

    assert calls["band"] == (0.5, 2.0, 1), calls["band"]
    # floor(1000 / (1.10 × 65)) = 13 lots → 845 qty, the burn-in's real order.
    assert out["lots"] == 13 and out["quantity"] == 845, out
    assert out["notional"] <= 1000
    assert calls["entries"][0]["lots"] == 13 and calls["entries"][0]["raw_price"] == 1.10
    assert out["ledger"] == "live" and out["broker_order_id"] == "1210407205"
    assert o.position is not None and o.position.trade_id == 6
    assert o.status.position and o.status.position["trade_id"] == 6


def test_test_entry_refuses_when_one_lot_exceeds_the_cap() -> None:
    cfg = _cfg()
    deps, calls = _deps(cfg, premium=20.0)  # one lot = ₹1,300
    o = ZoneOrchestrator(deps)
    try:
        asyncio.run(o.manual_test_entry(NOW, side="CALL", max_notional=1000, premium_min=0.5, premium_max=30))
    except ManualOrderError as e:
        assert "above the ₹1,000 cap" in str(e)
    else:
        raise AssertionError("an over-cap test entry must be refused")
    assert calls["entries"] == [] and o.position is None


def test_test_entry_refuses_while_a_position_is_open() -> None:
    cfg = _cfg()
    deps, _ = _deps(cfg)
    o = ZoneOrchestrator(deps)
    asyncio.run(o.manual_test_entry(NOW, side="CALL", max_notional=1000, premium_min=0.5, premium_max=2.0))
    try:
        asyncio.run(o.manual_test_entry(NOW, side="PUT", max_notional=1000, premium_min=0.5, premium_max=2.0))
    except ManualOrderError as e:
        assert "already open" in str(e)
    else:
        raise AssertionError("a second manual entry must be refused")


def test_square_off_uses_the_newest_tick_and_clears_the_strip() -> None:
    cfg = _cfg()
    deps, calls = _deps(cfg)
    o = ZoneOrchestrator(deps)
    asyncio.run(o.manual_test_entry(NOW, side="CALL", max_notional=1000, premium_min=0.5, premium_max=2.0))
    out = asyncio.run(o.manual_square_off(NOW + timedelta(minutes=5)))

    assert out["closed"] is True
    assert out["reference_price"] == 0.95 and out["reference_source"] == "last traded price", out
    assert out["reference_ts"] and out["reference_ts"].startswith("2026-09-15T14:49:40"), out["reference_ts"]
    # The exit went through the engine's own exit path, against the entry order.
    assert calls["exits"][0]["entry_order_id"] == "1210407205"
    assert calls["exits"][0]["raw_price"] == 0.95
    assert calls["closed"][0][1]["exit_reason"] == "MANUAL_SQUARE_OFF"
    assert o.position is None
    assert o.status.position is None, "the strip must not keep showing a closed position"


def test_square_off_falls_back_to_the_closed_minute_without_a_quote() -> None:
    cfg = _cfg()
    deps, _ = _deps(cfg, quote=None)
    o = ZoneOrchestrator(deps)
    asyncio.run(o.manual_test_entry(NOW, side="CALL", max_notional=1000, premium_min=0.5, premium_max=2.0))
    out = asyncio.run(o.manual_square_off(NOW + timedelta(minutes=5)))
    assert out["reference_price"] == 1.15 and out["reference_source"] == "last closed minute", out


def test_square_off_with_nothing_open_is_refused() -> None:
    deps, _ = _deps(_cfg())
    try:
        asyncio.run(ZoneOrchestrator(deps).manual_square_off(NOW))
    except ManualOrderError as e:
        assert "no open position" in str(e)
    else:
        raise AssertionError("expected a refusal")


def test_strip_prices_the_position_at_its_own_contract(monkeypatch) -> None:
    """The phantom +₹70,000: the strip's scoped strike must never price the position."""
    from app.algo import live_stream, series

    pos = {
        "trade_id": 6, "side": "CALL", "contract": "NIFTY:260915:23600:CE", "entry": 1.15,
        "lots": 13, "zone": "Z3", "ledger": "live", "lot_size": 65,
        "strike": 23600, "option_type": "CE", "expiry": "2026-09-15", "last_close": 1.10,
    }
    fake_orch = SimpleNamespace(
        status=SimpleNamespace(
            state="in_trade", active_zone="tuesday/Z3", direction="CALL", readings={},
            gate_blocks=[], paused_reason="", realized_pnl_today=0.0, trades_today=1,
            last_evaluated=None, hunting_strikes=0, position=pos,
        ),
        hunts=[],
    )
    monkeypatch.setattr(orch_mod, "get_orchestrator", lambda: fake_orch)
    seen = {}

    async def quote(symbol, expiry, strike, option_type):
        seen["asked"] = (symbol, expiry, strike, option_type)
        return 0.95, datetime.now(timezone.utc)

    monkeypatch.setattr(series, "latest_contract_quote", quote)
    out = asyncio.run(live_stream._status_block(84.70))  # the strip's 23150CE price
    p = out["position"]
    assert seen["asked"] == ("NIFTY", date(2026, 9, 15), 23600, "CE")
    assert p["current_price"] == 0.95
    # (0.95 − 1.15) × 65 × 13 = −169.00 — not (84.70 − 1.15) × 845 = +70,599.75
    assert p["unrealized_rupees"] == -169.0, p
    assert p["unrealized_pct"] == -17.39


def test_broker_positions_get_our_price_and_exact_mtm(monkeypatch) -> None:
    from app.algo import series
    from app.api import algo_trades

    async def quote(symbol, expiry, strike, option_type):
        assert (symbol, expiry, strike, option_type) == ("NIFTY", date(2026, 9, 15), 23600, "CE")
        return 0.90, datetime(2026, 9, 15, 9, 20, tzinfo=timezone.utc)

    monkeypatch.setattr(series, "latest_contract_quote", quote)
    closed = {"TradingSymbol": "NIFTY 15SEP2026 CE 23600", "Quantity": "0",
              "BuyAmount": "971.75", "SellAmount": "802.75"}
    open_ = {"TradingSymbol": "NIFTY 15SEP2026 CE 23600", "Quantity": "845",
             "BuyAmount": "971.75", "SellAmount": "0"}
    junk = {"TradingSymbol": "SOMETHING ELSE"}
    rows = [closed, open_, junk]
    asyncio.run(algo_trades._enrich_positions_with_our_price(rows))
    assert closed["OurMTM"] == -169.0, closed          # 802.75 − 971.75
    assert open_["OurMTM"] == round(0 - 971.75 + 845 * 0.90, 2), open_
    assert open_["OurLTP"] == 0.9 and open_["OurLTPAt"] == "14:50:00"
    assert "OurMTM" not in junk
