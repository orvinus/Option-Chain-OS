"""TrueData relay — frame round-trip, hub fan-out, tee ordering, route auth.

Runnable without pytest: ``PYTHONPATH=. python tests/test_td_relay.py``.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.ingest.types import Tick  # noqa: E402
from app.ws import td_relay  # noqa: E402
from app.ws.td_relay import RelayHub, TeeQueue, frame_to_tick, router, tick_to_frame  # noqa: E402


def _tick(**over) -> Tick:
    base = dict(
        ts=datetime(2026, 9, 2, 10, 0, 5, tzinfo=timezone.utc),
        token="td:NIFTY:260908:24000:CE",
        symbol="NIFTY", expiry=date(2026, 9, 8), strike=24000, option_type="CE",
        ltp=83.2, oi=123456, volume=789, underlying=23914.45,
        origin="ws", vendor="truedata",
        vendor_ts=datetime(2026, 9, 2, 10, 0, 4, 500000, tzinfo=timezone.utc),
    )
    base.update(over)
    return Tick(**base)


def test_frame_roundtrip_preserves_every_persisted_field():
    t = _tick()
    back = frame_to_tick(json.loads(json.dumps(tick_to_frame(t))))
    for f in ("ts", "token", "symbol", "expiry", "strike", "option_type", "ltp", "oi", "volume", "underlying", "vendor_ts"):
        assert getattr(back, f) == getattr(t, f), f
    # The follower stamps the transport itself — never taken from the wire.
    assert back.origin == "ws" and back.vendor == "truedata"


def test_frame_roundtrip_keeps_ltp_none_as_none():
    back = frame_to_tick(tick_to_frame(_tick(ltp=None, vendor_ts=None)))
    assert back.ltp is None and back.vendor_ts is None


def test_hub_fans_out_to_every_follower_and_drops_oldest():
    async def run():
        hub = RelayHub()
        assert hub.client_count == 0
        hub.publish(_tick())            # no clients: nothing queued, nothing counted as dropped
        assert hub.published == 0
        a_id, a_q = hub.subscribe()
        b_id, b_q = hub.subscribe()
        hub.publish(_tick(strike=24000))
        hub.publish(_tick(strike=24050))
        assert a_q.qsize() == 2 and b_q.qsize() == 2
        # Fill one follower past its cap: the OLDEST frame must give way.
        for k in range(td_relay.CLIENT_QUEUE_MAX + 3):
            hub.publish(_tick(strike=25000 + k))
        assert a_q.qsize() == td_relay.CLIENT_QUEUE_MAX
        first = json.loads(a_q.get_nowait())
        assert first["k"] != 24000, "oldest frame should have been evicted"
        assert hub.dropped >= 3
        hub.unsubscribe(a_id)
        hub.unsubscribe(b_id)
        assert hub.client_count == 0
    asyncio.run(run())


def test_tee_queue_writes_local_first_and_still_publishes_when_local_is_full():
    async def run():
        hub = RelayHub()
        _cid, q = hub.subscribe()
        inner: asyncio.Queue = asyncio.Queue(maxsize=1)
        tee = TeeQueue(inner, hub)
        tee.put_nowait(_tick(strike=1))
        assert inner.qsize() == 1 and q.qsize() == 1
        try:
            tee.put_nowait(_tick(strike=2))
            raise AssertionError("expected QueueFull to propagate to the owner's feed")
        except asyncio.QueueFull:
            pass
        # The follower still got the frame the owner could not store.
        assert q.qsize() == 2
    asyncio.run(run())


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


def test_route_refuses_when_disabled(monkeypatch=None):
    _set = _setter(monkeypatch)
    _set("td_relay_enabled", False)
    with TestClient(_app()) as c:
        with c.websocket_connect("/ws/td-relay?key=x") as ws:
            msg = ws.receive_json()
            assert msg["type"] == "error" and "disabled" in msg["message"]


def test_route_refuses_bad_key(monkeypatch=None):
    _set = _setter(monkeypatch)
    _set("td_relay_enabled", True)
    _set("td_relay_key", "s3cret")
    with TestClient(_app()) as c:
        with c.websocket_connect("/ws/td-relay?key=wrong") as ws:
            msg = ws.receive_json()
            assert msg["type"] == "error" and "key" in msg["message"]


def test_route_streams_hello_then_ticks_with_good_key(monkeypatch=None):
    _set = _setter(monkeypatch)
    _set("td_relay_enabled", True)
    _set("td_relay_key", "s3cret")
    hub = td_relay.get_relay_hub()
    with TestClient(_app()) as c:
        with c.websocket_connect("/ws/td-relay?key=s3cret") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello" and hub.client_count == 1
            hub.publish(_tick(strike=24100))
            frame = ws.receive_json()
            assert frame["type"] == "tick" and frame["k"] == 24100
            back = frame_to_tick(frame)
            assert back.token == "td:NIFTY:260908:24000:CE"
    assert hub.client_count == 0, "disconnect must unsubscribe"


# --------------------------------------------------------------- harness
_saved: dict[str, object] = {}


def _setter(monkeypatch):
    """Use pytest's monkeypatch when present; otherwise mutate + restore manually."""
    if monkeypatch is not None:
        return lambda k, v: monkeypatch.setattr(settings, k, v, raising=False)

    def _s(k, v):
        _saved.setdefault(k, getattr(settings, k, None))
        object.__setattr__(settings, k, v)
    return _s


if __name__ == "__main__":
    import traceback
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception:
                fails += 1
                print("FAIL", name)
                traceback.print_exc()
    for k, v in _saved.items():
        object.__setattr__(settings, k, v)
    sys.exit(1 if fails else 0)
