"""TrueData relay — ONE vendor socket, MANY consumers.

TrueData allows exactly one realtime login per user on its push port (a second
login is rejected with "User Already Connected" and the first session survives).
That makes it impossible to run the VPS and a dev PC — or two backends of any
kind — against the vendor at the same time.

This module turns the process that DOES hold the vendor socket (the "owner",
normally the VPS backend with ``FEED_VENDOR=truedata`` + ``TD_RELAY_ENABLED=true``)
into a fan-out point:

* ``TeeQueue`` sits between the vendor feed client and the local tick queue. The
  feed client only ever calls ``put_nowait`` on its out-queue, so a duck-typed
  wrapper is enough: every ``Tick`` still lands on the owner's own queue exactly
  as before, and is ALSO mirrored into the ``RelayHub``.
* ``RelayHub`` keeps one bounded queue per connected follower (drop-oldest, never
  block — a slow follower must not slow the owner's ingest).
* ``/ws/td-relay?key=…`` streams those ticks as small JSON frames to any number
  of followers that present the shared ``TD_RELAY_KEY``.

Followers run ``FEED_VENDOR=td_relay`` (see ``ingest/td_relay_feed.py``): they
never touch the vendor, so the single-login limit is spent once, on the owner.

Frame wire format (one JSON object per text message):

    {"type":"hello","vendor":"truedata","active_symbol":"NIFTY","tokens":47}
    {"type":"tick","ts":"…+00:00","tok":"td:NIFTY:260908:24000:CE","sym":"NIFTY",
     "exp":"2026-09-08","k":24000,"ot":"CE","ltp":83.2,"oi":123,"vol":456,
     "und":23914.45,"vts":"…"|null}
    {"type":"ping"}            (every PING_S; the follower answers "pong")
    {"type":"error","message":"…"}

Design rules (keep them):
* The owner's own pipeline must be unaffected by relay state. Publishing is
  non-blocking and happens AFTER the local put, so a wedged follower can only
  ever lose its own frames.
* ``origin`` is NOT on the wire. The follower stamps ``origin="ws"`` itself,
  because that field drives the freshness ladder on the follower and must mean
  "arrived over the follower's live transport".
* Auth is a shared secret compared in constant time. The route is meant to sit
  behind the same TLS terminator as the rest of the API.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import time
from datetime import date, datetime
from typing import Any, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..core.config import settings
from ..core.logging import get_logger
from ..ingest.types import Tick
from ..runtime import get_runtime

log = get_logger("td_relay")

router = APIRouter()

# Per-follower buffer. At the vendor's ~50 contracts × a few frames per second a
# follower that stalls for ~30 s loses frames — acceptable; the alternative is
# unbounded memory on the owner.
CLIENT_QUEUE_MAX = 5000
PING_S = 15.0


# ------------------------------------------------------------------ frames
def tick_to_frame(t: Tick) -> dict[str, Any]:
    return {
        "type": "tick",
        "ts": t.ts.isoformat(),
        "tok": t.token,
        "sym": t.symbol,
        "exp": t.expiry.isoformat(),
        "k": int(t.strike),
        "ot": t.option_type,
        "ltp": t.ltp,
        "oi": int(t.oi),
        "vol": int(t.volume),
        "und": t.underlying,
        "vts": t.vendor_ts.isoformat() if t.vendor_ts else None,
    }


def frame_to_tick(f: dict[str, Any], *, vendor: str = "truedata") -> Tick:
    """Inverse of ``tick_to_frame``. ``origin`` is stamped by the FOLLOWER."""
    ltp = f.get("ltp")
    vts = f.get("vts")
    return Tick(
        ts=datetime.fromisoformat(f["ts"]),
        token=str(f["tok"]),
        symbol=str(f["sym"]),
        expiry=date.fromisoformat(f["exp"]),
        strike=int(f["k"]),
        option_type=str(f["ot"]),
        ltp=float(ltp) if ltp is not None else None,
        oi=int(f.get("oi") or 0),
        volume=int(f.get("vol") or 0),
        underlying=float(f["und"]) if f.get("und") is not None else None,
        origin="ws",
        vendor=vendor,
        vendor_ts=datetime.fromisoformat(vts) if vts else None,
    )


# --------------------------------------------------------------------- hub
class RelayHub:
    """Fan-out of encoded tick frames to N follower queues (drop-oldest)."""

    def __init__(self) -> None:
        self._clients: dict[int, asyncio.Queue[str]] = {}
        self._next_id = 0
        self.published = 0
        self.dropped = 0
        self.last_publish_at: float = 0.0

    def subscribe(self) -> tuple[int, "asyncio.Queue[str]"]:
        self._next_id += 1
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=CLIENT_QUEUE_MAX)
        self._clients[self._next_id] = q
        log.info("td_relay.subscribe", clients=len(self._clients))
        return self._next_id, q

    def unsubscribe(self, cid: int) -> None:
        self._clients.pop(cid, None)
        log.info("td_relay.unsubscribe", clients=len(self._clients))

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def publish(self, tick: Tick) -> None:
        if not self._clients:
            return
        frame = json.dumps(tick_to_frame(tick), separators=(",", ":"))
        self.published += 1
        self.last_publish_at = time.time()
        for q in list(self._clients.values()):
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:
                # Drop the OLDEST frame, keep the newest — a follower that
                # catches up wants the freshest book, not a replay of its lag.
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(frame)
                except asyncio.QueueFull:
                    pass
                self.dropped += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": settings.td_relay_enabled,
            "clients": self.client_count,
            "published": self.published,
            "dropped": self.dropped,
            "last_publish_at": (
                datetime.fromtimestamp(self.last_publish_at).isoformat()
                if self.last_publish_at else None
            ),
        }


_hub = RelayHub()


def get_relay_hub() -> RelayHub:
    return _hub


class TeeQueue:
    """Duck-typed out-queue for the vendor feed: local queue first, relay second.

    Only ``put_nowait`` is exercised by the feed clients; everything else is
    delegated so any incidental attribute access keeps working.
    """

    def __init__(self, inner: "asyncio.Queue[Tick]", hub: RelayHub) -> None:
        self._inner = inner
        self._hub = hub

    def put_nowait(self, tick: Tick) -> None:
        # Publish to followers even if the local queue is full — the owner's
        # QueueFull is the owner's problem (counted by the feed as a drop) and
        # must not silence every follower too.
        try:
            self._inner.put_nowait(tick)
        finally:
            self._hub.publish(tick)

    def __getattr__(self, name: str):  # pragma: no cover - passthrough
        return getattr(self._inner, name)


# ------------------------------------------------------------------- route
@router.websocket("/ws/td-relay")
async def td_relay_ws(websocket: WebSocket, key: str = "") -> None:
    # accept() FIRST — a close before accept surfaces to clients as a 403 with
    # no reason; after accept we can say why (same convention as oi_stream).
    await websocket.accept()

    if not settings.td_relay_enabled:
        await websocket.send_json({"type": "error", "message": "relay disabled on this backend"})
        await websocket.close(code=1013)
        return
    secret = settings.td_relay_key or ""
    if not secret or not hmac.compare_digest(key, secret):
        log.warning("td_relay.auth_failed", client=str(websocket.client))
        await websocket.send_json({"type": "error", "message": "bad relay key"})
        await websocket.close(code=1008)
        return

    hub = get_relay_hub()
    cid, q = hub.subscribe()
    rt = get_runtime()
    await websocket.send_json({
        "type": "hello",
        "vendor": settings.feed_vendor,
        "active_symbol": rt.active_symbol,
        "tokens": len(rt.tokens),
    })

    async def _sender() -> None:
        while True:
            frame = await q.get()
            await websocket.send_text(frame)

    async def _pinger() -> None:
        while True:
            await asyncio.sleep(PING_S)
            await websocket.send_json({"type": "ping"})

    sender = asyncio.create_task(_sender(), name=f"td-relay-send-{cid}")
    pinger = asyncio.create_task(_pinger(), name=f"td-relay-ping-{cid}")
    try:
        while True:
            # Followers only ever send "pong"; anything else is ignored.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001 - one client must never take the route down
        log.info("td_relay.client_error", error=str(e))
    finally:
        sender.cancel()
        pinger.cancel()
        hub.unsubscribe(cid)
