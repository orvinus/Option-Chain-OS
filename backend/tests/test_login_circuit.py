"""Regression tests for the broker-login circuit breaker (outage #2, 2026-08-06).

The outage was a login-storm livelock: independent recovery actors (frontend
auto-login, ws self-heal, watchdog, refresh loop) each rotated the single-session
XTS token out from under each other every ~6 seconds for 14.6 hours — each new
login invalidated the token the previous login's socket was using. The policy now
lives INSIDE MarketDataSession.login() where no caller can bypass it.

Runnable without pytest:  PYTHONPATH=. python tests/test_login_circuit.py
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import app.market_data.xts_client as xc
from app.auth.market_session import (
    STORM_ROTATIONS_LIMIT,
    LoginCircuitOpen,
    LoginRateLimited,
    MarketDataSession,
)
from app.core.config import settings


def _make_sess(counter: dict) -> MarketDataSession:
    sess = MarketDataSession()

    async def noop_persist(_tokens) -> None:
        return None

    async def noop_seed() -> None:
        return None

    sess._persist = noop_persist  # type: ignore[assignment]
    sess._seed_floor_from_db_once = noop_seed  # type: ignore[assignment]
    return sess


def _patch_login(counter: dict):
    async def fake_login() -> dict:
        counter["n"] += 1
        return {"token": f"tok-{counter['n']}", "userID": "TESTUSER"}

    orig = xc.login
    xc.login = fake_login  # type: ignore[assignment]
    return orig


def _age_out_floor(sess: MarketDataSession) -> None:
    sess._last_login_wall = datetime.now(timezone.utc) - timedelta(
        seconds=settings.login_floor_s + 1
    )


# --------------------------------------------------------------------------- 1
async def test_storm_three_actors_one_login() -> None:
    """THE F3 regression: three recovery actors racing to rotate within the same
    instant must produce exactly ONE broker login, all receiving the same token."""
    counter = {"n": 0}
    orig = _patch_login(counter)
    sess = _make_sess(counter)
    try:
        results = await asyncio.gather(
            sess.login(force=True, actor="steward"),
            sess.login(actor="self-heal"),
            sess.login(force=True, actor="api"),
        )
    finally:
        xc.login = orig  # type: ignore[assignment]
    assert counter["n"] == 1, (
        f"{counter['n']} broker logins for 3 concurrent recovery requests — "
        "the single-session token would be rotated out from under its own socket"
    )
    toks = {r.jwt_token for r in results}
    assert toks == {"tok-1"}, toks


# --------------------------------------------------------------------------- 2
async def test_floor_blocks_forced_rotation_but_not_manual() -> None:
    counter = {"n": 0}
    orig = _patch_login(counter)
    sess = _make_sess(counter)
    try:
        await sess.login()
        t = await sess.login(force=True, actor="watchdog")
        assert counter["n"] == 1 and t.jwt_token == "tok-1"
        t = await sess.login(force=True, manual=True, actor="human")
        assert counter["n"] == 2 and t.jwt_token == "tok-2"
    finally:
        xc.login = orig  # type: ignore[assignment]


# --------------------------------------------------------------------------- 3
async def test_burst_budget_opens_circuit() -> None:
    counter = {"n": 0}
    orig = _patch_login(counter)
    sess = _make_sess(counter)
    try:
        # Rewind both throttles between logins so only the BUDGET binds.
        for _ in range(settings.login_burst_max):
            _age_out_floor(sess)
            await sess.login(force=True)
        assert counter["n"] == settings.login_burst_max
        _age_out_floor(sess)
        raised = False
        try:
            await sess.login(force=True)
        except LoginRateLimited:
            raised = True
        assert raised, "exceeding the burst budget must raise, not rotate"
        assert sess.circuit_state == "open"
        assert counter["n"] == settings.login_burst_max, "no login past the budget"
    finally:
        xc.login = orig  # type: ignore[assignment]


# --------------------------------------------------------------------------- 4
async def test_storm_breaker_opens_after_unproductive_rotations() -> None:
    """Rotations that never produce a verified-healthy feed are the storm
    signature — the circuit must open on its own even under the rate floor."""
    counter = {"n": 0}
    orig = _patch_login(counter)
    sess = _make_sess(counter)
    try:
        for _ in range(STORM_ROTATIONS_LIMIT + 1):
            _age_out_floor(sess)
            sess._prune_login_times(time.monotonic())
            sess._login_times.clear()  # keep the budget out of the way
            await sess.login(force=True)
        assert sess.circuit_state == "open", (
            f"{STORM_ROTATIONS_LIMIT + 1} rotations with no mark_feed_healthy() "
            "must open the circuit"
        )
    finally:
        xc.login = orig  # type: ignore[assignment]


# --------------------------------------------------------------------------- 5
async def test_open_circuit_blocks_then_allows_probe() -> None:
    counter = {"n": 0}
    orig = _patch_login(counter)
    sess = _make_sess(counter)
    try:
        sess._open_circuit("test")
        sess._last_probe_mono = time.monotonic()  # a probe just happened
        _age_out_floor(sess)
        raised = False
        try:
            await sess.login(force=True)
        except LoginCircuitOpen:
            raised = True
        assert raised and counter["n"] == 0
        # Probe interval elapsed -> exactly one probe goes through.
        sess._last_probe_mono = time.monotonic() - 301.0
        await sess.login(force=True)
        assert counter["n"] == 1
    finally:
        xc.login = orig  # type: ignore[assignment]


# --------------------------------------------------------------------------- 6
async def test_manual_override_bypasses_open_circuit() -> None:
    counter = {"n": 0}
    orig = _patch_login(counter)
    sess = _make_sess(counter)
    try:
        sess._open_circuit("test")
        sess._last_probe_mono = time.monotonic()
        await sess.login(force=True, manual=True)
        assert counter["n"] == 1, "a human override must always be able to rotate"
    finally:
        xc.login = orig  # type: ignore[assignment]


# --------------------------------------------------------------------------- 7
async def test_mark_feed_healthy_closes_circuit_and_resets_counter() -> None:
    counter = {"n": 0}
    orig = _patch_login(counter)
    sess = _make_sess(counter)
    try:
        sess._rotations_since_healthy = 99
        sess._open_circuit("test")
        sess.mark_feed_healthy()
        assert sess.circuit_state == "closed"
        assert sess._rotations_since_healthy == 0
    finally:
        xc.login = orig  # type: ignore[assignment]


# --------------------------------------------------------------------------- 8
async def test_floor_does_not_apply_without_usable_token() -> None:
    """With no token there is nothing of ours a rotation would kill — a fresh
    boot must be able to log in immediately even if another process logged in
    seconds ago (its token is dead to us anyway if restore failed)."""
    counter = {"n": 0}
    orig = _patch_login(counter)
    sess = _make_sess(counter)
    try:
        sess._last_login_wall = datetime.now(timezone.utc)  # someone JUST logged in
        await sess.login(force=True, actor="startup")
        assert counter["n"] == 1
    finally:
        xc.login = orig  # type: ignore[assignment]


# --------------------------------------------------------------------------- 9
async def test_stale_bundle_hammering_causes_at_most_one_rotation() -> None:
    """A cached OLD frontend bundle still running the removed auto-login loop
    POSTs a forced login every ~5s. Server-side the floor must reduce three
    minutes of that to at most one rotation."""
    counter = {"n": 0}
    orig = _patch_login(counter)
    sess = _make_sess(counter)
    try:
        await sess.login()  # the session that exists when the old tab starts
        for _ in range(36):  # 3 minutes at one POST per 5s, time compressed
            await sess.login(force=True, actor="stale-bundle")
        assert counter["n"] == 1, (
            f"{counter['n']} rotations from a hammering stale bundle — the floor "
            "must absorb it"
        )
    finally:
        xc.login = orig  # type: ignore[assignment]


# --------------------------------------------------------------------------- runner
async def _main() -> int:
    import inspect

    tests = [
        v for k, v in sorted(globals().items())
        if k.startswith("test_") and inspect.iscoroutinefunction(v)
    ]
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
