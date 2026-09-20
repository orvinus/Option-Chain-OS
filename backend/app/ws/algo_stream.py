"""WebSocket route ``/ws/algo-stream`` — the Algo Config page's live feed.

A SEPARATE endpoint from ``/ws/oi-stream``, for three reasons that all matter:

1. **Auth.** Every ``/api/algo/*`` route is ``Depends(require_admin)``;
   ``/ws/oi-stream`` is unauthenticated. Pushing config/position/P&L state
   down the open socket would hand it to anyone who can reach the port.
2. **Frame shape.** The OI stream's types are hard-wired to the chain
   (``oi_change`` / ``option_chain_full``); this one carries a single ``algo``
   envelope with a revision counter.
3. **Symbol filter.** ``hub.publish_flush`` silently skips subscribers whose
   symbol is not ``rt.active_symbol``. The Algo Config page must keep working
   on a Thursday/SENSEX zone while the feed follows NIFTY.

Query params: ``day``, ``zone`` (required); ``symbol``, ``expiry``,
``strike``, ``option_type`` (optional — resolved from the config when absent).

Behaviour mirrors ``oi_stream.py`` deliberately: accept() first so the browser
never sees a failed upgrade, an error frame + graceful close on bad input, a
per-subscriber queue drained by a sender task, a 15 s ping, and
``set:day=...,zone=...`` to re-scope in place.
"""
from __future__ import annotations

import asyncio
from datetime import date

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from ..algo.auth import SESSION_COOKIE, _identity_from_user_id, verify_token
from ..core.logging import get_logger
from .algo_hub import AlgoScope, AlgoSubscriber, get_algo_hub

log = get_logger("ws_algo_stream")
router = APIRouter()

PING_INTERVAL_SECONDS = 15.0


async def _resolve_scope(
    day: str,
    zone: str,
    symbol: str | None,
    expiry: str | None,
    strike: int | None,
    option_type: str,
) -> tuple[AlgoScope | None, str]:
    """Fill in whatever the client did not pin, from the live config — the
    same resolution ``/api/algo/engines/*`` performs, so the stream and the
    REST snapshot can never describe different contracts."""
    from ..algo.config_store import get_config_store
    from ..api._expiry_utils import resolve_expiry

    cv = await get_config_store().get_live()
    day_cfg = cv.config.days.get(day)  # type: ignore[arg-type]
    if day_cfg is None:
        return None, f"unknown day {day!r}"
    if zone not in ("Z1", "Z2", "Z3"):
        return None, f"unknown zone {zone!r}"

    sym = (symbol or day_cfg.index_symbol).upper()
    if expiry:
        try:
            exp = date.fromisoformat(expiry)
        except ValueError:
            return None, f"invalid expiry {expiry!r}"
    else:
        exp = await resolve_expiry(None, sym)

    ot = option_type.upper()
    if ot not in ("CE", "PE"):
        return None, f"invalid option_type {option_type!r}"

    return AlgoScope(
        day=day, zone=zone, symbol=sym, expiry=exp, strike=strike, option_type=ot
    ), ""


@router.websocket("/ws/algo-stream")
async def algo_stream(
    websocket: WebSocket,
    day: str = Query(...),
    zone: str = Query(...),
    symbol: str | None = Query(default=None),
    expiry: str | None = Query(default=None),
    strike: int | None = Query(default=None),
    option_type: str = Query(default="CE"),
) -> None:
    # accept() FIRST — closing before accept surfaces to the browser as an
    # opaque 403 with no error frame (the same trap oi_stream.py documents).
    await websocket.accept()

    # ---- auth: the same signed httpOnly cookie the REST layer uses --------
    # Browsers send cookies on a same-origin WS handshake, so nothing new has
    # to be plumbed through the client.
    token = websocket.cookies.get(SESSION_COOKIE, "")
    user_id = verify_token(token) if token else None
    if user_id is None:
        await websocket.send_json(
            {"type": "error", "message": "Not signed in to Algo Config."}
        )
        await websocket.close(code=1008)
        return
    ident = await _identity_from_user_id(user_id)
    if ident is None:
        await websocket.send_json(
            {"type": "error", "message": "Session user no longer exists."}
        )
        await websocket.close(code=1008)
        return

    scope, err = await _resolve_scope(day, zone, symbol, expiry, strike, option_type)
    if scope is None:
        await websocket.send_json({"type": "error", "message": err})
        await websocket.close(code=1008)
        return

    sub = AlgoSubscriber(ws=websocket, scope=scope, username=ident.username)
    hub = get_algo_hub()
    await hub.add(sub)

    sender_task = asyncio.create_task(_sender(sub), name=f"algo-ws-send-{id(sub)}")
    pinger_task = asyncio.create_task(_pinger(sub), name=f"algo-ws-ping-{id(sub)}")

    # Immediate first frame so the page paints without waiting up to a full
    # producer tick.
    try:
        from ..algo.live_stream import compute_frame

        await websocket.send_json(await compute_frame(scope))
    except Exception as e:
        log.warning("ws_algo_stream.initial_frame.error", error=str(e))

    try:
        while True:
            msg = await websocket.receive_text()
            if msg.strip() == "pong":
                continue
            if msg.startswith("set:"):
                await _handle_set(sub, msg[4:])
    except WebSocketDisconnect:
        log.info("ws_algo_stream.client_disconnected")
    except Exception as e:
        log.warning("ws_algo_stream.error", error=str(e))
    finally:
        sender_task.cancel()
        pinger_task.cancel()
        await hub.remove(sub)


async def _handle_set(sub: AlgoSubscriber, payload: str) -> None:
    """``set:day=friday,zone=Z2,strike=24500`` — re-scope in place.

    Unlike ``oi_stream._handle_set``, this does NOT recompute inline. That
    endpoint's inline recompute lets a rapid dropdown change fire unbounded
    full recomputes on the shared engine lock; here the next producer tick
    (≤ ~1 s away) picks the new scope up for free.
    """
    parts = dict(p.split("=", 1) for p in payload.split(",") if "=" in p)
    cur = sub.scope
    raw_strike = parts.get("strike", "").strip()
    try:
        strike = (
            None
            if raw_strike in ("", "-", "auto")
            else int(raw_strike)
        )
    except ValueError:
        strike = cur.strike

    scope, err = await _resolve_scope(
        parts.get("day", cur.day),
        parts.get("zone", cur.zone),
        parts.get("sym") or parts.get("symbol") or cur.symbol,
        parts.get("exp") or parts.get("expiry"),
        strike,
        parts.get("ot") or parts.get("option_type") or cur.option_type,
    )
    if scope is None:
        # Report it instead of silently ignoring (the OI stream swallows bad
        # `set:` values, which makes a typo look like a dead feed).
        try:
            await sub.ws.send_json({"type": "error", "message": err})
        except Exception:
            pass
        return
    await get_algo_hub().retarget(sub, scope)


async def _sender(sub: AlgoSubscriber) -> None:
    try:
        while True:
            msg = await sub.queue.get()
            await sub.ws.send_json(msg)
    except asyncio.CancelledError:
        return
    except Exception:
        return


async def _pinger(sub: AlgoSubscriber) -> None:
    try:
        while True:
            await asyncio.sleep(PING_INTERVAL_SECONDS)
            await sub.ws.send_json({"type": "ping"})
    except asyncio.CancelledError:
        return
    except Exception:
        return
