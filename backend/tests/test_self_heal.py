"""Regression tests for the XTS feed self-heal / auto-recovery layer.

These guard the "feed must never silently die" behaviour added 2026-06-17:
  * ``_classify_400`` distinguishes a benign "already subscribed" 400 from a
    genuine auth rejection ('Invalid Token').
  * ``MarketDataSession.login`` enforces the rotation floor (LOGIN_FLOOR_S):
    within it every non-manual login — forced or not — reuses the token, so
    the single-session token cannot be thrashed by racing recovery actors.
  * ``OptionFeedClient._self_heal_auth`` re-logins + requests a reconnect on an
    auth failure, but respects the cooldown and the attempt cap (no login storm).

Runnable without pytest:  python -m tests.test_self_heal   (exit 0 = all passed)
Also collectable by pytest (each ``test_*`` is an async function).
"""
from __future__ import annotations

import asyncio
import time

import httpx

from app.auth.market_session import MarketDataSession
from app.ingest import ws_client
from app.ingest.ws_client import (
    AUTO_RELOGIN_ATTEMPT_DECAY_S,
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
async def test_login_floor_reuses_token_and_only_manual_bypasses() -> None:
    """Floor semantics replaced the old 20s debounce: within LOGIN_FLOOR_S even a
    force=True login reuses the existing token (the 2026-08-06 storm was forced
    logins at ~6s cadence); only manual=True mints fresh."""
    from datetime import datetime, timedelta, timezone

    calls = {"n": 0}

    async def fake_xts_login() -> dict:
        calls["n"] += 1
        return {"token": f"tok-{calls['n']}", "userID": "TESTUSER"}

    sess = MarketDataSession()

    async def noop_persist(_tokens) -> None:
        return None

    async def noop_seed() -> None:
        return None

    # Patch the network login + DB persist + DB floor-seed on this instance.
    sess._persist = noop_persist  # type: ignore[assignment]
    sess._seed_floor_from_db_once = noop_seed  # type: ignore[assignment]
    ws_client_orig = ws_client  # keep linter calm
    import app.market_data.xts_client as xc

    orig = xc.login
    xc.login = fake_xts_login  # type: ignore[assignment]
    try:
        t1 = await sess.login()  # first real login (no usable token -> proceeds)
        t2 = await sess.login()  # within the floor -> reuse, no network call
        assert calls["n"] == 1, "second non-forced login must be floored"
        assert t1.jwt_token == t2.jwt_token == "tok-1"

        t3 = await sess.login(force=True)  # forced but NOT manual -> still floored
        assert calls["n"] == 1, "force=True must NOT bypass the rotation floor"
        assert t3.jwt_token == "tok-1"

        t4 = await sess.login(force=True, manual=True)  # human override -> fresh
        assert calls["n"] == 2, "manual=True is the only sub-floor path"
        assert t4.jwt_token == "tok-2"

        # Simulate the floor elapsing -> next non-forced login mints fresh.
        sess._last_login_wall = datetime.now(timezone.utc) - timedelta(seconds=999)
        t5 = await sess.login()
        assert calls["n"] == 3, "after the floor a non-forced login mints fresh"
        assert t5.jwt_token == "tok-3"
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

        # Broker-truth markers used by _self_heal_auth (see F4).
        def mark_broker_ok(self) -> None: ...
        def mark_broker_rejected(self, reason: str = "") -> None: ...

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

        # Broker-truth markers used by _self_heal_auth (see F4).
        def mark_broker_ok(self) -> None: ...
        def mark_broker_rejected(self, reason: str = "") -> None: ...

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

        # Broker-truth markers used by _self_heal_auth (see F4).
        def mark_broker_ok(self) -> None: ...
        def mark_broker_rejected(self, reason: str = "") -> None: ...

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


async def test_self_heal_attempt_budget_re_arms_after_quiet_period() -> None:
    """The attempt cap must NOT be a one-way latch.

    Its only other reset is a fully clean subscribe, which cannot happen while auth
    is broken — so before this, five failures disabled auto-recovery for the entire
    process lifetime and only a manual restart brought the feed back (the
    2026-08-03/04 production outage). After a quiet spell the budget must re-arm.
    """
    feed = _make_feed()
    login_calls = {"n": 0}

    class FakeSession:
        async def login(self, force: bool = False):
            login_calls["n"] += 1
            return None

        # Broker-truth markers used by _self_heal_auth (see F4).
        def mark_broker_ok(self) -> None: ...
        def mark_broker_rejected(self, reason: str = "") -> None: ...

    orig = ws_client.get_session_manager
    ws_client.get_session_manager = lambda: FakeSession()  # type: ignore[assignment]
    try:
        for _ in range(AUTO_RELOGIN_MAX_ATTEMPTS + 3):
            feed._last_auto_relogin = time.time() - (AUTO_RELOGIN_COOLDOWN_S + 1)
            await feed._self_heal_auth()
        assert login_calls["n"] == AUTO_RELOGIN_MAX_ATTEMPTS
        assert feed._auto_relogin_attempts == AUTO_RELOGIN_MAX_ATTEMPTS

        # Now go quiet past the decay window — the next failure must try again.
        feed._last_auto_relogin = time.time() - (AUTO_RELOGIN_ATTEMPT_DECAY_S + 1)
        await feed._self_heal_auth()
        assert login_calls["n"] == AUTO_RELOGIN_MAX_ATTEMPTS + 1, (
            "self-heal must re-arm after a quiet period instead of latching off "
            "for the life of the process"
        )
        assert feed._auto_relogin_attempts == 1
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
