"""Fan-out hub for the Algo Config live stream.

Deliberately shaped differently from ``hub.py``, and the difference is the
whole point:

* ``OIStreamHub.publish_flush`` recomputes the chain **per subscriber**, inside
  the publish call, serialised on a process-global engine lock (20-60 ms each).
  Two tabs on the same timeframe pay twice.
* This hub is a **dumb fan-out**. It knows only which distinct *scopes* are
  currently being watched (``live_scopes()``); the producer in
  ``algo/live_stream.py`` computes each scope ONCE and calls ``publish``. Ten
  tabs on the same zone cost one computation, and a scope nobody is looking at
  costs nothing at all.

That inversion is what makes a ~1 s cadence affordable where the OI hub's
per-subscriber model would not be.

The hub also never filters on ``rt.active_symbol`` — the OI hub does
(``hub.py:68``) because only the active symbol has live ticks, but the Algo
Config page must keep rendering a Thursday/SENSEX zone while the feed follows
NIFTY. Staleness is reported honestly in the frame instead (``live`` /
``source``), never by silently dropping frames.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from fastapi import WebSocket

from ..core.logging import get_logger

log = get_logger("algo_hub")


@dataclass(frozen=True)
class AlgoScope:
    """What one subscriber is watching. Frozen + hashable so the producer can
    de-duplicate scopes across tabs with a plain ``set``."""
    day: str
    zone: str
    symbol: str
    expiry: date
    strike: Optional[int] = None
    option_type: str = "CE"

    @property
    def key(self) -> str:
        return (
            f"{self.day}/{self.zone}/{self.symbol}/{self.expiry:%Y-%m-%d}"
            f"/{self.strike or '-'}{self.option_type}"
        )

    def as_dict(self) -> dict:
        return {
            "day": self.day,
            "zone": self.zone,
            "symbol": self.symbol,
            "expiry": self.expiry.isoformat(),
            "strike": self.strike,
            "option_type": self.option_type,
        }


@dataclass
class AlgoSubscriber:
    ws: WebSocket
    scope: AlgoScope
    username: str = ""
    # maxsize=4 (vs the OI hub's 8): frames here supersede each other
    # completely, so a deep queue only buys a slow client the right to render
    # stale state. Drop-oldest keeps the newest.
    queue: "asyncio.Queue[dict]" = field(
        default_factory=lambda: asyncio.Queue(maxsize=4)
    )


class AlgoStreamHub:
    def __init__(self) -> None:
        # list (not set): AlgoSubscriber holds a Queue → unhashable.
        self._subs: list[AlgoSubscriber] = []
        self._lock = asyncio.Lock()

    async def add(self, sub: AlgoSubscriber) -> None:
        async with self._lock:
            self._subs.append(sub)
        log.info("algo_hub.subscribe", count=len(self._subs), scope=sub.scope.key)

    async def remove(self, sub: AlgoSubscriber) -> None:
        async with self._lock:
            try:
                self._subs.remove(sub)
            except ValueError:
                pass
        log.info("algo_hub.unsubscribe", count=len(self._subs))

    async def retarget(self, sub: AlgoSubscriber, scope: AlgoScope) -> None:
        """Re-scope a live subscriber in place (the ``set:`` message), without
        tearing down the socket."""
        async with self._lock:
            sub.scope = scope
        log.info("algo_hub.retarget", scope=scope.key)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    async def live_scopes(self) -> list[AlgoScope]:
        """The DISTINCT scopes currently watched. The producer's work list —
        empty means the producer does nothing at all this tick."""
        async with self._lock:
            return list({s.scope for s in self._subs})

    async def publish(self, scope: AlgoScope, frame: dict) -> None:
        """Fan one computed frame out to every subscriber on that scope."""
        async with self._lock:
            targets = [s for s in self._subs if s.scope == scope]
        for sub in targets:
            _offer(sub.queue, frame)

    async def broadcast(self, frame: dict) -> None:
        """Send to everyone regardless of scope (feed-state changes)."""
        async with self._lock:
            targets = list(self._subs)
        for sub in targets:
            _offer(sub.queue, frame)


def _offer(queue: "asyncio.Queue[dict]", msg: dict) -> None:
    """Drop-oldest backpressure — identical semantics to the OI hub's inline
    version, factored out because this hub uses it in three places."""
    try:
        queue.put_nowait(msg)
        return
    except asyncio.QueueFull:
        pass
    try:
        queue.get_nowait()
    except Exception:
        pass
    try:
        queue.put_nowait(msg)
    except Exception:
        pass


_singleton: AlgoStreamHub | None = None


def get_algo_hub() -> AlgoStreamHub:
    global _singleton
    if _singleton is None:
        _singleton = AlgoStreamHub()
    return _singleton
