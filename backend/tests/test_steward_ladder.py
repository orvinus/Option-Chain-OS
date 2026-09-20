"""Regression tests for the SessionSteward's escalation ladder + rotation schedule.

The old watchdog's ladder had three defects observed live during outage #2:
its force_relogin rung was mathematically unreachable (escalations gated to
every 4th 30s check always landed on odd failure counts, and the rung required
even), it logged ``master_rows=0`` as a successful universe refresh, and when
the refresh RAISED it skipped the reconnect nudge entirely. The steward drives
an explicit step index and validates each rung's outcome.

Runnable without pytest:  PYTHONPATH=. python tests/test_steward_ladder.py
"""
from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta, timezone

from app.auth.market_session import LoginCircuitOpen
from app.ingest import session_steward as st_mod
from app.ingest.session_steward import SessionSteward


class FakeSession:
    def __init__(self) -> None:
        self.authenticated = True
        self.circuit_state = "closed"
        self.last_login_at = datetime.now(timezone.utc) - timedelta(hours=1)
        self.rotations = 0
        self.healthy_marks = 0
        self.parked_reasons: list[str] = []
        self.raise_circuit_open = False

    async def login(self, force=False, manual=False, actor="unknown"):
        if self.raise_circuit_open:
            raise LoginCircuitOpen("test circuit open")
        self.rotations += 1
        self.last_login_at = datetime.now(timezone.utc)

        class _T:
            issued_at = self.last_login_at

        return _T()

    def mark_feed_healthy(self):
        self.healthy_marks += 1

    def park_rotations(self, reason: str):
        self.circuit_state = "open"
        self.parked_reasons.append(reason)

    @property
    def tokens(self):
        class _T:
            issued_at = self.last_login_at

        return _T()


class FakeFeed:
    def __init__(self) -> None:
        self.supervisor_alive = True
        self.is_feed_connected = False
        self.nudges = 0

    def nudge_reconnect(self):
        self.nudges += 1


class Harness:
    """Steward wired to fakes, with verification stubbed to a scripted outcome."""

    def __init__(self) -> None:
        self.steward = SessionSteward()
        self.sess = FakeSession()
        self.feed = FakeFeed()
        self.rebuilds = 0
        self.scripmaster_rows: list | Exception = [1, 2, 3]
        self.verify_result = False
        self.actions: list[str] = []

        from app.runtime import get_runtime

        self.rt = get_runtime()
        self._prev_feed = self.rt.feed_client
        self.rt.feed_client = self.feed

        self._orig_get_sess = st_mod.get_session_manager
        st_mod.get_session_manager = lambda: self.sess  # type: ignore[assignment]

        async def fake_verify() -> bool:
            return self.verify_result

        self.steward._verify_rotation = fake_verify  # type: ignore[assignment]

    async def escalate_once(self) -> None:
        # Bypass the 90s gate the way the loop's cadence would eventually.
        await self.steward._escalate(force_gate=True)

    def record_ladder_labels(self) -> list[str]:
        return self.actions

    def close(self) -> None:
        st_mod.get_session_manager = self._orig_get_sess  # type: ignore[assignment]
        self.rt.feed_client = self._prev_feed


# --------------------------------------------------------------------------- 1
async def test_ladder_reaches_every_rung_including_rotation() -> None:
    """THE parity regression: the old ladder could never reach its relogin rung.
    Drive four escalations and demand kick -> rotate -> universe -> rebuild."""
    h = Harness()

    async def fake_scrip(symbol, force_refresh=False):
        return [1, 2, 3]

    import app.market.scripmaster as sm

    orig_scrip = sm.get_scripmaster
    sm.get_scripmaster = fake_scrip  # type: ignore[assignment]

    import app.ingest.feed_factory as ff

    async def fake_rebuild(reason):
        h.rebuilds += 1
        return h.feed

    orig_rebuild = ff.rebuild_feed_client
    ff.rebuild_feed_client = fake_rebuild  # type: ignore[assignment]
    try:
        h.steward._open_episode()
        await h.escalate_once()  # step 0: kick
        assert h.feed.nudges == 1 and h.sess.rotations == 0
        await h.escalate_once()  # step 1: rotate — unreachable in the old code
        assert h.sess.rotations == 1, "the rotation rung must actually rotate"
        await h.escalate_once()  # step 2: universe (+ nudge in finally)
        assert h.feed.nudges >= 2
        await h.escalate_once()  # step 3: rebuild
        assert h.rebuilds == 1, "the ladder must reach the client rebuild"
        await h.escalate_once()  # wraps to rotate, never back to kick
        assert h.sess.rotations == 2
    finally:
        sm.get_scripmaster = orig_scrip  # type: ignore[assignment]
        ff.rebuild_feed_client = orig_rebuild  # type: ignore[assignment]
        h.close()


