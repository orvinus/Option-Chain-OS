"""Follower feed client — consumes ticks from another backend's ``/ws/td-relay``.

Selected by ``FEED_VENDOR=td_relay``. Presents the same duck-typed surface as
``TrueDataFeedClient`` / ``OptionFeedClient`` (start/stop/nudge_reconnect/
swap_subscription + the health properties) so ``feed_factory``, the spot
refresher, ``/api/health`` and the symbol controller need no special cases.

What it deliberately does NOT do:

* It never logs into TrueData. The whole point is that the vendor's single
  realtime login is spent once, on the relay owner.
* It does not steer the owner's subscriptions. ``swap_subscription`` refreshes
  the follower's own ``rt.tokens`` / ``rt.expiries`` (so health and the ATM
  drift watcher stay meaningful) but the contracts on the wire are whatever the
  owner streams. Followers are mirrors, not remote controls — if two followers
  wanted different symbols the owner would have to arbitrate, and the ~50
  instrument cap makes that a losing game.

Freshness: every received frame refreshes the heartbeat; every tick frame
lands on the local queue with ``origin="ws"`` so the follower's own aggregator
and health ladder see a normal live transport. If the owner goes quiet the
follower's ``feed_connected`` drops and it reconnects with backoff.
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Any, Optional

from ..core.config import settings
from ..core.logging import get_logger
from .types import Tick

log = get_logger("td_relay_feed")

HEARTBEAT_STALE_S = 40.0      # owner pings every 15 s; 40 s of silence = dead link
BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 30.0
CONNECT_TIMEOUT_S = 20.0


def _relay_url() -> str:
    base = (settings.td_relay_url or "").strip()
    if not base:
        raise RuntimeError("TD_RELAY_URL is empty — FEED_VENDOR=td_relay needs the owner's /ws/td-relay URL")
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}key={settings.td_relay_key or ''}"


class TdRelayFeedClient:
    """Live feed sourced from a relay owner instead of the vendor."""

    def __init__(
        self,
        out_queue: "asyncio.Queue[Tick]",
        tokens_provider,
        index_token: str | None = None,
        active_symbol: str | None = None,
        index_segment: int | None = None,
    ) -> None:
        self._queue = out_queue
        self._tokens_provider = tokens_provider
        self._active_symbol = (active_symbol or settings.underlying_symbol or "NIFTY").upper()

        self._ws = None
        self._connected = asyncio.Event()
        self._stopping = asyncio.Event()
        self._kick = asyncio.Event()
        self._supervisor_task: asyncio.Task | None = None

        self._latest_underlying: float | None = None
        self._last_tick_at: float = 0.0
        self._last_heartbeat_at: float = 0.0
        self._connect_started_at: float = 0.0
        self._desired_tokens: int = 0
        self._owner_hello: dict[str, Any] = {}

        self.frames_seen = 0
        self.dropped_queue_full = 0
        self.reconnects = 0

    # ------------------------------------------------------------- surface
    @property
    def latest_underlying(self) -> float | None:
        return self._latest_underlying

    @property
    def is_feed_connected(self) -> bool:
        return self._connected.is_set()

    @property
    def supervisor_alive(self) -> bool:
        return self._supervisor_task is not None and not self._supervisor_task.done()

    @property
    def last_ws_tick_at(self) -> float:
        return self._last_tick_at

    @property
    def live_subscription_count(self) -> int:
        # The owner decides the universe; report what the follower expects so
        # health stays non-zero while frames flow.
        return self._desired_tokens if self._connected.is_set() else 0

    @property
    def heartbeat_age_s(self) -> float | None:
        if not self._last_heartbeat_at:
            return None
        return time.monotonic() - self._last_heartbeat_at

    @property
    def stuck_in_connect_s(self) -> float:
        if self._connected.is_set() or not self._connect_started_at:
            return 0.0
        return time.monotonic() - self._connect_started_at

    async def start(self) -> None:
        self._stopping.clear()
        # Seed the follower's universe view (rt.tokens / expiries) so health and
        # the drift watcher have something to report from the first minute.
        try:
            tokens, _spot = await self._tokens_provider()
            self._desired_tokens = len(tokens)
        except Exception as e:  # noqa: BLE001
            log.warning("td_relay_feed.tokens_provider_failed", error=str(e))
        self._supervisor_task = asyncio.create_task(self._supervisor(), name="td-relay-supervisor")

    def nudge_reconnect(self) -> None:
        self._connected.clear()
        self._kick.set()

    async def stop(self) -> None:
        self._stopping.set()
        self._kick.set()
        if self._supervisor_task:
            self._supervisor_task.cancel()
            try:
                await self._supervisor_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        self._connected.clear()

    async def swap_subscription(self, tokens: list, spot_token, symbol: str, spot_seg=None) -> None:
        """Followers cannot steer the owner; keep local bookkeeping honest."""
        self._active_symbol = (symbol or self._active_symbol).upper()
        self._desired_tokens = len(tokens or [])
        log.info(
            "td_relay_feed.swap_subscription_noop",
            symbol=self._active_symbol, desired=self._desired_tokens,
            note="relay followers mirror the owner's universe",
        )

    def feed_stats(self) -> dict[str, Any]:
        return {
            "vendor": "td_relay",
            "connected": self.is_feed_connected,
            "supervisor_alive": self.supervisor_alive,
            "relay_url": (settings.td_relay_url or "").split("?")[0],
            "owner": self._owner_hello,
            "heartbeat_age_s": self.heartbeat_age_s,
            "stuck_in_connect_s": self.stuck_in_connect_s,
            "frames_seen": self.frames_seen,
            "dropped_queue_full": self.dropped_queue_full,
            "reconnects": self.reconnects,
            "desired_tokens": self._desired_tokens,
        }

    # ----------------------------------------------------------- background
    async def _sleep_or_kick(self, seconds: float) -> None:
        self._kick.clear()
        try:
            await asyncio.wait_for(self._kick.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _supervisor(self) -> None:
        backoff = BACKOFF_MIN_S
        while not self._stopping.is_set():
            try:
                await self._run_once()
                backoff = BACKOFF_MIN_S
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("td_relay_feed.disconnected", error=str(e), backoff_s=round(backoff, 1))
            finally:
                self._connected.clear()
            if self._stopping.is_set():
                break
            self.reconnects += 1
            await self._sleep_or_kick(backoff + random.uniform(0, backoff / 2))
            backoff = min(BACKOFF_MAX_S, backoff * 2)

    async def _run_once(self) -> None:
        import websockets  # local import: keeps the module importable without the dep at test time

        url = _relay_url()
        self._connect_started_at = time.monotonic()
        async with websockets.connect(url, open_timeout=CONNECT_TIMEOUT_S, ping_interval=None, max_size=2**20) as ws:
            self._ws = ws
            self._last_heartbeat_at = time.monotonic()
            watchdog = asyncio.create_task(self._heartbeat_watch(), name="td-relay-heartbeat")
            try:
                async for raw in ws:
                    self._on_message(raw, ws)
                    if self._kick.is_set() and not self._stopping.is_set():
                        break  # nudge_reconnect() — drop this socket and let the supervisor redial
            finally:
                watchdog.cancel()
                self._ws = None

    def _on_message(self, raw, ws) -> None:
        self.frames_seen += 1
        self._last_heartbeat_at = time.monotonic()
        try:
            f = json.loads(raw)
        except Exception:  # noqa: BLE001
            return
        typ = f.get("type")
        if typ == "tick":
            try:
                from ..ws.td_relay import frame_to_tick
                tick = frame_to_tick(f)
            except Exception as e:  # noqa: BLE001
                log.warning("td_relay_feed.bad_frame", error=str(e))
                return
            if not self._connected.is_set():
                self._connected.set()
                log.info("td_relay_feed.connected", owner=self._owner_hello)
            # The owner streams several chains; only the followed symbol's
            # spot is this client's "latest underlying".
            if tick.underlying is not None and (tick.symbol or "").upper() == self._active_symbol:
                self._latest_underlying = tick.underlying
            self._last_tick_at = time.time()
            try:
                self._queue.put_nowait(tick)
            except asyncio.QueueFull:
                self.dropped_queue_full += 1
        elif typ == "hello":
            self._owner_hello = {k: v for k, v in f.items() if k != "type"}
            self._connected.set()
            log.info("td_relay_feed.hello", **self._owner_hello)
        elif typ == "ping":
            asyncio.create_task(self._safe_send(ws, "pong"))
        elif typ == "error":
            log.error("td_relay_feed.owner_error", message=f.get("message"))

    async def _safe_send(self, ws, text: str) -> None:
        try:
            await ws.send(text)
        except Exception:  # noqa: BLE001
            pass

    async def _heartbeat_watch(self) -> None:
        while True:
            await asyncio.sleep(5.0)
            age = self.heartbeat_age_s
            if age is not None and age > HEARTBEAT_STALE_S and self._connected.is_set():
                log.warning("td_relay_feed.heartbeat_stale", age_s=round(age, 1))
                self.nudge_reconnect()
                ws = self._ws
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception:  # noqa: BLE001
                        pass
                return
