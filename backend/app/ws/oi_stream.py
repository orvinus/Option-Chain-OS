"""WebSocket route ``/ws/oi-stream``.

Accepts query params:
    timeframe  -> default '5m'
    expiry     -> ISO date; default = first runtime expiry (or DB fallback)
    symbol     -> default = runtime.active_symbol

Behavior:
    1. Always accept() first so the browser never sees a 403/failed-upgrade.
    2. Send an error frame + close gracefully when warming up or bad params.
    3. On connect, immediately push the current snapshot (warm cache).
    4. Pump messages from the per-subscriber queue.
    5. Send a ping every 15s.
    6. ``set:tf=...,exp=...,sym=...`` swaps the subscriber's filter in place.
"""
from __future__ import annotations

import asyncio
from datetime import date

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import text

from ..core.db import AsyncSessionLocal
from ..core.logging import get_logger
from ..core.time_utils import TIMEFRAME_TO_DELTA
from ..market.symbols import get_registry
from ..runtime import get_runtime
from ..services import get_oi_engine, get_option_chain_full_engine
from .hub import Subscriber, _serialize_oi, _serialize_oc, get_hub

log = get_logger("ws_oi_stream")
router = APIRouter()

PING_INTERVAL_SECONDS = 15.0


async def _resolve_default_expiry(symbol: str) -> date | None:
    """Return the earliest available expiry for ``symbol``: runtime first, DB fallback."""
    rt = get_runtime()
    if symbol == rt.active_symbol and rt.expiries:
        return rt.expiries[0]
    try:
        async with AsyncSessionLocal() as s:
            row = await s.execute(
                text(
                    "SELECT MIN(expiry) FROM oi_snapshots_unified WHERE symbol = :sym"
                ),
                {"sym": symbol},
            )
            val = row.scalar()
        if val:
            return val
    except Exception as e:
        log.warning("ws.default_expiry.db_error", error=str(e))
    return None


@router.websocket("/ws/oi-stream")
async def oi_stream(
    websocket: WebSocket,
    timeframe: str = Query(default="5m"),
    expiry: str | None = Query(default=None),
    symbol: str | None = Query(default=None),
) -> None:
    # Always accept() FIRST — never close before accept (causes browser 403).
    await websocket.accept()

    if timeframe not in TIMEFRAME_TO_DELTA:
        await websocket.send_json({"type": "error", "message": f"Unsupported timeframe '{timeframe}'"})
        await websocket.close(code=1008)
        return

    rt = get_runtime()
    sym = (symbol or rt.active_symbol).upper()
    entry = get_registry().get(sym)
    if entry is None:
        await websocket.send_json({"type": "error", "message": f"Unknown symbol '{sym}'"})
        await websocket.close(code=1008)
        return
    if not entry.fno_eligible:
        await websocket.send_json({"type": "error", "message": "not_fno_eligible", "symbol": sym})
        await websocket.close(code=1013)
        return

    if expiry:
        try:
            exp_date = date.fromisoformat(expiry)
        except ValueError:
            await websocket.send_json({"type": "error", "message": f"Invalid expiry '{expiry}'"})
            await websocket.close(code=1008)
            return
    else:
        exp_date = await _resolve_default_expiry(sym)
        if exp_date is None:
            await websocket.send_json({"type": "error", "message": "warming_up", "detail": "No expiries available yet; ingestion is warming up."})
            await websocket.close(code=1013)
            return

    sub = Subscriber(ws=websocket, timeframe=timeframe, expiry=exp_date, symbol=sym)
    hub = get_hub()
    await hub.add(sub)

    sender_task = asyncio.create_task(_sender(sub), name=f"ws-sender-{id(sub)}")
    pinger_task = asyncio.create_task(_pinger(sub), name=f"ws-pinger-{id(sub)}")

    # Push an initial snapshot immediately so chart paints without waiting for first flush.
    try:
        live_spot = rt.latest_spot if sym == rt.active_symbol else None
        snap = await get_oi_engine().get(timeframe, exp_date, symbol=sym, live_spot=live_spot)
        await websocket.send_json({"type": "oi_change", "data": _serialize_oi(snap)})
    except Exception as e:
        log.warning("ws_oi_stream.initial_snapshot.error", error=str(e))
    try:
        live_spot = rt.latest_spot if sym == rt.active_symbol else None
        oc = await get_option_chain_full_engine().get(
            timeframe, exp_date, symbol=sym, live_spot=live_spot
        )
        await websocket.send_json({"type": "option_chain_full", "data": _serialize_oc(oc)})
    except Exception as e:
        log.warning("ws_oi_stream.initial_option_chain_full.error", error=str(e))

    try:
        while True:
            msg = await websocket.receive_text()
            if msg.strip() == "pong":
                continue
            if msg.startswith("set:"):
                await _handle_set(sub, msg[4:])
    except WebSocketDisconnect:
        log.info("ws_oi_stream.client_disconnected")
    except Exception as e:
        log.warning("ws_oi_stream.error", error=str(e))
    finally:
        sender_task.cancel()
        pinger_task.cancel()
        await hub.remove(sub)


async def _sender(sub: Subscriber) -> None:
    try:
        while True:
            msg = await sub.queue.get()
            await sub.ws.send_json(msg)
    except asyncio.CancelledError:
        return
    except Exception as e:
        log.warning("ws_sender.error", error=str(e))


async def _pinger(sub: Subscriber) -> None:
    try:
        while True:
            await asyncio.sleep(PING_INTERVAL_SECONDS)
            try:
                await sub.ws.send_json({"type": "ping"})
            except Exception:
                return
    except asyncio.CancelledError:
        return


async def _handle_set(sub: Subscriber, payload: str) -> None:
    """Switch this subscriber's tf / expiry / symbol via 'set:tf=5m,exp=YYYY-MM-DD,sym=BANKNIFTY'."""
    parts = dict(p.split("=", 1) for p in payload.split(",") if "=" in p)
    if "tf" in parts and parts["tf"] in TIMEFRAME_TO_DELTA:
        sub.timeframe = parts["tf"]
    if "sym" in parts:
        new_sym = parts["sym"].upper()
        entry = get_registry().get(new_sym)
        if entry is not None and entry.fno_eligible:
            sub.symbol = new_sym
    if "exp" in parts:
        try:
            sub.expiry = date.fromisoformat(parts["exp"])
        except ValueError:
            pass
    try:
        rt = get_runtime()
        live_spot = rt.latest_spot if sub.symbol == rt.active_symbol else None
        snap = await get_oi_engine().get(
            sub.timeframe, sub.expiry, symbol=sub.symbol, live_spot=live_spot
        )
        msg = {"type": "oi_change", "data": _serialize_oi(snap)}
        try:
            sub.queue.put_nowait(msg)
        except asyncio.QueueFull:
            try:
                sub.queue.get_nowait()
            except Exception:
                pass
            try:
                sub.queue.put_nowait(msg)
            except Exception:
                pass
        oc = await get_option_chain_full_engine().get(
            sub.timeframe, sub.expiry, symbol=sub.symbol, live_spot=live_spot
        )
        oc_msg = {"type": "option_chain_full", "data": _serialize_oc(oc)}
        try:
            sub.queue.put_nowait(oc_msg)
        except asyncio.QueueFull:
            try:
                sub.queue.get_nowait()
            except Exception:
                pass
            try:
                sub.queue.put_nowait(oc_msg)
            except Exception:
                pass
    except Exception as e:
        log.warning("ws_oi_stream.set_snapshot.error", error=str(e))
