"""Symphony XTS market-data feed — async Socket.IO client with reconnect + heartbeat.

The Shrilakshmi Fintech / Symphony XTS market-data stream is delivered over
Socket.IO. Unlike a sync, thread-based broker SDK whose snapquote carried LTP,
OI and volume in a single tick, XTS:

* uses ``python-socketio``'s ``AsyncClient`` — so we run directly on the event
  loop with no worker-thread bridge;
* requires a **REST** subscription call (``/instruments/subscription``) per
  instrument rather than subscribing over the socket; and
* splits market data across message codes — **LTP/volume arrive on touchline
  (1501)** while **open interest arrives on its own event (1510)**. We subscribe
  each option to *both* and **merge** them, per instrument, into the single
  ``Tick`` shape the rest of the pipeline already expects. (Indices have no OI,
  so the index/spot instrument is subscribed to 1501 only.)

XTS prices are already in rupees (no ``/100`` paise scaling).

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
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode

import httpx
import socketio  # type: ignore

from ..auth import get_session_manager
from ..core.config import settings
from ..core.logging import get_logger
from ..core.time_utils import now_ist
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
# Bounds the WHOLE sio.connect() call. python-socketio's wait_timeout only limits
# the wait for the Socket.IO CONNECT packet after the Engine.IO transport is up —
# the aiohttp WebSocket handshake underneath is NOT covered, so a half-open TCP
# (NAT/conntrack drop with no RST) could park the supervisor here indefinitely.
CONNECT_TOTAL_TIMEOUT_S = 25.0
# Total budget for _subscribe_all. The per-instrument 400 fallback can degrade to
# ~93 sequential POSTs; at the old 30s/request that was a worst case of ~46 minutes
# inside _connect_once with feed_connected=false and every watchdog nudge a no-op.
SUBSCRIBE_TOTAL_BUDGET_S = 60.0
SUBSCRIBE_CALL_TIMEOUT_S = 10.0
TEARDOWN_TIMEOUT_S = 5.0

# Auth trouble is REPORTED, not fixed, from here: the feed client never logs in.
# During outage #2 the in-feed self-heal was one of five independent actors
# rotating the single-session token out from under each other every ~6 seconds.
# All it may do now is request recovery from the SessionSteward (the one rotation
# authority) — throttled locally so a tight reconnect loop doesn't spam requests.
AUTH_RECOVERY_REQUEST_COOLDOWN_S = 60.0
# After this many consecutive unanswered requests, also mark the session
# broker-rejected so /api/health stops claiming authenticated=true (observability
# only — nothing auto-rotates off that flag anymore).
AUTH_RECOVERY_REQUESTS_BEFORE_REJECTED = 3


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
        # Set by nudge_reconnect() to wake the supervisor's backoff sleep. Without
        # it a nudge while already disconnected was a pure no-op: clearing a clear
        # _connected changes nothing, and the backoff wait only watched _stopping —
        # which is how every watchdog escalation during outage #2 did nothing.
        self._kick = asyncio.Event()
        self._supervisor_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        # Outcome of the most recent _subscribe_all/swap: instruments actually
        # delivering (newly subscribed + already-subscribed). Unlike rt.tokens
        # (which deliberately keeps last-known-good), this is honest about NOW.
        self._live_subs: int = 0

        self._token_meta: dict[str, InstrumentToken] = {}
        # Per-instrument merged state: token -> {"ltp", "volume", "oi"}.
        self._state: dict[str, dict] = {}
        # Last known *good* (non-zero) open interest per token. Unlike ``_state`` this
        # is NOT cleared on reconnect, so a socket bounce (the XTS feed drops every
        # ~80s) does not make OI momentarily collapse to 0 and write false
        # "OI crashed to zero" snapshots. Cleared only on symbol swap.
        self._last_oi: dict[str, int] = {}
        # Last known *good* price/volume per token, same rationale as ``_last_oi``:
        # ``_state`` is cleared on every reconnect, so without these the first frame
        # after a bounce (typically a 1510 OI frame, which passes the OI gate via
        # ``_last_oi``) would persist a row with ltp=0/volume=0 — a phantom "premium
        # crashed to zero" that also flips the buildup label and poisons IV.
        # UNLIKE OI, price and cumulative volume do NOT carry across trading days, so
        # these are keyed to a session date and dropped when the IST date rolls over.
        self._last_ltp: dict[str, float] = {}
        self._last_volume: dict[str, int] = {}
        self._px_day: date | None = None
        # Current subscription set per message code (for unsubscribe): code -> [instrument dicts].
        self._sub_groups: dict[int, list[dict]] = {}
        self._last_tick_at: float = 0.0
        self._latest_underlying: float | None = None
        # Auth-recovery REQUEST throttle (the steward does the actual recovering).
        self._last_auth_request: float = 0.0
        self._auth_recovery_requests: int = 0

    # ------------------------------------------------ public

    @property
    def latest_underlying(self) -> float | None:
        return self._latest_underlying

    @property
    def is_feed_connected(self) -> bool:
        return self._connected.is_set()

    @property
    def supervisor_alive(self) -> bool:
        """True while the ws-supervisor task exists and has not finished.

        A dead supervisor is otherwise invisible: the strong reference on the
        instance suppresses even asyncio's GC-time "exception was never retrieved"
        warning, so outage forensics found NOTHING in the logs when it died. The
        steward polls this and rebuilds the client when it goes false.
        """
        return self._supervisor_task is not None and not self._supervisor_task.done()

    @property
    def last_ws_tick_at(self) -> float:
        """time.time() of the last real tick from the socket (0.0 before the first)."""
        return self._last_tick_at

    @property
    def live_subscription_count(self) -> int:
        return self._live_subs

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
        """Drop the live socket AND wake the supervisor so it reconnects now.

        Both halves matter. Clearing ``_connected`` drops a live socket; setting
        ``_kick`` wakes a supervisor parked in its backoff sleep. The old
        implementation only cleared the flag — a no-op whenever the feed was
        already disconnected, which is every situation a recovery nudge exists for.
        """
        log.info("ws.nudge_reconnect_requested")
        self._connected.clear()
        self._kick.set()

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
            self._prune_value_caches()
            log.info("ws.swap_subscription.deferred", symbol=new_symbol, token_count=len(new_tokens))
            return

        old_groups = {code: list(insts) for code, insts in self._sub_groups.items()}

        self._token_meta = {t.token: t for t in new_tokens}
        self._index_token = new_index_token
        self._index_segment = new_index_segment or SEG_NSECM
        self._active_symbol = new_symbol
        self._latest_underlying = None
        self._state.clear()
        self._prune_value_caches()

        new_groups = self._build_subscription_groups()
        try:
            token = get_session_manager().token
            for code, insts in old_groups.items():
                for chunk in _chunks(insts, MAX_INSTRUMENTS_PER_REQUEST):
                    await xts_client.unsubscribe(token, chunk, code, timeout=SUBSCRIBE_CALL_TIMEOUT_S)
            ok = 0  # newly subscribed
            already = 0  # benign "already subscribed"
            rejected = 0  # real 400 (incl. cap breach) — no data flows
            errors = 0  # non-400 transport errors
            reasons: Counter[str] = Counter()
            for code, insts in new_groups.items():
                for chunk in _chunks(insts, MAX_INSTRUMENTS_PER_REQUEST):
                    try:
                        await xts_client.subscribe(token, chunk, code, timeout=SUBSCRIBE_CALL_TIMEOUT_S)
                        ok += len(chunk)
                    except httpx.HTTPStatusError as he:
                        if he.response is not None and he.response.status_code == 400 and len(chunk) > 1:
                            for inst in chunk:
                                try:
                                    await xts_client.subscribe(token, [inst], code, timeout=SUBSCRIBE_CALL_TIMEOUT_S)
                                    ok += 1
                                except httpx.HTTPStatusError as he2:
                                    if he2.response is not None and he2.response.status_code == 400:
                                        is_already, detail = _classify_400(he2.response)
                                        if detail:
                                            reasons[detail] += 1
                                        if is_already:
                                            already += 1
                                        else:
                                            rejected += 1
                                            log.warning(
                                                "ws.swap.rejected",
                                                inst=self._describe_inst(inst),
                                                code=code, detail=detail,
                                            )
                                    else:
                                        errors += 1
                                        log.warning("ws.swap.inst_error", inst=self._describe_inst(inst), code=code)
                                except Exception:
                                    errors += 1
                        else:
                            raise
            self._sub_groups = new_groups
            self._live_subs = ok + already
            log.info(
                "ws.swap_subscription.subscribed",
                newly_subscribed=ok, already_present=already,
                rejected=rejected, errors=errors, reasons=dict(reasons) or None,
            )
            # Unlike the old code (which swallowed EVERY per-instrument 400 as a
            # benign "skipped"), a cap breach during a re-center is now surfaced and
            # healed. A partial breach silently drops (strike, side) instruments —
            # the "missing strike" symptom; if it was the broker's 50-instrument cap
            # (usually stale gateway slots the unsubscribe above didn't free), self-
            # heal to a fresh 0/50 session and re-subscribe the current window.
            if rejected or errors:
                log.warning(
                    "ws.swap.partial_truncation",
                    subscribed=ok + already, rejected=rejected, errors=errors,
                    reasons=dict(reasons) or None,
                    hint="some strikes were NOT re-subscribed after the ATM re-center "
                    "and will be missing from the chain until healed.",
                )
                if _has_cap_breach(reasons):
                    await self._request_auth_recovery("cap breach on swap")
        except Exception as e:
            log.warning("ws.swap_subscription.error", error=str(e))
        self._last_tick_at = time.time()
        log.info("ws.swap_subscription.done", symbol=new_symbol, token_count=len(new_tokens))

    # ------------------------------------------------ supervisor

    async def _supervisor(self) -> None:
        backoff = BACKOFF_INITIAL
        while not self._stopping.is_set():
            try:
                # A kick that arrived while we were connecting/connected is satisfied
                # by the connect itself — clear it so it can't skip the NEXT backoff.
                self._kick.clear()
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
            if await self._sleep_or_kick(backoff):
                return
            backoff = min(backoff * 2, BACKOFF_MAX)

    async def _sleep_or_kick(self, backoff: float) -> bool:
        """Back off, but wake early on a kick. Returns True when stopping.

        The old wait watched only ``_stopping``, so a watchdog nudge could not
        shorten a 60s backoff — recovery waited for a timer while the market moved.
        A kick also resets the caller's backoff (we return normally and the caller
        re-enters connect immediately with backoff untouched-for-this-round).
        """
        stop_wait = asyncio.create_task(self._stopping.wait())
        kick_wait = asyncio.create_task(self._kick.wait())
        try:
            done, _ = await asyncio.wait(
                {stop_wait, kick_wait}, timeout=backoff, return_when=asyncio.FIRST_COMPLETED
            )
            if stop_wait in done:
                return True
            if kick_wait in done:
                log.info("ws.backoff_kicked")
            return False
        finally:
            for t in (stop_wait, kick_wait):
                if not t.done():
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass

    async def _connect_once(self) -> None:
        sess = get_session_manager()
        if not sess.authenticated:
            # `authenticated` can be false because XTS rejected the token — not only
            # because nobody has logged in yet. Report it so the steward rotates;
            # parking silently is how a feed stays dead for a whole session.
            await self._request_auth_recovery("not authenticated")
            if not sess.authenticated:
                raise RuntimeError(
                    "Not authenticated yet. Use the dashboard login form to authenticate first."
                )

        tokens, spot = await self._tokens_provider()
        if not tokens:
            log.warning(
                "ws.no_tokens_to_subscribe",
                hint="the option universe resolved empty — usually a dead token "
                "failing the scripmaster fetch. Requesting auth recovery.",
            )
            # This state STARVES event-based recovery: with no instruments we never
            # POST /instruments/subscription, so the 'Invalid Token' 400 that would
            # otherwise surface the dead token never happens (production outage
            # 2026-08-03 19:55 -> 2026-08-04, full session).
            await self._request_auth_recovery("empty universe")
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
            # wait_timeout alone does NOT bound the underlying aiohttp websocket
            # handshake (only the Socket.IO CONNECT packet after it) — the outer
            # wait_for is what guarantees this call can never hang the supervisor.
            await asyncio.wait_for(
                sio.connect(
                    f"{origin}?{query}",
                    socketio_path=sio_path.lstrip("/"),
                    transports=["websocket"],
                    wait=True,
                    wait_timeout=CONNECT_TIMEOUT_SECONDS,
                ),
                timeout=CONNECT_TOTAL_TIMEOUT_S,
            )
        except Exception as e:
            log.error("ws.connect.failed", error=str(e))
            await self._teardown()
            # XTS bakes the token into the handshake query, so a dead token is
            # rejected HERE — before _subscribe_all ever runs. Report it; the
            # steward decides whether a rotation is warranted (a genuine network
            # outage must not become a login storm).
            await self._request_auth_recovery(f"handshake failed: {e}")
            raise RuntimeError(f"Socket.IO failed to connect: {e}") from e

        # Subscribe over REST now that the socket is open. Safe to call on every
        # (re)connect: if XTS already has these subscriptions on the appKey it
        # returns 400 "already subscribed" (tolerated below); if not, this restores
        # the stream. Either way data flows without tearing the connection down.
        # Budgeted: the per-instrument 400 fallback can serialize ~93 POSTs, and an
        # unbounded run here kept feed_connected=false for tens of minutes while
        # every recovery nudge was a no-op. On budget exhaustion we proceed with
        # whatever subscribed — partial data now beats a perfect subscription later;
        # the heartbeat and the steward judge the outcome.
        try:
            await asyncio.wait_for(self._subscribe_all(), timeout=SUBSCRIBE_TOTAL_BUDGET_S)
        except asyncio.TimeoutError:
            log.error(
                "ws.subscribe.budget_exceeded",
                budget_s=SUBSCRIBE_TOTAL_BUDGET_S,
                hint="proceeding with the instruments that made it; the heartbeat "
                "will drop the socket if nothing actually flows.",
            )

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
            # Bounded: a disconnect against a half-open TCP can hang, and this runs
            # OUTSIDE the supervisor's try — an unbounded await here wedged the loop.
            await asyncio.wait_for(sio.disconnect(), timeout=TEARDOWN_TIMEOUT_S)
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

    def _describe_inst(self, inst: dict) -> str:
        """Readable label (e.g. '24300PE') for a subscription instrument dict, so
        cap-rejection logs name the exact (strike, side) that went dark."""
        iid = inst.get("exchangeInstrumentID")
        meta = self._token_meta.get(str(iid))
        if meta is not None:
            return f"{meta.strike}{meta.option_type}"
        if self._index_token and str(iid) == str(self._index_token):
            return "INDEX"
        return str(iid)

    async def _subscribe_all(self, _rearmed: bool = False) -> None:
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
                        await xts_client.subscribe(token, chunk, code, timeout=SUBSCRIBE_CALL_TIMEOUT_S)
                        ok += len(chunk)
                    except httpx.HTTPStatusError as he:
                        if he.response is not None and he.response.status_code == 400 and len(chunk) > 1:
                            # Retry one at a time so new instruments get through.
                            for inst in chunk:
                                try:
                                    await xts_client.subscribe(token, [inst], code, timeout=SUBSCRIBE_CALL_TIMEOUT_S)
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
                                                inst=self._describe_inst(inst),
                                                code=code,
                                                detail=detail,
                                            )
                                    else:
                                        errors += 1
                                        log.warning("ws.subscribe.inst_error", inst=self._describe_inst(inst), code=code)
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
            self._live_subs = live
            # MEASURED IN PRODUCTION 2026-08-11: the gateway cycles the socket
            # every ~67 s, and after a reconnect the NSEFO options broadcast is
            # DEAD no matter what: "Instrument Already Subscribed" delivers
            # nothing, and even a same-session unsubscribe→resubscribe cycle
            # (tried first — newly-subscribed instruments still stream nothing)
            # does not re-arm it. Only a FRESH LOGIN arms delivery — which is
            # what the pre-steward code did on every cycle, and why it survived
            # this gateway for weeks. So: free the broker-side slots, then ask
            # the steward for a rotation. The steward remains the ONE rotation
            # authority (floor + burst budget + circuit all still apply) — this
            # is the bounded, single-actor version of the old behaviour, not a
            # return of the outage-#2 login storm. Tune LOGIN_FLOOR_S /
            # LOGIN_BURST_MAX to the measured cycle cadence.
            if ok == 0 and already > 0 and not _rearmed:
                log.warning(
                    "ws.subscribe.rearm",
                    already_present=already,
                    hint="already-subscribed after a gateway socket cycle — "
                    "broadcast re-arms only on a fresh login; freeing slots and "
                    "requesting a steward rotation",
                )
                for code, insts in groups.items():
                    for chunk in _chunks(insts, MAX_INSTRUMENTS_PER_REQUEST):
                        try:
                            await xts_client.unsubscribe(
                                token, chunk, code, timeout=SUBSCRIBE_CALL_TIMEOUT_S
                            )
                        except Exception:
                            # Best-effort: a failed unsubscribe leaves us no
                            # worse off than the dead broadcast we already have.
                            pass
                await self._request_auth_recovery(
                    "gateway socket cycle: options broadcast re-arms only on a fresh login"
                )
                return
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
                # Auth failure (every instrument 'Invalid Token') → the steward
                # rotates to a fresh token and reconnects. The same recovery
                # clears 'Exceeded Instrument Subscription Limit' — a fresh
                # session starts with 0/50 slots, releasing stale subscriptions
                # a swap failed to free on the gateway.
                if rejected and any(
                    "token" in r.lower() or ("limit" in r.lower() and "exceed" in r.lower())
                    for r in reasons
                ):
                    await self._request_auth_recovery("all subscriptions rejected")
            else:
                # A FULLY clean subscribe is the broker's positive proof-of-life
                # for this token: clear the rejected flag and the request streak.
                if not (rejected or errors):
                    self._auth_recovery_requests = 0
                    get_session_manager().mark_broker_ok()
                log.info(
                    "ws.subscribe.success",
                    instruments=total,
                    newly_subscribed=ok,
                    already_present=already,
                    rejected=rejected,
                    errors=errors,
                    reasons=dict(reasons) or None,
                )
                # A PARTIAL failure (feed stays live because some instruments
                # succeeded, but others were rejected) silently drops those
                # strikes: they never emit ticks, so their rows never reach
                # option_oi_snapshots and /api/oi-change + /api/option-chain
                # totals come out truncated. This used to hide under
                # subscribe.success — surface it loudly so a competing session
                # (a 2nd backend on the same XTS appKey) or a partial breach of
                # the broker's 50-instrument cap is visible instead of quiet.
                if rejected or errors:
                    log.warning(
                        "ws.subscribe.partial_truncation",
                        instruments=total,
                        subscribed=live,
                        rejected=rejected,
                        errors=errors,
                        reasons=dict(reasons) or None,
                        hint="some instruments were NOT subscribed — their strikes "
                        "will be missing from OI totals. Usual cause: a 2nd backend "
                        "on the same XTS appKey stealing the session, or STRIKE_WINDOW "
                        "exceeding the broker's 50-instrument cap.",
                    )
                    # If the rejection was the broker's 50-instrument cap (usually
                    # stale gateway slots a prior ATM re-center never freed), ask
                    # for a rotation: a fresh session starts at 0/50 and
                    # re-subscribes the full window.
                    if _has_cap_breach(reasons):
                        await self._request_auth_recovery("cap breach on subscribe")
        except Exception as e:
            # Don't drop the connection on a subscribe hiccup; if no data flows the
            # heartbeat will recover. Avoids a reconnect storm on transient errors.
            log.error("ws.subscribe.error", error=str(e))

    async def _request_auth_recovery(self, reason: str) -> None:
        """Report auth trouble to the SessionSteward — the ONE rotation authority.

        This used to log in directly ("self-heal"). During outage #2 that made the
        feed one of five independent actors rotating the single-session token out
        from under each other every ~6 seconds for 14.6 hours; its give-up latch
        then flipped ``authenticated`` false, which re-armed the frontend's
        auto-login and closed the livelock. The feed now only ASKS; the steward
        rotates under a global rate floor and verifies the outcome.
        """
        now = time.time()
        since = now - self._last_auth_request
        if since < AUTH_RECOVERY_REQUEST_COOLDOWN_S:
            return
        self._last_auth_request = now
        self._auth_recovery_requests += 1
        log.warning(
            "ws.auth_recovery_requested",
            reason=reason, consecutive=self._auth_recovery_requests,
        )
        if self._auth_recovery_requests >= AUTH_RECOVERY_REQUESTS_BEFORE_REJECTED:
            # Observability only: /api/health should stop claiming
            # authenticated=true while recovery keeps not landing. Nothing
            # auto-rotates off this flag anymore.
            get_session_manager().mark_broker_rejected(
                f"{self._auth_recovery_requests} auth-recovery requests unanswered"
            )
        try:
            from .session_steward import get_steward

            steward = get_steward()
            if steward is not None:
                steward.request_recovery(f"ws:{reason}")
        except Exception as e:  # pragma: no cover — reporting must never hurt
            log.warning("ws.auth_recovery_request_failed", error=str(e))

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
        self._remember_px(token, ltp, vol)
        self._emit(token, st)

    def _prune_value_caches(self) -> None:
        """Drop cached OI/price/volume for tokens outside the NEW subscription set.

        Previously these were cleared wholesale on every swap. Most swaps are ATM-drift
        re-centres of the SAME symbol, where the windows overlap heavily: wiping the
        memory meant a retained strike that XTS answers with "already subscribed"
        (so it may not resend a snapshot immediately) had no last-known OI, and
        ``_emit`` skipped it until its next OI update — a self-inflicted gap.
        Keys are exchange instrument ids, globally unique, so a retained entry can
        never be attributed to a different contract.
        """
        keep = set(self._token_meta)
        self._last_oi = {k: v for k, v in self._last_oi.items() if k in keep}
        self._last_ltp = {k: v for k, v in self._last_ltp.items() if k in keep}
        self._last_volume = {k: v for k, v in self._last_volume.items() if k in keep}

    def _remember_px(self, token: str, ltp: float | None, vol: int | None) -> None:
        """Record the last known good price/volume, scoped to the IST trading day.

        Price and cumulative volume reset every session, so a carry-forward must never
        cross a day boundary (unlike open interest, which genuinely does carry over).
        """
        today = now_ist().date()
        if self._px_day != today:
            self._px_day = today
            self._last_ltp.clear()
            self._last_volume.clear()
        if ltp is not None and ltp > 0:
            self._last_ltp[token] = float(ltp)
        if vol is not None and vol > 0:
            self._last_volume[token] = int(vol)

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

        # Same reasoning as OI, for price: ``_state`` is wiped on reconnect, so a
        # post-bounce OI-first frame would otherwise stamp ltp=0 (a phantom "premium
        # went to zero" that flips the buildup label and yields a garbage IV).
        # Fall back to the last good same-day price; if none is known, persist NULL
        # rather than 0 — NULL means "price unknown" and every consumer already
        # handles it, whereas 0 is indistinguishable from a real quote. We must NOT
        # skip the row: a genuinely untraded strike still has real OI that belongs in
        # the totals.
        ltp_val = float(st.get("ltp") or 0.0)
        ltp: float | None = ltp_val if ltp_val > 0 else self._last_ltp.get(token)
        # Cumulative traded volume never decreases within a session, so a lower value
        # after a reconnect is a reset artifact, not a real number.
        vol = int(st.get("volume") or 0)
        vol = max(vol, self._last_volume.get(token, 0))

        tick = Tick(
            ts=datetime.now(timezone.utc),
            token=token,
            symbol=meta.name,
            expiry=meta.expiry,
            strike=meta.strike,
            option_type=meta.option_type,
            ltp=ltp,
            oi=oi,
            volume=vol,
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


def _has_cap_breach(reasons: "Counter[str]") -> bool:
    """True if any XTS 400 reason indicates the broker's ~50-instrument cap."""
    return any(
        "limit" in r.lower() and ("exceed" in r.lower() or "50/50" in r.lower())
        for r in reasons
    )


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
