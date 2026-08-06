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


# (bucket_cutoff, rows_flushed, ws_rows_flushed). ws_rows counts only ticks with
# origin == "ws" so the caller can maintain a WS-specific freshness timestamp that
# REST-poller rows cannot advance (a dead socket must never look healthy because
# the failover path is writing).
OnFlushHook = Callable[[datetime, int, int], Awaitable[None]]

# A DB outage must not let open buckets accrete until the OOM killer converts it
# into a feed outage too. At 1s buckets x ~100 tokens this is ~10 minutes of full
# flow; beyond it the oldest buckets are dropped (counted, logged).
MAX_OPEN_BUCKETS = 60_000


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
        self._dropped_buckets = 0
        # Sample the 1/s "flushed" INFO line down to ~1/min; every flush still
        # fires the hook, and errors are never sampled.
        self._last_flush_log: float = 0.0

    async def start(self) -> None:
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="aggregator")

    async def stop(self) -> None:
        """Stop the loop, giving it a chance to persist what it is holding.

        Cancelling the task outright (the previous behaviour) killed it inside its
        `await`, so the "final flush on shutdown" at the end of ``_run`` was
        unreachable and the in-progress bucket was silently dropped on EVERY restart.
        """
        self._stopping.set()
        if self._task:
            try:
                await asyncio.wait_for(
                    self._task, timeout=self._bucket.total_seconds() + 5.0
                )
            except asyncio.TimeoutError:
                log.warning("aggregator.stop.timeout_cancelling")
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self) -> None:
        log.info("aggregator.started", bucket=settings.persist_bucket)
        last_bucket = _floor_to_bucket(datetime.now(timezone.utc), self._bucket)
        while not self._stopping.is_set():
            # Poll often enough to notice a bucket boundary promptly; the wait is
            # cheap (it is just the queue read).
            timeout = max(0.5, min(5.0, self._bucket.total_seconds() / 4))
            try:
                tick = await asyncio.wait_for(self._queue.get(), timeout=timeout)
                self._absorb(tick)
            except asyncio.TimeoutError:
                pass
            # Flush when the clock CROSSES a bucket boundary, not after "one bucket of
            # elapsed time". The old elapsed-time rule drifted: a bucket that closed at
            # T could sit unwritten until the next check, so rows routinely landed
            # 60-120s late and the dashboard lagged the market by up to two minutes.
            now = datetime.now(timezone.utc)
            cur_bucket = _floor_to_bucket(now, self._bucket)
            if cur_bucket != last_bucket:
                await self._flush_closed(now)
                last_bucket = cur_bucket
        # Graceful shutdown: absorb whatever is still queued, then persist everything
        # including the in-progress bucket.
        drained = 0
        while True:
            try:
                self._absorb(self._queue.get_nowait())
                drained += 1
            except asyncio.QueueEmpty:
                break
        log.info("aggregator.stopping.final_flush", drained=drained, open_buckets=len(self._open_buckets))
        await self._flush_closed(datetime.now(timezone.utc), force_all=True)

    def _absorb(self, tick: Tick) -> None:
        if tick.option_type == "IDX":
            return  # spot is broadcast via ws_client.latest_underlying, not stored
        bucket_start = _floor_to_bucket(tick.ts, self._bucket)
        key = (tick.token, bucket_start)
        prev = self._open_buckets.get(key)
        # Within a bucket the newest tick wins — EXCEPT that a REST-poller tick may
        # never displace a live WS tick: poller quotes can be tens of seconds old
        # (429 backoff) yet carry a fresher enqueue timestamp, and right after WS
        # recovery a lingering failover sweep would otherwise overwrite the socket's
        # value with staler data.
        if prev is not None and prev.origin == "ws" and tick.origin != "ws":
            return
        if prev is None or tick.ts >= prev.ts or (prev.origin != "ws" and tick.origin == "ws"):
            self._open_buckets[key] = tick
        # Bounded memory under a DB outage (flush failures leave buckets in place).
        # Drop in BATCHES down to 90% of the cap: a per-tick drop would sort the
        # whole dict and emit a warning on every absorb once saturated.
        if len(self._open_buckets) > MAX_OPEN_BUCKETS:
            target = int(MAX_OPEN_BUCKETS * 0.9)
            overflow = len(self._open_buckets) - target
            for old_key in sorted(self._open_buckets, key=lambda k: k[1])[:overflow]:
                self._open_buckets.pop(old_key, None)
            self._dropped_buckets += overflow
            log.warning(
                "aggregator.buckets_dropped_oldest",
                dropped=overflow, total_dropped=self._dropped_buckets,
                hint="DB flushes are failing and the retry backlog hit its cap.",
            )

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
                            -- COALESCE so a later "price unknown" (NULL) tick can never
                            -- erase a price we already knew for this bucket.
                            ltp = COALESCE(EXCLUDED.ltp, option_oi_snapshots.ltp),
                            volume = GREATEST(EXCLUDED.volume, option_oi_snapshots.volume),
                            underlying = COALESCE(EXCLUDED.underlying, option_oi_snapshots.underlying)
                        """
                    ),
                    rows,
                )
            for key, _ in to_flush:
                self._open_buckets.pop(key, None)
            # At PERSIST_BUCKET=1s this line fired every second (~86k lines/day of
            # pure noise on the production disk) — sample it to ~1/min. The flush
            # itself, the hook and every error path are NOT sampled.
            now_mono = asyncio.get_running_loop().time()
            if now_mono - self._last_flush_log >= 60.0:
                self._last_flush_log = now_mono
                log.info("aggregator.flushed", rows=len(rows), cutoff=cutoff.isoformat())
            if self._on_flush is not None:
                ws_rows = sum(1 for _, tick in to_flush if tick.origin == "ws")
                try:
                    await self._on_flush(cutoff, len(rows), ws_rows)
                except Exception as e:  # pragma: no cover - hook failure must not stop ingest
                    log.warning("aggregator.flush_hook.error", error=str(e))
        except Exception as e:
            log.error("aggregator.flush.error", error=str(e), rows=len(rows))
            # leave buckets in place so we retry on next iteration
