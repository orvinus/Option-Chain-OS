"""Supervised background tasks: a dead task must never be silent again.

Every long-lived loop in this backend used to be a bare ``asyncio.create_task``
with no done-callback and (for some) not even a retained reference. Consequences,
both observed in production:

* a crashed task logged NOTHING — the strong reference on the owner suppressed
  even asyncio's GC-time "exception was never retrieved" warning; the
  ws-supervisor could die at 19:09 and leave zero forensic trace;
* the process kept reporting healthy while the dead task's job silently stopped.

``spawn_supervised`` gives every loop three guarantees: a strong reference (no
GC kills), a death log + Telegram alert with the traceback, and — for loops that
are safe to re-enter — an automatic respawn after a short delay.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from .logging import get_logger
from .notify import notify

log = get_logger("tasks")

RESPAWN_DELAY_S = 5.0

# Strong references so no supervised task can be garbage-collected mid-flight.
_tasks: dict[str, asyncio.Task] = {}


def spawn_supervised(
    factory: Callable[[], Awaitable[None]],
    name: str,
    *,
    respawn: bool = True,
) -> asyncio.Task:
    """Create a named task that logs, alerts, and (optionally) respawns on death.

    ``factory`` is a zero-arg callable returning a fresh coroutine — required so
    a respawn can build a NEW coroutine (a coroutine object is single-use).
    ``respawn=False`` still logs + alerts the death; use it for loops whose owner
    handles restarts itself.
    """
    task = asyncio.get_running_loop().create_task(factory(), name=name)
    _tasks[name] = task

    def _on_done(t: asyncio.Task) -> None:
        _tasks.pop(name, None)
        if t.cancelled():
            log.info("task.cancelled", task=name)
            return
        exc = t.exception()
        if exc is None:
            log.info("task.finished", task=name)
            return
        log.error("task.died", task=name, error=str(exc), exc_info=exc)
        notify(f"task_died:{name}", f"⚠️ Background task '{name}' died: {exc!r}")
        if respawn:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                return

            def _respawn() -> None:
                log.warning("task.respawning", task=name, delay_s=RESPAWN_DELAY_S)
                spawn_supervised(factory, name, respawn=True)

            loop.call_later(RESPAWN_DELAY_S, _respawn)

    task.add_done_callback(_on_done)
    return task


def supervised_task_names() -> list[str]:
    """Names of currently-alive supervised tasks (for /api/health introspection)."""
    return sorted(name for name, t in _tasks.items() if not t.done())


def cancel_supervised(name: str) -> None:
    task = _tasks.get(name)
    if task is not None and not task.done():
        task.cancel()