# --------------------------------------------------------------------------- 2
async def test_scripmaster_exception_still_kicks() -> None:
    """Old bug: a raising universe refresh skipped the nudge entirely."""
    h = Harness()
    import app.market.scripmaster as sm

    async def raising_scrip(symbol, force_refresh=False):
        raise RuntimeError("XTS instruments/master returned no F&O rows")

    orig = sm.get_scripmaster
    sm.get_scripmaster = raising_scrip  # type: ignore[assignment]
    try:
        h.steward._open_episode()
        h.steward._step = 2
        nudges_before = h.feed.nudges
        await h.escalate_once()
        assert h.feed.nudges == nudges_before + 1, (
            "the reconnect kick must run even when the refresh raises (finally)"
        )
        assert h.steward._step == 3
    finally:
        sm.get_scripmaster = orig  # type: ignore[assignment]
        h.close()


# --------------------------------------------------------------------------- 3
async def test_circuit_open_parks_the_ladder_without_suicide() -> None:
    h = Harness()
    h.sess.raise_circuit_open = True
    h.sess.circuit_state = "open"
    try:
        h.steward._open_episode()
        h.steward._step = 1
        await h.escalate_once()
        assert h.steward._step == 1, "a parked rotation must not advance the ladder"
        # Suicide must be off while parked, no matter how long it has been.
        h.steward._unhealthy_since = asyncio.get_event_loop().time() - 99999
        h.steward._episode_had_rebuild = True
        assert h.steward._should_exit() is False, (
            "os._exit while the circuit is open restarts into the same wall — "
            "and rebuilds the storm the circuit exists to stop"
        )
    finally:
        h.close()


# --------------------------------------------------------------------------- 4
async def test_dead_token_skips_the_kick() -> None:
    h = Harness()
    h.sess.authenticated = False
    try:
        h.steward._open_episode()
        await h.escalate_once()
        assert h.sess.rotations == 1, "kicking a dead token is pointless — rotate first"
        assert h.steward._step == 2, "the ladder skipped straight past the kick rung"
        # (the nudge that DID happen came from the rotation minting a fresh token —
        # that one is correct and required)
    finally:
        h.close()


# --------------------------------------------------------------------------- 5
async def test_suicide_gate_requires_all_four_conditions() -> None:
    h = Harness()
    try:
        loop_now = asyncio.get_event_loop().time()
        in_window = st_mod._within_watch_window() and not st_mod._in_warmup()
        # Not unhealthy at all:
        assert h.steward._should_exit() is False
        # Unhealthy long enough but no rebuild tried:
        h.steward._unhealthy_since = loop_now - 700
        h.steward._episode_had_rebuild = False
        assert h.steward._should_exit() is False
        # Rebuild tried but not unhealthy long enough:
        h.steward._unhealthy_since = loop_now - 60
        h.steward._episode_had_rebuild = True
        assert h.steward._should_exit() is False
        # All in-process conditions met -> verdict now depends ONLY on the clock.
        h.steward._unhealthy_since = loop_now - 700
        assert h.steward._should_exit() is in_window
    finally:
        h.close()


