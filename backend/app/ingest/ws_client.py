"""Symphony XTS market-data feed — async Socket.IO client with reconnect + heartbeat.

The Shrilakshmi Fintech / Symphony XTS market-data stream is delivered over
Socket.IO. Unlike AngelOne's ``SmartWebSocketV2`` (a sync, thread-based client
whose mode-3 snapquote carried LTP + OI + volume in one tick), XTS:

* uses ``python-socketio``'s ``AsyncClient`` — so we run directly on the event
  loop with no worker-thread bridge;
* requires a **REST** subscription call (``/instruments/subscription``) per
  instrument rather than subscribing over the socket; and
* splits market data across message codes — **LTP/volume arrive on touchline
  (1501)** while **open interest arrives on its own event (1510)**. We subscribe
  each option to *both* and **merge** them, per instrument, into the single
  ``Tick`` shape the rest of the pipeline already expects. (Indices have no OI,
  so the index/spot instrument is subscribed to 1501 only.)

XTS prices are already in rupees (no ``/100`` paise scaling that Angel needed).

Public surface (unchanged from the previous integration):
    feed = OptionFeedClient(queue, get_tokens, index_token=..., active_symbol=...)
    await feed.start()
    ...
    await feed.stop()
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode

import httpx
import socketio  # type: ignore

from ..auth import get_session_manager
from ..core.config import settings
from ..core.logging import get_logger
from ..market.scripmaster import InstrumentToken
from ..market_data import xts_client
from .types import Tick

log = get_logger("ws_client")

MSG_TOUCHLINE = xts_client.MSG_TOUCHLINE      # 1501 — LTP / volume
MSG_OPENINTEREST = xts_client.MSG_OPENINTEREST  # 1510 — open interest
SEG_NSECM = xts_client.SEG_NSECM              # 1 — index spot lives here

MAX_INSTRUMENTS_PER_REQUEST = 200
HEARTBEAT_TIMEOUT_SECONDS = 30
BACKOFF_INITIAL = 2.0
BACKOFF_MAX = 60.0
CONNECT_TIMEOUT_SECONDS = 15

# Self-heal: when EVERY subscription is rejected with an auth error ('Invalid
# Token'), the feed auto re-logins (minting a fresh XTS token) and reconnects —
# recovering from the daily token expiry / single-session invalidation with no
# human action. Cooldown + attempt cap prevent a login storm if something else
# (a 2nd backend, revoked creds) keeps invalidating the session.
AUTO_RELOGIN_COOLDOWN_S = 120.0
AUTO_RELOGIN_MAX_ATTEMPTS = 5


TokensProvider = Callable[[], Awaitable[tuple[list[InstrumentToken], float]]]


def _as_obj(data: Any) -> dict | None:
    """Socket.IO payloads may arrive as a JSON string or an already-parsed dict."""
    if isinstance(data, dict):
        return data
    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8", "ignore")
    if isinstance(data, str):
        try:
            obj = json.loads(data)
            return obj if isinstance(obj, dict) else None
        except (ValueError, TypeError):
            return None
    return None


class OptionFeedClient:
    """Asyncio Socket.IO ingestion client for the XTS market-data feed."""

    def __init__(
        self,
        out_queue: "asyncio.Queue[Tick]",
        tokens_provider: TokensProvider,
        index_token: str | None = None,
        active_symbol: str | None = None,
        index_segment: int | None = None,
    ) -> None:
        self._queue = out_queue
        self._tokens_provider = tokens_provider
        self._index_token = index_token or settings.nifty_index_token
        # Cash-market segment the index spot is subscribed on (NSECM for NIFTY,
        # BSECM for SENSEX). Driven by the active symbol's exchange.
        self._index_segment = index_segment or SEG_NSECM
        self._active_symbol = (active_symbol or settings.underlying_symbol or "NIFTY").upper()

        self._loop: asyncio.AbstractEventLoop | None = None
        self._sio: socketio.AsyncClient | None = None
        self._stopping = asyncio.Event()
        self._connected = asyncio.Event()
        self._supervisor_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None

        self._token_meta: dict[str, InstrumentToken] = {}
        # Per-instrument merged state: token -> {"ltp", "volume", "oi"}.
        self._state: dict[str, dict] = {}
        # Last known *good* (non-zero) open interest per token. Unlike ``_state`` this
        # is NOT cleared on reconnect, so a socket bounce (the XTS feed drops every
        # ~80s) does not make OI momentarily collapse to 0 and write false
        # "OI crashed to zero" snapshots. Cleared only on symbol swap.
        self._last_oi: dict[str, int] = {}
        # Current subscription set per message code (for unsubscribe): code -> [instrument dicts].
        self._sub_groups: dict[int, list[dict]] = {}
        self._last_tick_at: float = 0.0
        self._latest_underlying: float | None = None
        # Self-heal state: set when an auth-failure re-login asks _connect_once to
        # drop the socket and reconnect with the fresh token.
        self._reconnect_requested: bool = False
        self._last_auto_relogin: float = 0.0
        self._auto_relogin_attempts: int = 0

    # ------------------------------------------------ public

    @property
    def latest_underlying(self) -> float | None:
        return self._latest_underlying

    @property
    def is_feed_connected(self) -> bool:
        return self._connected.is_set()

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stopping.clear()
        self._supervisor_task = asyncio.create_task(self._supervisor(), name="ws-supervisor")

    async def stop(self) -> None:
        self._stopping.set()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._supervisor_task:
            self._supervisor_task.cancel()
            try:
                await self._supervisor_task
            except (asyncio.CancelledError, Exception):
                pass
        await self._teardown()

    def nudge_reconnect(self) -> None:
        """Drop the live socket so the supervisor reconnects (e.g. at IST session open)."""
        log.info("ws.nudge_reconnect_requested")
        self._connected.clear()

    async def swap_subscription(
        self,
        new_tokens: list[InstrumentToken],
        new_index_token: str,
        new_symbol: str,
        new_index_segment: int | None = None,
    ) -> None:
        """Atomically swap the live subscription set to a different underlying.

        Unsubscribes the prior instruments and subscribes the new ones over REST
        on the existing socket connection — no reconnect.
        """
        new_symbol = (new_symbol or "").upper()
        # Not connected yet: stash state so the next connect picks it up.
        if not self._connected.is_set() or self._sio is None:
            self._token_meta = {t.token: t for t in new_tokens}
            self._index_token = new_index_token
            self._index_segment = new_index_segment or SEG_NSECM
            self._active_symbol = new_symbol
            self._latest_underlying = None
            self._state.clear()
            self._last_oi.clear()
            log.info("ws.swap_subscription.deferred", symbol=new_symbol, token_count=len(new_tokens))
            return

        old_groups = {code: list(insts) for code, insts in self._sub_groups.items()}

        self._token_meta = {t.token: t for t in new_tokens}
        self._index_token = new_index_token
        self._index_segment = new_index_segment or SEG_NSECM
        self._active_symbol = new_symbol
        self._latest_underlying = None
        self._state.clear()
        self._last_oi.clear()

        new_groups = self._build_subscription_groups()
        try:
            token = get_session_manager().token
            for code, insts in old_groups.items():
                for chunk in _chunks(insts, MAX_INSTRUMENTS_PER_REQUEST):
                    await xts_client.unsubscribe(token, chunk, code)
            ok = 0; skipped = 0; errors = 0
            for code, insts in new_groups.items():
                for chunk in _chunks(insts, MAX_INSTRUMENTS_PER_REQUEST):
                    try:
                        await xts_client.subscribe(token, chunk, code)
                        ok += len(chunk)
                    except httpx.HTTPStatusError as he:
                        if he.response is not None and he.response.status_code == 400 and len(chunk) > 1:
                            for inst in chunk:
                                try:
                                    await xts_client.subscribe(token, [inst], code)
                                    ok += 1
                                except httpx.HTTPStatusError as he2:
                                    if he2.response is not None and he2.response.status_code == 400:
                                        skipped += 1
                                    else:
                                        errors += 1
                                except Exception:
                                    errors += 1
                        else:
                            raise
            self._sub_groups = new_groups
            log.info(
                "ws.swap_subscription.subscribed",
                newly_subscribed=ok, already_present=skipped, errors=errors,
            )
        except Exception as e:
            log.warning("ws.swap_subscription.error", error=str(e))
        self._last_tick_at = time.time()
        log.info("ws.swap_subscription.done", symbol=new_symbol, token_count=len(new_tokens))

    # ------------------------------------------------ supervisor

    async def _supervisor(self) -> None:
        backoff = BACKOFF_INITIAL
        while not self._stopping.is_set():
            try:
                await self._connect_once()
                backoff = BACKOFF_INITIAL
                while not self._stopping.is_set() and self._connected.is_set():
                    await asyncio.sleep(1.0)
                if self._stopping.is_set():
                    return
                log.warning("ws.connection_dropped, will reconnect", backoff=backoff)
            except Exception as e:
                if "Not authenticated" in str(e):
                    log.warning("ws.awaiting_dashboard_login", detail=str(e))
                else:
                    log.error("ws.supervisor.error", error=str(e))
            await self._teardown()
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=backoff)
                return
            except asyncio.TimeoutError:
                backoff = min(backoff * 2, BACKOFF_MAX)

    async def _connect_once(self) -> None:
        sess = get_session_manager()
        if not sess.authenticated:
            raise RuntimeError(
                "Not authenticated yet. Use the dashboard login form to authenticate first."
            )

        tokens, spot = await self._tokens_provider()
        if not tokens:
            log.warning("ws.no_tokens_to_subscribe")
            await asyncio.sleep(5)
            return
        self._token_meta = {t.token: t for t in tokens}
        if spot:
            self._latest_underlying = spot
        self._state.clear()

        token = sess.token
        user_id = sess.user_id
        origin, sio_path = xts_client.socket_host_and_path()
        query = urlencode(
            {
                "token": token,
                "userID": user_id,
                "source": settings.xts_md_source or "WebAPI",
                "publishFormat": settings.xts_md_publish_format or "JSON",
                "broadcastMode": settings.xts_md_broadcast_mode or "Full",
            }
        )

        sio = socketio.AsyncClient(reconnection=False, logger=False, engineio_logger=False)
        self._register_handlers(sio)
        self._sio = sio
        self._connected.clear()

        try:
            await sio.connect(
                f"{origin}?{query}",
                socketio_path=sio_path.lstrip("/"),
                transports=["websocket"],
                wait=True,
                wait_timeout=CONNECT_TIMEOUT_SECONDS,
            )
        except Exception as e:
            log.error("ws.connect.failed", error=str(e))
            await self._teardown()
            raise RuntimeError(f"Socket.IO failed to connect: {e}") from e

        # Subscribe over REST now that the socket is open. Safe to call on every
        # (re)connect: if XTS already has these subscriptions on the appKey it
        # returns 400 "already subscribed" (tolerated below); if not, this restores
        # the stream. Either way data flows without tearing the connection down.
        await self._subscribe_all()

        # Self-heal: if the subscribe found an auth failure and re-logged in, the
        # socket we just opened is bound to the now-dead token. Drop it and return
        # so the supervisor reconnects via _connect_once() with the fresh token.
        if self._reconnect_requested:
            self._reconnect_requested = False
            log.info("ws.reconnect_after_self_heal")
            await self._teardown()
            return

        self._connected.set()

        self._last_tick_at = time.time()
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_watch(), name="ws-heartbeat")

    async def _teardown(self) -> None:
        sio = self._sio
        self._sio = None
        self._connected.clear()
        if not sio:
            return
        try:
            await sio.disconnect()
        except Exception:
            pass

    # ------------------------------------------------ subscription

    def _build_subscription_groups(self) -> dict[int, list[dict]]:
        """Instruments grouped by message code: options on 1501+1510, index on 1501.

        XTS subscribes one message code per call (top-level ``xtsMessageCode``), so
        each instrument here is just ``{"exchangeSegment", "exchangeInstrumentID"}``.
        """
        options: list[dict] = []
        for tok in self._token_meta.values():
            try:
                iid = int(tok.token)
            except (TypeError, ValueError):
                continue
            options.append({"exchangeSegment": tok.exchange_type, "exchangeInstrumentID": iid})

        touchline = list(options)
        if self._index_token:
            try:
                touchline.append(
                    {"exchangeSegment": self._index_segment, "exchangeInstrumentID": int(self._index_token)}
                )
            except (TypeError, ValueError):
                pass
        return {MSG_TOUCHLINE: touchline, MSG_OPENINTEREST: list(options)}

    async def _subscribe_all(self) -> None:
        groups = self._build_subscription_groups()
        total = sum(len(v) for v in groups.values())
        if not total:
            log.warning("ws.subscribe.no_instruments")
            return
        token = get_session_manager().token

        # XTS returns 400 for an entire batch if even one instrument is already
        # subscribed on the appKey (subscriptions persist across socket reconnects).
        # Strategy: try the batch first; if 400, fall back to one-at-a-time so that
        # new instruments (outside the previous range) actually get subscribed even
        # when stale ones are mixed in.
        #
        # IMPORTANT: a per-instrument 400 is NOT always benign. XTS returns 400 for
        # both "already subscribed" (data flows anyway) *and* genuine rejections
        # (expired/invalid market-data token, unknown instrument). We must read the
        # response body to tell them apart — otherwise a totally dead feed (every
        # instrument rejected) gets logged as ``subscribe.success`` and the UI shows
        # "connected" while no ticks ever arrive.
        ok = 0  # newly subscribed
        already = 0  # genuine "already subscribed" 400 — data flows anyway
        rejected = 0  # real 400 rejection — NO data flows for these
        errors = 0  # non-400 transport errors
        reasons: Counter[str] = Counter()  # distinct XTS error descriptions seen
        try:
            for code, insts in groups.items():
                for chunk in _chunks(insts, MAX_INSTRUMENTS_PER_REQUEST):
                    try:
                        await xts_client.subscribe(token, chunk, code)
                        ok += len(chunk)
                    except httpx.HTTPStatusError as he:
                        if he.response is not None and he.response.status_code == 400 and len(chunk) > 1:
                            # Retry one at a time so new instruments get through.
                            for inst in chunk:
                                try:
                                    await xts_client.subscribe(token, [inst], code)
                                    ok += 1
                                except httpx.HTTPStatusError as he2:
                                    if he2.response is not None and he2.response.status_code == 400:
                                        is_already, detail = _classify_400(he2.response)
                                        if detail:
                                            reasons[detail] += 1
                                        if is_already:
                                            already += 1  # data flows anyway
                                        else:
                                            rejected += 1
                                            log.warning(
                                                "ws.subscribe.rejected",
                                                inst=inst,
                                                code=code,
                                                detail=detail,
                                            )
                                    else:
                                        errors += 1
                                        log.warning("ws.subscribe.inst_error", inst=inst, code=code)
                                except Exception:
                                    errors += 1
                        else:
                            raise
            self._sub_groups = groups
            # The feed is only live if at least one instrument is newly subscribed
            # OR genuinely already-subscribed. If every instrument was *rejected*
            # (real 400) or errored, no ticks will ever arrive — surface it loudly
            # with the XTS reason instead of masking it as success.
            live = ok + already
            if live == 0 and (rejected or errors):
                log.error(
                    "ws.subscribe.all_failed",
                    instruments=total,
                    rejected=rejected,
                    errors=errors,
                    reasons=dict(reasons),
                    hint="every instrument was rejected — likely an expired XTS "
                    "market-data token (re-login) or a stale scripmaster. No ticks "
                    "will arrive until this is resolved.",
                )
                # Auth failure (every instrument 'Invalid Token') → self-heal by
                # re-logging in and reconnecting under a fresh token. The same
                # recovery clears 'Exceeded Instrument Subscription Limit' — a
                # fresh session starts with 0/50 slots, releasing stale
                # subscriptions a swap failed to free on the gateway.
                if rejected and any(
                    "token" in r.lower() or ("limit" in r.lower() and "exceed" in r.lower())
                    for r in reasons
                ):
                    await self._self_heal_auth()
            else:
                # Healthy subscribe — clear the self-heal attempt counter.
                self._auto_relogin_attempts = 0
                log.info(
                    "ws.subscribe.success",
                    instruments=total,
                    newly_subscribed=ok,
                    already_present=already,
                    rejected=rejected,
                    errors=errors,
                    reasons=dict(reasons) or None,
                )
        except Exception as e:
            # Don't drop the connection on a subscribe hiccup; if no data flows the
            # heartbeat will recover. Avoids a reconnect storm on transient errors.
            log.error("ws.subscribe.error", error=str(e))

    async def _self_heal_auth(self) -> None:
        """Auto-recover from an all-instruments 'Invalid Token' rejection.

        Mints a fresh XTS token (single-flight + debounced in the session manager)
        and requests a socket reconnect so the new token is used for both the
        handshake and the resubscribe. Cooldown + attempt cap stop a login storm
        when something keeps invalidating the session (e.g. a 2nd backend competing
        for the single XTS market-data session, or revoked credentials).
        """
        now = time.time()
        since = now - self._last_auto_relogin
        if since < AUTO_RELOGIN_COOLDOWN_S:
            log.info("ws.self_heal.cooldown", since_s=round(since, 1))
            return
        if self._auto_relogin_attempts >= AUTO_RELOGIN_MAX_ATTEMPTS:
            log.error(
                "ws.self_heal.gave_up",
                attempts=self._auto_relogin_attempts,
                hint="auth still failing after repeated auto re-logins — likely a "
                "2nd backend competing for the single XTS session, or revoked "
                "credentials. Manual intervention required.",
            )
            return
        self._last_auto_relogin = now
        self._auto_relogin_attempts += 1
        log.warning(
            "ws.self_heal.relogin",
            attempt=self._auto_relogin_attempts,
            max=AUTO_RELOGIN_MAX_ATTEMPTS,
        )
        try:
            await get_session_manager().login()
            # Force the supervisor to reconnect so the socket re-handshakes with
            # the fresh token (the live socket is still bound to the dead one).
            self._reconnect_requested = True
            log.info("ws.self_heal.relogin_ok")
        except Exception as e:
            log.error("ws.self_heal.relogin_failed", error=str(e))

    # ------------------------------------------------ socket handlers

    def _register_handlers(self, sio: socketio.AsyncClient) -> None:
        @sio.event
        async def connect() -> None:  # noqa: D401
            log.info("ws.open")

        @sio.event
        async def disconnect() -> None:
            log.warning("ws.close")
            self._connected.clear()

        @sio.on("joined")
        async def on_joined(data: Any = None) -> None:
            log.info("ws.joined")

        @sio.on("error")
        async def on_error(data: Any = None) -> None:
            log.error("ws.error", error=str(data))
            self._connected.clear()

        async def on_touchline(data: Any = None) -> None:
            self._handle_touchline(data)

        async def on_oi(data: Any = None) -> None:
            self._handle_oi(data)

        # JSON full + partial variants for touchline (1501) and open interest (1510).
        for ev in ("1501-json-full", "1501-json-partial"):
            sio.on(ev, on_touchline)
        for ev in ("1510-json-full", "1510-json-partial"):
            sio.on(ev, on_oi)

    def _handle_touchline(self, data: Any) -> None:
        obj = _as_obj(data)
        if obj is None:
            return
        token = _instrument_id(obj)
        if not token:
            return
        ltp = _num(obj.get("LastTradedPrice"))
        if ltp is None and isinstance(obj.get("Touchline"), dict):
            ltp = _num(obj["Touchline"].get("LastTradedPrice"))
        vol = _int(obj.get("TotalTradedQuantity"))
        if vol is None and isinstance(obj.get("Touchline"), dict):
            vol = _int(obj["Touchline"].get("TotalTradedQuantity"))

        self._last_tick_at = time.time()

        # Index / underlying spot — no OI, no option tick.
        if token == str(self._index_token):
            if ltp is not None:
                self._latest_underlying = ltp
            return

        st = self._state.setdefault(token, {"ltp": 0.0, "volume": 0, "oi": 0})
        if ltp is not None:
            st["ltp"] = ltp
        if vol is not None:
            st["volume"] = vol
        self._emit(token, st)

    def _handle_oi(self, data: Any) -> None:
        obj = _as_obj(data)
        if obj is None:
            return
        token = _instrument_id(obj)
        if not token or token == str(self._index_token):
            return
        oi = _int(obj.get("OpenInterest"))
        if oi is None:
            return
        self._last_tick_at = time.time()
        # A subscribed, trading option does not drop to literally zero open interest
        # intraday — a 0 here is a partial-frame / post-reconnect artifact. Ignore it
        # and keep the last known value so the chart never shows a false OI collapse.
        if oi == 0 and self._last_oi.get(token, 0) > 0:
            return
        st = self._state.setdefault(token, {"ltp": 0.0, "volume": 0, "oi": 0})
        st["oi"] = oi
        if oi > 0:
            self._last_oi[token] = oi
        self._emit(token, st)

    def _emit(self, token: str, st: dict) -> None:
        meta = self._token_meta.get(token)
        if not meta:
            return
        # Prefer the live value; fall back to the last known good OI so a
        # touchline (price-only) tick that arrives right after a reconnect — when
        # ``_state`` was cleared but no fresh 1510 frame has landed yet — still
        # carries the real OI instead of 0.
        oi = int(st.get("oi") or 0)
        if oi <= 0:
            oi = self._last_oi.get(token, 0)
        if oi <= 0:
            # No real OI is known for this token yet (brand-new subscription before
            # its first 1510 frame). Skip rather than persist a misleading 0-OI row.
            return
        tick = Tick(
            ts=datetime.now(timezone.utc),
            token=token,
            symbol=meta.name,
            expiry=meta.expiry,
            strike=meta.strike,
            option_type=meta.option_type,
            ltp=float(st["ltp"]),
            oi=oi,
            volume=int(st["volume"]),
            underlying=self._latest_underlying,
        )
        try:
            self._queue.put_nowait(tick)
        except asyncio.QueueFull:
            pass

    async def _heartbeat_watch(self) -> None:
        while not self._stopping.is_set() and self._connected.is_set():
            await asyncio.sleep(5.0)
            silent_for = time.time() - self._last_tick_at
            if silent_for > HEARTBEAT_TIMEOUT_SECONDS:
                log.warning("ws.heartbeat.timeout", silent_seconds=int(silent_for))
                self._connected.clear()
                return


def _chunks(items: list[dict], size: int) -> list[list[dict]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _classify_400(resp: httpx.Response) -> tuple[bool, str]:
    """Classify an XTS subscription 400 response.

    Returns ``(is_already_subscribed, detail)`` where ``is_already_subscribed``
    is True only when the body indicates the instrument is already on the feed
    (benign — data still flows). Any other 400 is a genuine rejection (expired
    token, unknown instrument, malformed request) that means no ticks will flow.
    ``detail`` is the trimmed XTS error description for logging.
    """
    detail = ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            detail = str(
                body.get("description")
                or body.get("result")
                or body.get("message")
                or body
            )
        else:
            detail = str(body)
    except Exception:
        try:
            detail = resp.text or ""
        except Exception:
            detail = ""
    detail = detail.strip()[:300]
    low = detail.lower()
    # "Exceeded Instrument Subscription Limit of 50. You have already subscribed
    # 50/50 ..." also contains "already ... subscrib" but is a REAL rejection
    # (server-side slots full, e.g. a symbol swap that never freed the old
    # universe) — no ticks flow for the refused instrument.
    is_limit = "limit" in low and ("exceed" in low or "50/50" in low)
    is_already = "already" in low and "subscrib" in low and not is_limit
    return is_already, detail


def _instrument_id(obj: dict) -> str:
    val = obj.get("ExchangeInstrumentID")
    if val is None:
        val = obj.get("exchangeInstrumentID")
    return str(val) if val is not None else ""


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None
