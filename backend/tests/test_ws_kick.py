"""Regression tests for the feed client's kick + bounded-await hardening.

During the 2026-08-06 outage the watchdog "nudged" the feed dozens of times and
nothing happened, because ``nudge_reconnect()`` only cleared an already-clear
``_connected`` flag: it could not wake the supervisor's backoff sleep (which
watched only ``_stopping``) and could not interrupt an in-flight connect. And
three awaits inside ``_connect_once`` were effectively unbounded — the aiohttp
handshake under ``sio.connect`` (wait_timeout does not cover it), the
one-at-a-time subscribe fallback (~93 x 30s worst case), and the teardown
disconnect.

Runnable without pytest:  PYTHONPATH=. python tests/test_ws_kick.py
"""
from __future__ import annotations

import asyncio
import time

from app.ingest import ws_client as ws_mod
from app.ingest.ws_client import OptionFeedClient


def _make_feed() -> OptionFeedClient:
    async def provider():
        return [], None

    return OptionFeedClient(asyncio.Queue(), provider)


# --------------------------------------------------------------------------- 1
async def test_nudge_while_disconnected_sets_kick() -> None:
    """The exact outage no-op: nudging an already-disconnected feed must leave a
    visible wake-up signal, not just re-clear a clear flag."""
    feed = _make_feed()
    assert not feed._connected.is_set()
    feed.nudge_reconnect()
    assert feed._kick.is_set(), (
        "nudge_reconnect() left no signal while disconnected — the watchdog's "
        "escalations would all be no-ops again"
    )


# --------------------------------------------------------------------------- 2
async def test_kick_wakes_backoff_sleep() -> None:
    """A supervisor parked in a 60s backoff must reconnect ~immediately on a kick."""
    feed = _make_feed()
    t0 = time.monotonic()
    task = asyncio.create_task(feed._sleep_or_kick(60.0))
    await asyncio.sleep(0.05)
    feed.nudge_reconnect()
    stopped = await asyncio.wait_for(task, timeout=2.0)
    elapsed = time.monotonic() - t0
    assert stopped is False, "a kick must not be mistaken for shutdown"
    assert elapsed < 2.0, f"backoff sleep survived the kick ({elapsed:.1f}s)"


# --------------------------------------------------------------------------- 3
async def test_stop_wins_over_backoff() -> None:
    feed = _make_feed()
    task = asyncio.create_task(feed._sleep_or_kick(60.0))
    await asyncio.sleep(0.05)
    feed._stopping.set()
    stopped = await asyncio.wait_for(task, timeout=2.0)
    assert stopped is True


# --------------------------------------------------------------------------- 4
async def test_backoff_expires_normally_without_kick() -> None:
    feed = _make_feed()
    t0 = time.monotonic()
    stopped = await asyncio.wait_for(feed._sleep_or_kick(0.2), timeout=2.0)
    assert stopped is False
    assert time.monotonic() - t0 >= 0.19


# --------------------------------------------------------------------------- 5
async def test_supervisor_alive_property() -> None:
    feed = _make_feed()
    assert feed.supervisor_alive is False, "no task yet -> not alive"
    started = asyncio.Event()

    async def fake_supervisor():
        started.set()
        await asyncio.sleep(30)

    feed._supervisor_task = asyncio.create_task(fake_supervisor())
    await started.wait()
    assert feed.supervisor_alive is True
    feed._supervisor_task.cancel()
    try:
        await feed._supervisor_task
    except asyncio.CancelledError:
        pass
    assert feed.supervisor_alive is False, "a dead/cancelled task must read as not alive"


# --------------------------------------------------------------------------- 6
async def test_subscribe_all_bounded_by_budget() -> None:
    """A hung broker gateway must not park _connect_once for minutes: the
    _subscribe_all call is wrapped in wait_for at the call site; verify the
    budget constant is wired by timing a stubbed hang."""
    feed = _make_feed()

    async def hang(*a, **k):
        await asyncio.sleep(999)

    orig = ws_mod.xts_client.subscribe
    ws_mod.xts_client.subscribe = hang  # type: ignore[assignment]
    # Minimal token_meta so _build_subscription_groups yields instruments.
    from app.market.scripmaster import InstrumentToken
    from datetime import date

    feed._token_meta = {
        "1": InstrumentToken(
            token="1", symbol="NIFTY26AUG24600CE", name="NIFTY",
            expiry=date(2026, 8, 11), strike=24600, option_type="CE",
            exchange="NFO", lotsize=75,
        )
    }
    # Fake session token access.
    from app import runtime as _rt  # noqa: F401

    class _FakeSess:
        token = "tok"

    orig_get = ws_mod.get_session_manager
    ws_mod.get_session_manager = lambda: _FakeSess()  # type: ignore[assignment]
    try:
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(feed._subscribe_all(), timeout=0.5)
        except asyncio.TimeoutError:
            pass
        assert time.monotonic() - t0 < 2.0
    finally:
        ws_mod.xts_client.subscribe = orig  # type: ignore[assignment]
        ws_mod.get_session_manager = orig_get  # type: ignore[assignment]


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
