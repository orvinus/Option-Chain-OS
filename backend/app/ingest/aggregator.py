"""1-minute (or configurable) aggregator: ticks -> TimescaleDB rows.

Algorithm:
  * For every (token, minute_bucket) we keep the *latest* tick observed within
    that bucket — that's the canonical value of the OI at the close of the
    minute.
  * When a new minute boundary is crossed (locally or because the queue is
    quiet), we flush all closed buckets in a single batched INSERT.
  * On startup we rewind to the current minute boundary so partial buckets
    are not dropped.

The aggregator also publishes a notification on the ``OnFlushHook`` whenever
a flush happens, so the WebSocket hub can broadcast a fresh OI-change frame
to connected clients without polling the DB on a timer.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from sqlalchemy import text

from ..core.config import settings
from ..core.db import session_scope
from ..core.logging import get_logger
from .types import Tick

log = get_logger("aggregator")


def _bucket_size() -> timedelta:
    return {
        "1s": timedelta(seconds=1),
        "5s": timedelta(seconds=5),
        "1min": timedelta(minutes=1),
    }[settings.persist_bucket]


def _floor_to_bucket(ts: datetime, bucket: timedelta) -> datetime:
    epoch = ts.timestamp()
    floored = epoch - (epoch % bucket.total_seconds())
    return datetime.fromtimestamp(floored, tz=timezone.utc)


OnFlushHook = Callable[[datetime, int], Awaitable[None]]


class MinuteAggregator:
    """Bucketing consumer for ``Tick`` objects."""

    def __init__(
        self,
        in_queue: "asyncio.Queue[Tick]",
        on_flush: OnFlushHook | None = None,
    ) -> None:
        self._queue = in_queue
        self._on_flush = on_flush
        self._stopping = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._bucket = _bucket_size()
        # (token, bucket_start) -> Tick (latest observed)
        self._open_buckets: dict[tuple[str, datetime], Tick] = {}

    async def start(self) -> None:
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="aggregator")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self) -> None:
        log.info("aggregator.started", bucket=settings.persist_bucket)
        last_check = datetime.now(timezone.utc)
        while not self._stopping.is_set():
            timeout = max(0.5, self._bucket.total_seconds() / 4)
            try:
                tick = await asyncio.wait_for(self._queue.get(), timeout=timeout)
                self._absorb(tick)
            except asyncio.TimeoutError:
                pass
            now = datetime.now(timezone.utc)
            if (now - last_check).total_seconds() >= self._bucket.total_seconds():
                await self._flush_closed(now)
                last_check = now
        # Final flush on shutdown
        await self._flush_closed(datetime.now(timezone.utc), force_all=True)

    def _absorb(self, tick: Tick) -> None:
        if tick.option_type == "IDX":
            return  # spot is broadcast via ws_client.latest_underlying, not stored
        bucket_start = _floor_to_bucket(tick.ts, self._bucket)
        key = (tick.token, bucket_start)
        prev = self._open_buckets.get(key)
        if prev is None or tick.ts >= prev.ts:
            self._open_buckets[key] = tick

    async def _flush_closed(self, now: datetime, force_all: bool = False) -> None:
        cutoff = _floor_to_bucket(now, self._bucket)
        if force_all:
            cutoff = now + timedelta(seconds=1)
        to_flush: list[tuple[tuple[str, datetime], Tick]] = []
        for key, tick in list(self._open_buckets.items()):
            _, bstart = key
            # Bucket is closed when its end is <= cutoff
            if bstart + self._bucket <= cutoff:
                to_flush.append((key, tick))
        if not to_flush:
            return
        rows = [
            {
                "ts": bstart,
                "symbol": tick.symbol,
                "expiry": tick.expiry,
                "strike": tick.strike,
                "option_type": tick.option_type,
                "token": tick.token,
                "oi": tick.oi,
                "ltp": tick.ltp,
                "volume": tick.volume,
                "underlying": tick.underlying,
            }
            for (_, bstart), tick in to_flush
        ]
        try:
            async with session_scope() as s:
                await s.execute(
                    text(
                        """
                        INSERT INTO option_oi_snapshots
                            (ts, symbol, expiry, strike, option_type, token,
                             oi, ltp, volume, underlying)
                        VALUES
                            (:ts, :symbol, :expiry, :strike, :option_type, :token,
                             :oi, :ltp, :volume, :underlying)
                        ON CONFLICT (ts, token) DO UPDATE SET
                            oi = EXCLUDED.oi,
                            ltp = EXCLUDED.ltp,
                            volume = EXCLUDED.volume,
                            underlying = EXCLUDED.underlying
                        """
                    ),
                    rows,
                )
            for key, _ in to_flush:
                self._open_buckets.pop(key, None)
            log.info("aggregator.flushed", rows=len(rows), cutoff=cutoff.isoformat())
            if self._on_flush is not None:
                try:
                    await self._on_flush(cutoff, len(rows))
                except Exception as e:  # pragma: no cover - hook failure must not stop ingest
                    log.warning("aggregator.flush_hook.error", error=str(e))
        except Exception as e:
            log.error("aggregator.flush.error", error=str(e), rows=len(rows))
            # leave buckets in place so we retry on next iteration
