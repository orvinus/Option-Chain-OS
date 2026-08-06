"""Regression tests for the XTS feed self-heal / auto-recovery layer.

These guard the "feed must never silently die" behaviour added 2026-06-17:
  * ``_classify_400`` distinguishes a benign "already subscribed" 400 from a
    genuine auth rejection ('Invalid Token').
  * ``MarketDataSession.login`` enforces the rotation floor (LOGIN_FLOOR_S):
    within it every non-manual login — forced or not — reuses the token, so
    the single-session token cannot be thrashed by racing recovery actors.
  * ``OptionFeedClient._request_auth_recovery`` REPORTS auth trouble to the
    SessionSteward (throttled) and never rotates the token itself — the feed
    client is a demoted actor under the single-rotation-authority design.

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
    AUTH_RECOVERY_REQUEST_COOLDOWN_S,
    AUTH_RECOVERY_REQUESTS_BEFORE_REJECTED,
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


async def test_auth_trouble_requests_recovery_never_logs_in() -> None:
    """DEMOTION regression: the feed client must never rotate the token itself.
    During outage #2 its self-heal was one of five independent rotation actors;
    now it only reports to the steward."""
    feed = _make_feed()
    login_calls = {"n": 0}
    requests: list[str] = []

    class FakeSession:
        async def login(self, force: bool = False, manual: bool = False, actor: str = ""):
            login_calls["n"] += 1
            return None

        def mark_broker_ok(self) -> None: ...
        def mark_broker_rejected(self, reason: str = "") -> None: ...

    class FakeSteward:
        def request_recovery(self, reason: str) -> None:
            requests.append(reason)

    from app.ingest import session_steward as st_mod

    orig_sess = ws_client.get_session_manager
    orig_steward = st_mod._steward
    ws_client.get_session_manager = lambda: FakeSession()  # type: ignore[assignment]
    st_mod._steward = FakeSteward()  # type: ignore[assignment]
    try:
        await feed._request_auth_recovery("test failure")
        assert login_calls["n"] == 0, (
            "the feed client logged in directly — the single-rotation-authority "
            "invariant is broken and the login storm can return"
        )
        assert len(requests) == 1 and "test failure" in requests[0]
    finally:
        ws_client.get_session_manager = orig_sess  # type: ignore[assignment]
        st_mod._steward = orig_steward


async def test_auth_recovery_requests_are_throttled() -> None:
    feed = _make_feed()
    requests: list[str] = []

    class FakeSession:
        def mark_broker_ok(self) -> None: ...
        def mark_broker_rejected(self, reason: str = "") -> None: ...

    class FakeSteward:
        def request_recovery(self, reason: str) -> None:
            requests.append(reason)

    from app.ingest import session_steward as st_mod

    orig_sess = ws_client.get_session_manager
    orig_steward = st_mod._steward
    ws_client.get_session_manager = lambda: FakeSession()  # type: ignore[assignment]
    st_mod._steward = FakeSteward()  # type: ignore[assignment]
    try:
        await feed._request_auth_recovery("a")
        await feed._request_auth_recovery("b")  # inside the cooldown -> dropped
        assert len(requests) == 1, "a tight reconnect loop must not spam the steward"
        feed._last_auth_request = time.time() - (AUTH_RECOVERY_REQUEST_COOLDOWN_S + 1)
        await feed._request_auth_recovery("c")
        assert len(requests) == 2
    finally:
        ws_client.get_session_manager = orig_sess  # type: ignore[assignment]
        st_mod._steward = orig_steward


async def test_repeated_unanswered_requests_mark_broker_rejected() -> None:
    """Health honesty: after N unanswered recovery requests, authenticated must
    stop reading true — but WITHOUT any actor auto-rotating off that flag."""
    feed = _make_feed()
    rejected: list[str] = []

    class FakeSession:
        def mark_broker_ok(self) -> None: ...

        def mark_broker_rejected(self, reason: str = "") -> None:
            rejected.append(reason)

    class FakeSteward:
        def request_recovery(self, reason: str) -> None: ...

    from app.ingest import session_steward as st_mod

    orig_sess = ws_client.get_session_manager
    orig_steward = st_mod._steward
    ws_client.get_session_manager = lambda: FakeSession()  # type: ignore[assignment]
    st_mod._steward = FakeSteward()  # type: ignore[assignment]
    try:
        for _ in range(AUTH_RECOVERY_REQUESTS_BEFORE_REJECTED):
            feed._last_auth_request = time.time() - (AUTH_RECOVERY_REQUEST_COOLDOWN_S + 1)
            await feed._request_auth_recovery("still broken")
        assert rejected, "health must be told the session is not answering"
    finally:
        ws_client.get_session_manager = orig_sess  # type: ignore[assignment]
        st_mod._steward = orig_steward


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