# --------------------------------------------------------------------------- 6
async def test_second_client_detector_parks_rotations() -> None:
    h = Harness()
    try:
        loop = asyncio.get_event_loop()
        for _ in range(st_mod.SECOND_CLIENT_DEATHS):
            h.steward._last_verify_ok_at = loop.time() - 10  # verified 10s ago...
            h.steward._note_possible_post_verify_death()      # ...and died
        assert h.sess.circuit_state == "open"
        assert any("second client" in r for r in h.sess.parked_reasons)
    finally:
        h.close()


# --------------------------------------------------------------------------- 7
async def test_slow_deaths_do_not_trip_second_client_detector() -> None:
    """The ~83s gateway cycle and ordinary drops must never look like a thief."""
    h = Harness()
    try:
        loop = asyncio.get_event_loop()
        for _ in range(10):
            h.steward._last_verify_ok_at = loop.time() - 600  # died 10 min after verify
            h.steward._note_possible_post_verify_death()
        assert h.sess.circuit_state == "closed"
    finally:
        h.close()


# --------------------------------------------------------------------------- 8
async def test_daily_rotation_fires_once_in_preopen_window() -> None:
    h = Harness()
    rotations: list[str] = []

    async def fake_rotate(reason: str) -> str:
        rotations.append(reason)
        h.steward._last_rotation_ist_date = st_mod.now_ist().date()
        return "ok"

    h.steward._rotate = fake_rotate  # type: ignore[assignment]
    orig_now = st_mod.now_ist
    try:
        from app.core.time_utils import IST

        # 08:40 IST on a Wednesday — inside [08:35, open).
        fake_t = IST.localize(datetime.combine(datetime(2026, 8, 5), time(8, 40)))
        st_mod.now_ist = lambda: fake_t  # type: ignore[assignment]
        await h.steward._maybe_scheduled_rotation()
        assert rotations == ["daily_preopen"]
        await h.steward._maybe_scheduled_rotation()
        assert rotations == ["daily_preopen"], "must fire once per IST date"
        # 08:20 — before the configured instant: nothing (fresh steward state).
        h2 = SessionSteward()
        h2._rotate = fake_rotate  # type: ignore[assignment]
        fake_t = IST.localize(datetime.combine(datetime(2026, 8, 5), time(8, 20)))
        st_mod.now_ist = lambda: fake_t  # type: ignore[assignment]
        h.sess.last_login_at = datetime.now(timezone.utc) - timedelta(hours=1)
        await h2._maybe_scheduled_rotation()
        assert rotations == ["daily_preopen"]
    finally:
        st_mod.now_ist = orig_now  # type: ignore[assignment]
        h.close()


# --------------------------------------------------------------------------- 9
async def test_ttl_backstop_rotates_old_token_any_hour() -> None:
    h = Harness()
    rotations: list[str] = []

    async def fake_rotate(reason: str) -> str:
        rotations.append(reason)
        return "ok"

    h.steward._rotate = fake_rotate  # type: ignore[assignment]
    orig_now = st_mod.now_ist
    try:
        from app.core.time_utils import IST

        # 20:00 IST (evening — outside any window): a 23h token must still rotate.
        fake_t = IST.localize(datetime.combine(datetime(2026, 8, 5), time(20, 0)))
        st_mod.now_ist = lambda: fake_t  # type: ignore[assignment]
        h.sess.last_login_at = datetime.now(timezone.utc) - timedelta(hours=23)
        await h.steward._maybe_scheduled_rotation()
        assert rotations == ["ttl_backstop"]
    finally:
        st_mod.now_ist = orig_now  # type: ignore[assignment]
        h.close()


# --------------------------------------------------------------------------- 10
async def test_recovery_request_wakes_the_loop() -> None:
    h = Harness()
    try:
        t0 = asyncio.get_event_loop().time()
        sleeper = asyncio.create_task(h.steward._sleep_or_request(30.0))
        await asyncio.sleep(0.05)
        h.steward.request_recovery("test")
        await asyncio.wait_for(sleeper, timeout=2.0)
        assert asyncio.get_event_loop().time() - t0 < 2.0, (
            "a recovery request must wake the loop, not wait out the 30s check"
        )
    finally:
        h.close()


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
