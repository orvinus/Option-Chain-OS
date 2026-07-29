"""Regression tests for the XTS feed self-heal / auto-recovery layer.

These guard the "feed must never silently die" behaviour added 2026-06-17:
  * ``_classify_400`` distinguishes a benign "already subscribed" 400 from a
    genuine auth rejection ('Invalid Token').
  * ``MarketDataSession.login`` is debounced (non-forced re-logins within
    LOGIN_DEBOUNCE_S reuse the token) but ``force=True`` always mints fresh —
    so the single-session token is not thrashed by racing logins.
  * ``OptionFeedClient._self_heal_auth`` re-logins + requests a reconnect on an
    auth failure, but respects the cooldown and the attempt cap (no login storm).

Runnable without pytest:  python -m tests.test_self_heal   (exit 0 = all passed)
Also collectable by pytest (each ``test_*`` is an async function).
"""
from __future__ import annotations

import asyncio
import time

import httpx

from app.auth.smartapi_session import LOGIN_DEBOUNCE_S, MarketDataSession
from app.ingest import ws_client
from app.ingest.ws_client import (
    AUTO_RELOGIN_COOLDOWN_S,
    AUTO_RELOGIN_MAX_ATTEMPTS,
    OptionFeedClient,
    _classify_400,
)


# --------------------------------------------------------------------------- 1
async def test_classify_400_invalid_token_is_not_already_subscribed() -> None:
    resp = httpx.Response(400, json={"description": "Invalid Token"})
    is_already, detail = _classify_400(resp)
    assert is_already is False, "Invalid Token must NOT be treated as already-subscribed"
    assert detail == "Invalid Token"


async def test_classify_400_already_subscribed_is_benign() -> None:
    resp = httpx.Response(400, json={"description": "Instrument already subscribed!"})
    is_already, detail = _classify_400(resp)
    assert is_already is True, "already-subscribed body must be classified benign"
    assert "already" in detail.lower()


async def test_classify_400_plain_text_body() -> None:
    resp = httpx.Response(400, text="Invalid Token")
    is_already, detail = _classify_400(resp)
    assert is_already is False
    assert detail == "Invalid Token"


async def test_classify_400_subscription_limit_is_rejection() -> None:
    # Live message seen 2026-07-06: contains "already subscribed" but is a REAL
    # rejection (gateway slots full after a swap failed to free the old set).
    resp = httpx.Response(400, json={"description": (
        "Exceeded Instrument Subscription Limit of 50.You have already "
        "subscribed 50/50 and you are trying subscribe 51 Instrument.")})
    is_already, detail = _classify_400(resp)
    assert is_already is False, "limit-exceeded must NOT be treated as already-subscribed"
    assert "limit" in detail.lower()


# --------------------------------------------------------------------------- 2
async def test_login_debounce_reuses_token_then_force_mints_fresh() -> None:
    calls = {"n": 0}

    async def fake_xts_login() -> dict:
        calls["n"] += 1
        return {"token": f"tok-{calls['n']}", "userID": "TESTUSER"}

    sess = MarketDataSession()

    async def noop_persist(_tokens) -> None:
        return None

    # Patch the network login + DB persist on this isolated instance.
    sess._persist = noop_persist  # type: ignore[assignment]
    ws_client_orig = ws_client  # keep linter calm
    import app.market_data.xts_client as xc

    orig = xc.login
    xc.login = fake_xts_login  # type: ignore[assignment]
    try:
        t1 = await sess.login()  # first real login
        t2 = await sess.login()  # within debounce -> reuse, no network call
        assert calls["n"] == 1, "second non-forced login must be debounced"
        assert t1.jwt_token == t2.jwt_token == "tok-1"

        t3 = await sess.login(force=True)  # explicit -> fresh
        assert calls["n"] == 2, "force=True must bypass debounce"
        assert t3.jwt_token == "tok-2"

        # Simulate the debounce window elapsing -> next non-forced login mints fresh.
        sess._last_login_mono = time.monotonic() - (LOGIN_DEBOUNCE_S + 1)
        t4 = await sess.login()
        assert calls["n"] == 3, "after debounce window a non-forced login mints fresh"
        assert t4.jwt_token == "tok-3"
    finally:
        xc.login = orig  # type: ignore[assignment]


# --------------------------------------------------------------------------- 3
def _make_feed() -> OptionFeedClient:
    q: asyncio.Queue = asyncio.Queue()

    async def _provider():
        return ([], 0.0)

    return OptionFeedClient(q, _provider)


async def test_self_heal_relogins_and_requests_reconnect() -> None:
    feed = _make_feed()
    login_calls = {"n": 0}

    class FakeSession:
        async def login(self, force: bool = False):
            login_calls["n"] += 1
            return None

    fake = FakeSession()
    orig = ws_client.get_session_manager
    ws_client.get_session_manager = lambda: fake  # type: ignore[assignment]
    try:
        assert feed._reconnect_requested is False
        await feed._self_heal_auth()
        assert login_calls["n"] == 1, "first auth failure must trigger a re-login"
        assert feed._reconnect_requested is True, "self-heal must request a socket reconnect"
        assert feed._auto_relogin_attempts == 1
    finally:
        ws_client.get_session_manager = orig  # type: ignore[assignment]


async def test_self_heal_respects_cooldown() -> None:
    feed = _make_feed()
    login_calls = {"n": 0}

    class FakeSession:
        async def login(self, force: bool = False):
            login_calls["n"] += 1
            return None

    orig = ws_client.get_session_manager
    ws_client.get_session_manager = lambda: FakeSession()  # type: ignore[assignment]
    try:
        await feed._self_heal_auth()  # attempt 1 (logs in)
        await feed._self_heal_auth()  # immediate retry -> blocked by cooldown
        assert login_calls["n"] == 1, "second self-heal within cooldown must NOT re-login"
    finally:
        ws_client.get_session_manager = orig  # type: ignore[assignment]


async def test_self_heal_gives_up_after_cap() -> None:
    feed = _make_feed()
    login_calls = {"n": 0}

    class FakeSession:
        async def login(self, force: bool = False):
            login_calls["n"] += 1
            return None

    orig = ws_client.get_session_manager
    ws_client.get_session_manager = lambda: FakeSession()  # type: ignore[assignment]
    try:
        # Drive past the cap, defeating the cooldown each round by rewinding the clock.
        for _ in range(AUTO_RELOGIN_MAX_ATTEMPTS + 3):
            feed._last_auto_relogin = time.time() - (AUTO_RELOGIN_COOLDOWN_S + 1)
            await feed._self_heal_auth()
        assert login_calls["n"] == AUTO_RELOGIN_MAX_ATTEMPTS, (
            f"self-heal must stop after {AUTO_RELOGIN_MAX_ATTEMPTS} attempts, "
            f"got {login_calls['n']} (login storm!)"
        )
    finally:
        ws_client.get_session_manager = orig  # type: ignore[assignment]


# --------------------------------------------------------------------------- runner
async def _main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and asyncio.iscoroutinefunction(v)]
    passed = 0
    for t in tests:
        try:
            await t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
