"""WebSocket fan-out hub.

The aggregator triggers ``hub.publish_flush(bucket)`` on every flush.  The hub
then iterates over its connected clients, computes the OI-change snapshot for
each client's selected ``(timeframe, expiry)`` pair, and pushes the result
over the websocket.

Per-client timeframe means we don't compute the same dataset twice: results
are looked up via the cached ``OIChangeEngine``.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from fastapi import WebSocket

from ..core.logging import get_logger
from ..runtime import get_runtime
from ..services import get_oi_engine, get_option_chain_full_engine

log = get_logger("ws_hub")


@dataclass
class Subscriber:
    ws: WebSocket
    timeframe: str
    expiry: date
    symbol: str
    queue: "asyncio.Queue[dict]" = field(default_factory=lambda: asyncio.Queue(maxsize=8))


class OIStreamHub:
    def __init__(self) -> None:
        # list (not set): Subscriber is a @dataclass with a Queue field → unhashable.
        self._subs: list[Subscriber] = []
        self._lock = asyncio.Lock()

    async def add(self, sub: Subscriber) -> None:
        async with self._lock:
            self._subs.append(sub)
        log.info("ws_hub.subscribe", count=len(self._subs))

    async def remove(self, sub: Subscriber) -> None:
        async with self._lock:
            try:
                self._subs.remove(sub)
            except ValueError:
                pass
        log.info("ws_hub.unsubscribe", count=len(self._subs))

    async def publish_flush(self, bucket: datetime, _rows: int) -> None:
        async with self._lock:
            subs = list(self._subs)
        if not subs:
            return
        oi_engine = get_oi_engine()
        oc_engine = get_option_chain_full_engine()
        rt = get_runtime()
        active = rt.active_symbol
        for sub in subs:
            # Only the active symbol has fresh ticks landing in the DB; subscribers
            # tracking a different symbol receive no live flushes (they can still
            # poll the REST endpoints for that symbol's historical data).
            if sub.symbol and sub.symbol != active:
                continue
            try:
                live_spot = rt.latest_spot
                payload = await oi_engine.get(
                    sub.timeframe, sub.expiry, symbol=sub.symbol, live_spot=live_spot
                )
            except Exception as e:
                log.warning("ws_hub.compute.error", error=str(e))
                continue
            msg = {
                "type": "oi_change",
                "bucket": bucket.isoformat(),
                "data": _serialize_oi(payload),
            }
            try:
                sub.queue.put_nowait(msg)
            except asyncio.QueueFull:
                # Slow client: drop oldest, keep latest.
                try:
                    sub.queue.get_nowait()
                except Exception:
                    pass
                try:
                    sub.queue.put_nowait(msg)
                except Exception:
                    pass
            try:
                live_spot = rt.latest_spot
                oc_payload = await oc_engine.get(
                    sub.timeframe, sub.expiry, symbol=sub.symbol, live_spot=live_spot
                )
            except Exception as e:
                log.warning("ws_hub.option_chain_full.error", error=str(e))
                continue
            oc_msg = {
                "type": "option_chain_full",
                "bucket": bucket.isoformat(),
                "data": _serialize_oc(oc_payload),
            }
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


def _serialize_oi(res) -> dict:
    return {
        "timeframe": res.timeframe,
        "expiry": res.expiry,
        "spot": res.spot,
        "asof": res.asof,
        "computed_at": res.computed_at,
        "total_call_oi_change": res.total_call_oi_change,
        "total_put_oi_change": res.total_put_oi_change,
        "rows": [r.__dict__ for r in res.rows],
    }


def _serialize_oc(res) -> dict:
    return {
        "timeframe": res.timeframe,
        "expiry": res.expiry,
        "spot": res.spot,
        "asof": res.asof,
        "computed_at": res.computed_at,
        "lot_size": res.lot_size,
        "synthetic_future": getattr(res, "synthetic_future", None),
        "atm_iv": getattr(res, "atm_iv", None),
        "ivp": getattr(res, "ivp", None),
        "rows": [r.__dict__ for r in res.rows],
    }


_singleton: OIStreamHub | None = None


def get_hub() -> OIStreamHub:
    global _singleton
    if _singleton is None:
        _singleton = OIStreamHub()
    return _singleton
