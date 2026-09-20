"""Tests for /api/health/strict — the machine-facing probe, and health-v2 compat.

The old /api/health returned 200 {"status":"ok"} through 13+ hours of dead feed
in both August outages: no status-code monitor could ever have noticed. Strict
exists for machines (the VPS oi-sentinel, external monitors), and its body is a
CONTRACT: restart_recommended must be false for every cause a backend restart
cannot fix, or the sentinel restart-loops against broker/DB problems.

Runnable without pytest:  PYTHONPATH=. python tests/test_health_strict.py
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from app.api import health as health_mod
from app.runtime import get_runtime


class _State:
    """Snapshot/restore the runtime + patch points around each test."""

    def __init__(self, db_ok=True, circuit="closed", session_open=True, holiday=False):
        self.rt = get_runtime()
        self._saved = (self.rt.last_flush_at, self.rt.steward)
        self._orig = (
            health_mod._db_ok,
            health_mod.is_nse_regular_session_open,
            health_mod.is_nse_holiday,
            health_mod.get_session_manager,
        )

        async def fake_db_ok():
            return db_ok

        class FakeSession:
            circuit_state = circuit
            circuit_reason = "test reason" if circuit == "open" else ""
            authenticated = True
            last_login_at = None
            logins_last_hour = 0

        health_mod._db_ok = fake_db_ok
        health_mod.is_nse_regular_session_open = lambda: session_open
        health_mod.is_nse_holiday = lambda d: holiday
        health_mod.get_session_manager = lambda: FakeSession()

    def restore(self):
        self.rt.last_flush_at, self.rt.steward = self._saved
        (
            health_mod._db_ok,
            health_mod.is_nse_regular_session_open,
            health_mod.is_nse_holiday,
            health_mod.get_session_manager,
        ) = self._orig


def _body(resp) -> dict:
    return json.loads(bytes(resp.body))


# --------------------------------------------------------------------------- 1
async def test_strict_503_when_stale_during_session_recommends_restart() -> None:
    s = _State()
    try:
        s.rt.last_flush_at = datetime.now(timezone.utc) - timedelta(hours=2)
        resp = await health_mod.health_strict()
        assert resp.status_code == 503
        b = _body(resp)
        assert "stale_data" in b["reasons"]
        assert b["restart_recommended"] is True, (
            "pure staleness with DB up and circuit closed is the one case a "
            "restart can fix — the sentinel must be told so"
        )
    finally:
        s.restore()


# --------------------------------------------------------------------------- 2
async def test_strict_stale_with_circuit_open_forbids_restart() -> None:
    s = _State(circuit="open")
    try:
        s.rt.last_flush_at = datetime.now(timezone.utc) - timedelta(hours=2)
        resp = await health_mod.health_strict()
        assert resp.status_code == 503
        b = _body(resp)
        assert b["restart_recommended"] is False, (
            "restarting into an open circuit resumes the login storm the "
            "circuit exists to stop — the sentinel must never do it"
        )
        assert "recovery_circuit_open" in b["reasons"]
    finally:
        s.restore()


# --------------------------------------------------------------------------- 3
async def test_strict_db_down_forbids_restart() -> None:
    s = _State(db_ok=False)
    try:
        s.rt.last_flush_at = datetime.now(timezone.utc) - timedelta(hours=2)
        resp = await health_mod.health_strict()
        assert resp.status_code == 503
        b = _body(resp)
        assert "db_down" in b["reasons"]
        assert b["restart_recommended"] is False, "a backend restart cannot fix the DB"
    finally:
        s.restore()


# --------------------------------------------------------------------------- 4
async def test_strict_200_when_stale_off_session() -> None:
    """Evening staleness is the market being closed, not an emergency."""
    s = _State(session_open=False)
    try:
        s.rt.last_flush_at = datetime.now(timezone.utc) - timedelta(hours=13)
        resp = await health_mod.health_strict()
        assert resp.status_code == 200
    finally:
        s.restore()


# --------------------------------------------------------------------------- 5
async def test_strict_holiday_returns_200_with_note() -> None:
    s = _State(holiday=True)
    try:
        s.rt.last_flush_at = None  # would be "stale" on a trading day
        resp = await health_mod.health_strict()
        assert resp.status_code == 200
        assert _body(resp).get("note") == "nse_holiday"
    finally:
        s.restore()


# --------------------------------------------------------------------------- 6
async def test_strict_fresh_data_via_failover_is_200_but_says_degraded() -> None:
    """REST failover keeping data fresh must NOT trigger sentinel restarts —
    but must not read as fully healthy either."""
    s = _State()

    class FakeSteward:
        ws_feed_healthy = False
        last_check_at = None

    try:
        s.rt.last_flush_at = datetime.now(timezone.utc)  # fresh (poller rows)
        s.rt.steward = FakeSteward()
        resp = await health_mod.health_strict()
        assert resp.status_code == 200, "a successfully-limping process must not be restarted"
        assert _body(resp).get("degraded") == "rest_failover"
    finally:
        s.restore()


# --------------------------------------------------------------------------- 7
async def test_basic_health_keeps_backward_compatible_shape() -> None:
    """Every pre-existing consumer field must still be present with 200-safety."""
    s = _State()
    try:
        res = await health_mod.health()
        d = res.model_dump()
        for key in (
            "status", "authenticated", "latest_spot", "tokens_subscribed",
            "last_flush_at", "expiries", "run_mode", "now_ist", "nse_session_open",
            "session_open_ist", "session_close_ist", "feed_connected",
            "active_symbol", "poller_enabled", "poller_last_sweep_at",
            "poller_last_ticks",
        ):
            assert key in d, f"pre-existing health field '{key}' disappeared"
        for key in (
            "last_ws_flush_at", "supervisor_alive", "circuit_state", "db_ok",
            "watchdog_last_check_at", "live_subscriptions", "poller_mode",
        ):
            assert key in d, f"v2 health field '{key}' missing"
        assert d["status"] == "ok"
    finally:
        s.restore()


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
