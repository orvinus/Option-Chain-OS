"""Lakshmishree XTS Interactive adapter + execution routing tests.

Run:  cd backend && PYTHONPATH=. python tests/test_algo_broker.py

The client's transport is injected, so the full session/order lifecycle tests
against scripted gateway responses — no network. The live gateway itself
remains UNVERIFIED until the user's Interactive credentials arrive (the M7
burn-in); these tests pin the contract our side sends and how responses are
interpreted.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from app.algo.broker.xts_interactive import XtsInteractiveClient, XtsInteractiveError
from app.algo.config_models import default_config
from app.core.config import settings


class StubTransport:
    def __init__(self, script: list[tuple[int, dict[str, Any]]]) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, str, Optional[dict[str, Any]]]] = []

    async def __call__(self, method, url, headers, json_body):
        self.calls.append((method, url, json_body))
        if not self.script:
            raise AssertionError("transport called more times than scripted")
        return self.script.pop(0)


def _configure(url: str = "https://xts.example.broker") -> tuple[str, str, str]:
    old = (
        settings.lakshmishree_interactive_url,
        settings.lakshmishree_interactive_app_key,
        settings.lakshmishree_interactive_secret_key,
    )
    settings.lakshmishree_interactive_url = url
    settings.lakshmishree_interactive_app_key = "ikey"
    settings.lakshmishree_interactive_secret_key = "isecret"
    return old


def _restore(old: tuple[str, str, str]) -> None:
    (
        settings.lakshmishree_interactive_url,
        settings.lakshmishree_interactive_app_key,
        settings.lakshmishree_interactive_secret_key,
    ) = old


LOGIN_OK = (200, {"type": "success", "result": {"token": "tok1", "userID": "U1", "isInvestorClient": True}})


def test_unconfigured_client_refuses_login():
    old = _configure("")
    settings.lakshmishree_interactive_url = ""
    try:
        c = XtsInteractiveClient(StubTransport([]))
        assert not c.configured
        try:
            asyncio.run(c.login())
            raise AssertionError("login must refuse when unconfigured")
        except XtsInteractiveError as e:
            assert "not configured" in str(e)
    finally:
        _restore(old)


def test_login_stores_token_and_sends_credentials():
    old = _configure()
    try:
        t = StubTransport([LOGIN_OK])
        c = XtsInteractiveClient(t)
        asyncio.run(c.login())
        assert c.logged_in
        method, url, body = t.calls[0]
        assert method == "POST" and url.endswith("/interactive/user/session")
        assert body == {"appKey": "ikey", "secretKey": "isecret", "source": "WEBAPI"}
        # Second login is a no-op (one session per process).
        asyncio.run(c.login())
        assert len(t.calls) == 1
    finally:
        _restore(old)


def test_login_failure_surfaces_description():
    old = _configure()
    try:
        t = StubTransport([(400, {"type": "error", "description": "Invalid appKey"})])
        c = XtsInteractiveClient(t)
        try:
            asyncio.run(c.login())
            raise AssertionError("should raise")
        except XtsInteractiveError as e:
            assert "Invalid appKey" in str(e)
        assert c.last_error == "Invalid appKey"
    finally:
        _restore(old)


def test_place_market_order_payload_contract():
    old = _configure()
    try:
        t = StubTransport([
            LOGIN_OK,
            (200, {"type": "success", "result": {"AppOrderID": 4242}}),
        ])
        c = XtsInteractiveClient(t)
        oid = asyncio.run(
            c.place_market_order(
                exchange_instrument_id=51234, side="BUY", quantity=150, unique_id="algo-093000"
            )
        )
        assert oid == "4242"
        _, url, body = t.calls[-1]
        assert url.endswith("/interactive/orders")
        assert body["exchangeSegment"] == "NSEFO"
        assert body["exchangeInstrumentID"] == 51234
        assert body["orderType"] == "MARKET" and body["orderSide"] == "BUY"
        assert body["orderQuantity"] == 150 and body["productType"] == "NRML"
        assert body["timeInForce"] == "DAY"
        assert "clientID" not in body, "investor clients must not send clientID"
    finally:
        _restore(old)


def test_bsefo_segment_routes_sensex_orders():
    """SENSEX options trade on BSE F&O — the segment must follow the
    contract (hardcoded NSEFO mis-routed every SENSEX order until
    2026-08-18)."""
    from app.algo.broker.execution import _segment_for

    assert _segment_for("SENSEX") == "BSEFO"
    assert _segment_for("BANKEX") == "BSEFO"
    assert _segment_for("NIFTY") == "NSEFO"
    assert _segment_for("sensex") == "BSEFO", "case-insensitive"

    old = _configure()
    try:
        t = StubTransport([
            LOGIN_OK,
            (200, {"type": "success", "result": {"AppOrderID": 99}}),
        ])
        c = XtsInteractiveClient(t)
        oid = asyncio.run(
            c.place_market_order(
                exchange_instrument_id=812345, side="BUY", quantity=20,
                unique_id="algo-101500", exchange_segment="BSEFO",
            )
        )
        assert oid == "99"
        _, _url, body = t.calls[-1]
        assert body["exchangeSegment"] == "BSEFO"
    finally:
        _restore(old)


def test_token_rejection_triggers_exactly_one_relogin():
    old = _configure()
    try:
        t = StubTransport([
            LOGIN_OK,
            (401, {"type": "error", "description": "token expired"}),
            (200, {"type": "success", "result": {"token": "tok2", "userID": "U1", "isInvestorClient": True}}),
            (200, {"type": "success", "result": {"AppOrderID": 7}}),
        ])
        c = XtsInteractiveClient(t)
        oid = asyncio.run(
            c.place_market_order(
                exchange_instrument_id=1, side="SELL", quantity=75, unique_id="x"
            )
        )
        assert oid == "7"
        assert len(t.calls) == 4, "login → rejected order → re-login → retried order"
    finally:
        _restore(old)


def test_body_level_invalid_token_triggers_relogin():
    """The LIVE Lakshmishree gateway rejects a dead token as HTTP 200 with an
    error body ("Invalid Token" / "Token/Authorization not found"), never
    401/403 — observed 2026-08-17. The re-login path must fire on those too."""
    old = _configure()
    try:
        for desc in ("Invalid Token", "Token/Authorization not found"):
            t = StubTransport([
                LOGIN_OK,
                (200, {"type": "error", "description": desc}),
                (200, {"type": "success", "result": {"token": "tok2", "userID": "U1", "isInvestorClient": True}}),
                (200, {"type": "success", "result": {"BalanceList": []}}),
            ])
            c = XtsInteractiveClient(t)
            bal = asyncio.run(c.balance())
            assert bal == {"BalanceList": []}, desc
            assert len(t.calls) == 4, f"{desc}: login → rejected → re-login → retried"
    finally:
        _restore(old)


def test_non_auth_error_body_does_not_relogin():
    """A plain business error (no token wording) must NOT burn the one
    re-login — it raises straight through."""
    old = _configure()
    try:
        t = StubTransport([
            LOGIN_OK,
            (200, {"type": "error", "description": "Order history not found for AppOrderID [9] given."}),
        ])
        c = XtsInteractiveClient(t)
        try:
            asyncio.run(c.balance())
            raise AssertionError("expected XtsInteractiveError")
        except XtsInteractiveError:
            pass
        assert len(t.calls) == 2, "login → failed call, no re-login attempt"
    finally:
        _restore(old)


def test_order_result_parses_history_list_and_avg_price():
    old = _configure()
    try:
        t = StubTransport([
            LOGIN_OK,
            (200, {"type": "success", "result": [
                {"OrderStatus": "New", "OrderAverageTradedPrice": ""},
                {"OrderStatus": "Filled", "OrderAverageTradedPrice": "101.35"},
            ]}),
        ])
        c = XtsInteractiveClient(t)
        res = asyncio.run(c.order_result("9"))
        assert res.status == "Filled" and res.average_price == 101.35
    finally:
        _restore(old)


def test_await_fill_returns_avg_and_raises_on_rejection():
    old = _configure()
    try:
        t = StubTransport([
            LOGIN_OK,
            (200, {"type": "success", "result": [{"OrderStatus": "PendingNew", "OrderAverageTradedPrice": ""}]}),
            (200, {"type": "success", "result": [{"OrderStatus": "Filled", "OrderAverageTradedPrice": "99.9"}]}),
        ])
        c = XtsInteractiveClient(t)
        fill, status = asyncio.run(c.await_fill("5", fallback_price=100.0))
        assert fill == 99.9 and status == "Filled"

        t2 = StubTransport([
            LOGIN_OK,
            (200, {"type": "success", "result": [{
                "OrderStatus": "Rejected", "OrderAverageTradedPrice": "",
                "CancelRejectReason": "OEMS:RMS : Margin Exceeds : Shortfall[416]",
            }]}),
        ])
        c2 = XtsInteractiveClient(t2)
        try:
            asyncio.run(c2.await_fill("6", fallback_price=100.0))
            raise AssertionError("rejection must raise")
        except XtsInteractiveError as e:
            # The broker's own reason must surface in the error (rejections
            # were "no reason given" before this was parsed — burn-in finding).
            assert "Rejected" in str(e) and "Margin Exceeds" in str(e)
    finally:
        _restore(old)


def test_execution_router_paper_mode_never_touches_broker():
    from app.algo.broker.execution import execute_entry

    cfg = default_config()
    assert cfg.global_.paper.paper_mode
    res = asyncio.run(
        execute_entry(
            cfg, symbol="NIFTY", expiry=None, strike=24500, option_type="CE",
            raw_price=100.0, lots=1, lot_size=75, unique_id="t",
        )
    )
    assert res is not None and res.note == "paper"
    assert res.fill_price == 100.5, "paper buy fill = +0.5% slippage"
    assert res.broker_order_id == ""


def test_execution_router_refuses_live_when_unconfigured():
    from app.algo.broker.execution import execute_entry

    old = _configure("")
    settings.lakshmishree_interactive_url = ""
    try:
        cfg = default_config()
        cfg.global_.paper.paper_mode = False   # LIVE mode…
        res = asyncio.run(
            execute_entry(
                cfg, symbol="NIFTY", expiry=None, strike=24500, option_type="CE",
                raw_price=100.0, lots=1, lot_size=75, unique_id="t",
            )
        )
        assert res is None, "…but no broker configured → entry REFUSED, never silent paper"
    finally:
        _restore(old)


def test_cancel_order_and_positions_net():
    old = _configure()
    try:
        t = StubTransport([
            LOGIN_OK,
            (200, {"type": "success", "result": {}}),
            (200, {"type": "success", "result": {"positionList": [
                {"ExchangeInstrumentId": 45091, "Quantity": "65"},
            ]}}),
        ])
        c = XtsInteractiveClient(t)
        asyncio.run(c.cancel_order("77"))
        method, url, _ = t.calls[-1]
        assert method == "DELETE" and url.endswith("/interactive/orders?appOrderID=77")
        rows = asyncio.run(c.positions_net())
        assert rows and rows[0]["Quantity"] == "65"
        assert "dayOrNet=NetWise" in t.calls[-1][1]
    finally:
        _restore(old)


def test_order_book_and_balance_shapes():
    old = _configure()
    try:
        t = StubTransport([
            LOGIN_OK,
            (200, {"type": "success", "result": [
                {"AppOrderID": 1, "OrderStatus": "Filled", "OrderSide": "BUY"},
            ]}),
            (200, {"type": "success", "result": {"BalanceList": [
                {"limitObject": {"RMSSubLimits": {"netMarginAvailable": "0"}}},
            ]}}),
        ])
        c = XtsInteractiveClient(t)
        book = asyncio.run(c.order_book())
        assert len(book) == 1 and book[0]["OrderStatus"] == "Filled"
        assert t.calls[-1][1].endswith("/interactive/orders")
        bal = asyncio.run(c.balance())
        assert "BalanceList" in bal
    finally:
        _restore(old)


def test_reconcile_live_position_match_mismatch_unresolved():
    from app.algo.broker import execution as ex

    old = _configure()
    orig_resolve = ex._resolve_instrument_id
    orig_get = ex.get_interactive_client
    try:
        async def resolve_ok(*a, **k):
            return 45091

        t = StubTransport([
            LOGIN_OK,
            (200, {"type": "success", "result": {"positionList": [
                {"ExchangeInstrumentId": "45091", "Quantity": 65},
            ]}}),
            (200, {"type": "success", "result": {"positionList": []}}),
        ])
        client = XtsInteractiveClient(t)
        ex._resolve_instrument_id = resolve_ok
        ex.get_interactive_client = lambda: client

        match, detail = asyncio.run(ex.reconcile_live_position(
            symbol="NIFTY", expiry=None, strike=24000, option_type="PE",
            expected_qty=65,
        ))
        assert match is True and "65" in detail

        match2, _ = asyncio.run(ex.reconcile_live_position(
            symbol="NIFTY", expiry=None, strike=24000, option_type="PE",
            expected_qty=65,
        ))
        assert match2 is False, "empty broker book vs open position = mismatch"

        async def resolve_none(*a, **k):
            return None

        ex._resolve_instrument_id = resolve_none
        match3, detail3 = asyncio.run(ex.reconcile_live_position(
            symbol="NIFTY", expiry=None, strike=24000, option_type="PE",
            expected_qty=65,
        ))
        assert match3 is None and "unresolved" in detail3
    finally:
        ex._resolve_instrument_id = orig_resolve
        ex.get_interactive_client = orig_get
        _restore(old)


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
    print("all broker tests passed")


if __name__ == "__main__":
    _run_all()
