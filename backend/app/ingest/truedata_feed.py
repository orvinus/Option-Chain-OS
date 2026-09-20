"""TrueData realtime WebSocket feed — the XTS ``OptionFeedClient`` replacement.

Exposes the SAME public surface as ws_client.OptionFeedClient so the steward,
/api/health, the spot refresher and feed_factory need no special-casing:

    start() stop() nudge_reconnect() swap_subscription()
    latest_underlying  is_feed_connected  supervisor_alive
    last_ws_tick_at    live_subscription_count

What is deliberately DIFFERENT from the XTS client, and why:

* **No 1501/1510 split-merge.** TrueData puts LTP, volume, OHLC, OI and bid/ask
  in ONE frame, so the ``_state``/``_last_oi`` merge machinery disappears. The
  defensive guards it wrapped (never store OI<=0, never store LTP as 0, volume
  monotonic) are kept verbatim — they defend against the vendor, not against the
  merge.

* **Subscriptions are on-socket, not REST.** ``addsymbol``/``removesymbol`` with
  batched arrays, so the XTS 400-sniffing, chunk-fallback and cap-breach
  detection all die. The budget knows the ceiling up front instead.

* **A hold-last refresher exists.** This is the single biggest behavioural
  difference between the vendors and it is easy to miss: TrueData sends a trade
  frame ONLY WHEN A TRADE HAPPENS, whereas XTS pushed OI roughly once a minute
  regardless. Without the refresher, a deep-OTM strike that does not trade for
  twenty minutes produces zero rows for twenty minutes — and those are precisely
  the strikes a wing/OI analysis cares about. See ``_hold_last``.

* **Liveness keys on the vendor heartbeat (5-6s), not on tick silence.** Tick
  silence is NORMAL (off-session, illiquid strikes); conflating it with a dead
  socket is what made the XTS 30s no-tick heuristic flap. Detection improves
  from ~180s to ~25s and works outside market hours.

* **A drain-only reader.** The vendor explicitly warns that a slow consumer
  overflows its server-side send buffer and it then DROPS PACKETS SILENTLY —
  the worst possible failure for an OI product. The socket reader therefore does
  nothing but read into an internal queue; all parsing happens elsewhere.

Wire-format caution: the vendor's own documentation contradicts itself on frame
widths (trade frames are 15 fields on one page and 19 on the next; touchline
rows are 17 fields in three samples and 18 in a fourth, with the prose listing a
field the samples do not contain). Indices 0..14 of a trade frame are consistent
across every sample and are parsed positionally; everything beyond is optional
and guarded. Touchline fields at index >= 11 are NOT trusted for OI until
scripts/probe_truedata_ws.py confirms them, because misreading Turnover as OI is
a ~10,000x error that still looks like a plausible number.
"""
from __future__ import annotations

import asyncio
import json
import ssl
import time
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import websockets

from ..auth.td_session import SessionCoolingDown, get_td_session
from ..core.config import settings
from ..core.logging import get_logger
from ..core.time_utils import IST, ist_naive_to_utc
from ..market import td_identity as ident
from ..market.td_identity import SymbolIdMap
from .subscription_budget import Priority, Slot, SubscriptionBudget
from .types import Tick

log = get_logger("td_feed")

# ---------------------------------------------------------------- constants

# Vendor heartbeat is documented every 5-6s and measured at ~5s. Four missed
# beats is the timeout: three (18s) is too tight for a GC pause plus a WARP
# proxy hiccup, five (30s) merely reproduces the XTS latency we are escaping.
HEARTBEAT_TIMEOUT_S = 22.0
HEARTBEAT_WATCH_INTERVAL_S = 3.0

BACKOFF_INITIAL_S = 2.0
BACKOFF_MAX_S = 60.0
CONNECT_TIMEOUT_S = 20.0
LOGIN_TIMEOUT_S = 20.0
TEARDOWN_TIMEOUT_S = 5.0

# Internal queue between the drain-only reader and the parser. Sized so a parser
# stall is absorbed for seconds rather than milliseconds, but still bounded:
# unbounded here just moves the vendor's buffer problem into our heap.
READER_QUEUE_MAX = 50_000

# Vendor error strings (there are no numeric codes anywhere in the API).
# Matched case-insensitively as substrings.
_REJECTION_MARKERS = (
    "user already connected",
    "user subscription expired",
    "invalid username",
    "invalid password",
    "the user name or password is incorrect",
)
_LIMIT_MARKER = "symbol limit reached"

# Trade-frame indices. Stable across BOTH the 15-field (bid/ask off) and
# 19-field (bid/ask on) layouts, which share an identical prefix.
_T_SYMBOL_ID = 0
_T_TIMESTAMP = 1
_T_LTP = 2
_T_LTQ = 3
_T_ATP = 4
_T_VOLUME = 5      # TTQ — cumulative day volume
_T_OPEN = 6
_T_HIGH = 7
_T_LOW = 8
_T_PREV_CLOSE = 9
_T_OI = 10
_T_PREV_OI = 11
_T_TURNOVER = 12
_T_SPECIAL = 13
_T_SEQ = 14
_TRADE_MIN_FIELDS = 15


def _f(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def _i(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _parse_vendor_ts(v) -> datetime | None:
    """Vendor stamps are IST-naive ISO (``2026-08-13T09:42:02``)."""
    if not v:
        return None
    s = str(v).strip().replace(" ", "T")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return ist_naive_to_utc(datetime.strptime(s, fmt))
        except ValueError:
            continue
    return None


class _ContractState:
    """Last known values for one contract, for the hold-last refresher."""

    __slots__ = ("symbol", "expiry", "strike", "option_type", "ltp", "oi", "volume", "ts")

    def __init__(self, symbol: str, expiry: date, strike: int, option_type: str) -> None:
        self.symbol = symbol
        self.expiry = expiry
        self.strike = strike
        self.option_type = option_type
        self.ltp: float | None = None
        self.oi: int = 0
        self.volume: int = 0
        self.ts: datetime | None = None


class TrueDataFeedClient:
    """Live TrueData feed emitting ``Tick``s onto a queue."""

    def __init__(
        self,
        out_queue: "asyncio.Queue[Tick]",
        tokens_provider,
        index_token: str | None = None,
        active_symbol: str | None = None,
        index_segment: int | None = None,
        vendor_tag: str = "truedata",
        shadow: bool = False,
    ) -> None:
        self._queue = out_queue
        self._tokens_provider = tokens_provider
        self._active_symbol = (active_symbol or settings.underlying_symbol or "NIFTY").upper()
        # index_token/index_segment are XTS concepts (numeric ids + segment ints).
        # Accepted for call-site compatibility with OptionFeedClient and ignored:
        # TrueData subscribes index spot BY NAME on the same socket.
        self._index_token = index_token
        self._index_segment = index_segment
        self._vendor = vendor_tag
        self._shadow = shadow

        self._sess = get_td_session()
        self._budget = SubscriptionBudget()
        self._ids = SymbolIdMap()

        self._ws = None
        self._connected = asyncio.Event()
        self._stopping = asyncio.Event()
        self._kick = asyncio.Event()
        self._supervisor_task: asyncio.Task | None = None
        self._reader_task: asyncio.Task | None = None
        self._parser_task: asyncio.Task | None = None
        self._hb_task: asyncio.Task | None = None
        self._hold_task: asyncio.Task | None = None
        self._reader_q: "asyncio.Queue[str]" = asyncio.Queue(maxsize=READER_QUEUE_MAX)

        # Spot per underlying, from each chain's own reference instrument. One
        # shared value was fine while the socket carried a single chain; with
        # pinned chains it would stamp SENSEX's index on NIFTY rows.
        self._spot_by_symbol: dict[str, float] = {}
        # The dashboard's chain as last handed to swap_subscription, and the
        # pinned chains (TRUEDATA_PINNED_SYMBOLS) with the spot each is centred on.
        self._active_tokens: list = []
        self._pinned_tokens: dict[str, list] = {}
        self._pinned_centre: dict[str, float] = {}
        self._pinned_task: asyncio.Task | None = None
        self._reconcile_lock = asyncio.Lock()
        self._last_tick_at: float = 0.0
        self._last_heartbeat_at: float = 0.0
        self._connect_started_at: float = 0.0

        # Per-contract last-known state, keyed by td: token.
        self._state: dict[str, _ContractState] = {}
        # Day-scoped price/volume carry (OI legitimately carries over a day
        # boundary; a cumulative volume or a stale premium does not).
        self._px_day: date | None = None

        # Counters surfaced on health — every drop is COUNTED, never silent.
        self.dropped_queue_full = 0
        self.dropped_unmapped = 0
        self.seq_gaps = 0
        self.frames_seen = 0
        self._last_seq: dict[str, int] = {}

    # ------------------------------------------------------------- surface
    @property
    def latest_underlying(self) -> float | None:
        """Spot of the ACTIVE (dashboard) symbol — what runtime.latest_spot means."""
        return self._spot_by_symbol.get(self._active_symbol)

    def _set_spot(self, reference_name: str, ltp: float) -> None:
        sym = ident.reference_symbol(reference_name)
        if sym:
            self._spot_by_symbol[sym] = ltp

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
        return self._budget.live_count

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
        self._supervisor_task = asyncio.create_task(self._supervisor(), name="td-ws-supervisor")

    def nudge_reconnect(self) -> None:
        """Drop the socket and wake the supervisor out of its backoff."""
        self._connected.clear()
        self._kick.set()

    async def stop(self) -> None:
        """Graceful shutdown — MUST log out on the socket.

        A dirty disconnect leaves the vendor believing we are still connected,
        and the next start lands inside a ~60s "User Already Connected" window.
        Since a container restart is exactly this path, skipping the logout would
        make every deploy pay a wedge.
        """
        self._stopping.set()
        self._kick.set()
        await self._socket_logout()
        for task in (self._supervisor_task, self._reader_task, self._parser_task,
                     self._hb_task, self._hold_task, self._pinned_task):
            if task is not None and not task.done():
                task.cancel()
        await self._teardown()

    # ------------------------------------------------------------ supervisor
    async def _supervisor(self) -> None:
        backoff = BACKOFF_INITIAL_S
        while not self._stopping.is_set():
            try:
                await self._connect_once()
                backoff = BACKOFF_INITIAL_S
            except SessionCoolingDown as e:
                # Sleep the EXACT remainder. Retrying inside the cool-down is
                # what turns a single wedge into a permanent one.
                log.info("td_feed.cooldown_wait", remaining_s=round(e.remaining_s, 1))
                await self._sleep_or_kick(e.remaining_s + 1.0)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("td_feed.connect_failed", error=str(e)[:200])
            if self._stopping.is_set():
                return
            await self._sleep_or_kick(backoff)
            backoff = min(BACKOFF_MAX_S, backoff * 2)

    async def _sleep_or_kick(self, seconds: float) -> None:
        self._kick.clear()
        try:
            await asyncio.wait_for(self._kick.wait(), timeout=max(0.1, seconds))
        except asyncio.TimeoutError:
            pass
        self._kick.clear()

    async def _connect_once(self) -> None:
        sess = self._sess
        sess.require_credentials()
        # Single-flight: two concurrent connects reject each other, and under
        # rejection semantics that is indistinguishable from a genuine wedge.
        async with sess.connect_guard():
            sess.mark_connecting()
            self._connect_started_at = time.monotonic()
            # Credentials go in RAW — deliberately NOT percent-encoded.
            #
            # The vendor's realtime endpoint does not URL-decode the query
            # string; it compares the literal characters. Percent-encoding
            # therefore turns a password containing "@" into one containing
            # "%40" and the server answers "Invalid User Credentials" — which
            # reads exactly like a wrong password or a dead subscription, so it
            # cost most of an afternoon to find. Proved back-to-back on
            # 2026-08-21, same account, same port 8084, seconds apart:
            #     password=ayush@1075   -> {"success":true, maxsymbols:50, ...}
            #     password=ayush%401075 -> {"success":false,"Invalid User Credentials"}
            # Their own reference client (wstest.truedata.in) also sends it raw.
            #
            # The REST hosts are unaffected: auth.truedata.in takes a POST form
            # body, which IS decoded normally.
            _bad = set(sess.password) & set("&=#?")
            if _bad:
                # These genuinely cannot survive a query string un-encoded, and
                # encoding them is what the vendor mishandles — so it is
                # unfixable at our end. Say so loudly instead of emitting a
                # silently-wrong URL.
                log.error(
                    "td_feed.password_unsafe_for_query",
                    chars="".join(sorted(_bad)),
                    hint="TrueData does not URL-decode the WS query string; ask "
                         "the vendor to reissue a password without these characters.",
                )
            uri = (
                f"wss://{settings.truedata_ws_host}:{settings.truedata_ws_port}"
                f"?user={sess.user}&password={sess.password}"
            )
            proxy = (settings.truedata_proxy or "").strip() or None
            # `proxy` defaults to True in websockets>=13, which silently reads
            # ALL_PROXY/HTTPS_PROXY from the environment. Always explicit: an env
            # var must never be able to reroute a production market-data feed.
            self._ws = await asyncio.wait_for(
                websockets.connect(
                    uri,
                    proxy=proxy,
                    ssl=ssl.create_default_context(),
                    ping_interval=None,   # vendor sends its own heartbeat
                    close_timeout=5.0,
                    max_queue=READER_QUEUE_MAX,
                ),
                timeout=CONNECT_TIMEOUT_S,
            )
            raw = await asyncio.wait_for(self._ws.recv(), timeout=LOGIN_TIMEOUT_S)
            login = self._as_obj(raw) or {}
            low = json.dumps(login).lower()

            # The vendor's own success flag outranks the keyword list. Measured
            # 2026-08-21 on the paid account: the realtime service answered
            #   {"success":false,"message":"Invalid User Credentials",
            #    "maxsymbols":0,"subscription":null,"validity":"0001-01-01..."}
            # which matches NONE of _REJECTION_MARKERS ("invalid username" /
            # "invalid password" / "the user name or password is incorrect").
            # The login was therefore treated as good: mark_live() logged
            # `td_session.live maxsymbols=0`, addsymbol was sent to a socket the
            # server was already closing, and the steward looped logout →
            # 75 s cool-down → retry forever while reporting a live session.
            # Keying on `success` is wording-independent and catches every
            # future message the vendor invents.
            if isinstance(login, dict) and login.get("success") is False:
                msg = str(login.get("message") or "login refused").strip()
                sess.mark_rejected(msg)
                await self._teardown()
                raise RuntimeError(f"login rejected: {msg}")

            for marker in _REJECTION_MARKERS:
                if marker in low:
                    sess.mark_rejected(marker)
                    await self._teardown()
                    raise RuntimeError(f"login rejected: {marker}")

            sess.mark_live(login)
            problems = sess.entitlement_problems()
            if problems:
                # Loud, but not fatal: a degraded entitlement still produces data,
                # and killing the feed would turn a data-quality problem into an
                # outage. The steward and health surface it.
                for p in problems:
                    log.error("td_feed.entitlement", problem=p)

            self._budget.set_capacity(sess.maxsymbols)
            self._connected.set()
            self._last_heartbeat_at = time.monotonic()

        # Tasks are started OUTSIDE the connect lock so a slow first subscribe
        # cannot hold the guard and stall a concurrent recovery attempt.
        self._reader_task = asyncio.create_task(self._reader(), name="td-ws-reader")
        self._parser_task = asyncio.create_task(self._parser(), name="td-ws-parser")
        self._hb_task = asyncio.create_task(self._heartbeat_watch(), name="td-ws-heartbeat")
        if settings.truedata_hold_last_enabled:
            self._hold_task = asyncio.create_task(self._hold_last(), name="td-ws-holdlast")
        self._pinned_task = asyncio.create_task(self._pinned_watch(), name="td-ws-pinned")

        await self._resubscribe_all()

        # Hold the connection until something clears it.
        while self._connected.is_set() and not self._stopping.is_set():
            await asyncio.sleep(1.0)
        await self._teardown()

    async def _teardown(self) -> None:
        self._connected.clear()
        # Every acknowledgement is invalidated: nothing documents whether the
        # server keeps subscriptions across a reconnect, so we assume it does not
        # and replay the full desired set rather than diffing against state the
        # server may have forgotten.
        self._budget.reset_live()
        self._ids.clear()
        # Vendor sequence ids restart per connection: comparing a new
        # connection's seq against the old one manufactured a phantom gap per
        # contract per reconnect (and the dict grew forever).
        self._last_seq.clear()
        # _hold_last's loop condition does not watch _connected, so without an
        # explicit cancel every reconnect leaked one immortal 0.9s-tick
        # coroutine (observed: ~1 per reconnect × 551 reconnects/day).
        hold, self._hold_task = self._hold_task, None
        if hold is not None and not hold.done():
            hold.cancel()
        pinned, self._pinned_task = self._pinned_task, None
        if pinned is not None and not pinned.done() and pinned is not asyncio.current_task():
            pinned.cancel()
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await asyncio.wait_for(ws.close(), timeout=TEARDOWN_TIMEOUT_S)
            except Exception:
                pass

    async def _socket_logout(self) -> None:
        """``{"method":"logout"}`` — the CHEAP way out of a session.

        Documented and far preferable to the REST logoutRequest: it clears the
        session without the ~60s cool-down, so a graceful restart costs nothing.
        Best-effort by nature — if the socket is already gone, the dirty path and
        its cool-down are the fallback.
        """
        ws = self._ws
        if ws is None:
            return
        try:
            await asyncio.wait_for(ws.send(json.dumps({"method": "logout"})), timeout=3.0)
            await asyncio.sleep(0.5)
            log.info("td_feed.socket_logout.sent")
        except Exception as e:
            log.info("td_feed.socket_logout.failed", error=str(e)[:120])

    # ---------------------------------------------------------------- reader
    async def _reader(self) -> None:
        """Drain-only. Does NO work — see the vendor's backpressure warning."""
        ws = self._ws
        if ws is None:
            return
        try:
            async for raw in ws:
                try:
                    self._reader_q.put_nowait(raw)
                except asyncio.QueueFull:
                    # Our own parser is the bottleneck. Counted, never silent.
                    self.dropped_queue_full += 1
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("td_feed.reader.closed", error=str(e)[:160])
        finally:
            self._connected.clear()

    async def _parser(self) -> None:
        while not self._stopping.is_set():
            try:
                raw = await asyncio.wait_for(self._reader_q.get(), timeout=5.0)
            except asyncio.TimeoutError:
                if not self._connected.is_set():
                    return
                continue
            except asyncio.CancelledError:
                raise
            try:
                self._dispatch(self._as_obj(raw))
            except Exception as e:
                log.warning("td_feed.parse.error", error=str(e)[:160])

    @staticmethod
    def _as_obj(raw):
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except Exception:
                return None
        return raw

    def _dispatch(self, msg) -> None:
        if not isinstance(msg, dict):
            return
        # ANY decoded frame proves the socket is alive. The specific-key match
        # below never fired for the vendor's actual heartbeat spelling, so off
        # session (no trades to refresh liveness via _on_trade) the watchdog
        # tore down a healthy connection every ~26s — 551 forced reconnects
        # observed in one day (2026-08-18), each a fresh login + full
        # resubscribe and a wedge risk.
        self._last_heartbeat_at = time.monotonic()
        if "trade" in msg:
            self.frames_seen += 1
            self._on_trade(msg["trade"])
            return
        if "symbollist" in msg:
            self._on_symbollist(msg["symbollist"])
            return
        if "heartbeat" in msg or "Heartbeat" in msg:
            self._last_heartbeat_at = time.monotonic()
            return
        if "bidask" in msg or "bidaskL2" in msg:
            return  # carries no OI; nothing downstream stores it
        if "marketstatus" in msg or "MarketStatus" in msg:
            log.info("td_feed.market_status", payload=str(msg)[:200])
            return
        low = json.dumps(msg).lower()
        if _LIMIT_MARKER in low:
            # Should be unreachable: the budget never asks for more than
            # maxsymbols. If it fires, the ceiling we were told is wrong.
            log.error("td_feed.symbol_limit_reached", budget=self._budget.state().__dict__)

    # ------------------------------------------------------------- handlers
    def _on_symbollist(self, rows) -> None:
        """``addsymbol`` acknowledgement: symbol <-> vendor-id mapping.

        The rows also carry a touchline snapshot, which is tempting to seed
        state from — it would kill the cold-start blank chain. We deliberately do
        NOT read OI from it: the vendor's samples disagree on row width (17 vs
        18 fields) and on whether PrevOpenInterestClose is present at all, so an
        index >= 11 could be Turnover. Misreading turnover as OI is a ~10,000x
        corruption that still looks like a plausible number. Mapping only, until
        probe B1 settles the layout.
        """
        if not isinstance(rows, list):
            return
        mapped = 0
        for row in rows:
            if not isinstance(row, list) or len(row) < 2:
                continue
            name, vid = str(row[0]).strip(), str(row[1]).strip()
            if ident.is_reference_instrument(name):
                self._ids.bind_reference(vid, name)
                ltp = _f(row[3]) if len(row) > 3 else None
                if ltp and ltp > 0:
                    self._set_spot(name, ltp)
                mapped += 1
                continue
            slot = self._slot_by_vendor_symbol(name)
            if slot is not None:
                self._ids.bind(vid, slot.token)
                self._budget.mark_live([slot.token])
                mapped += 1
        log.info("td_feed.symbollist", rows=len(rows), mapped=mapped,
                 live=self._budget.live_count)

    def _slot_by_vendor_symbol(self, vendor_symbol: str) -> Slot | None:
        want = vendor_symbol.upper()
        for s in self._budget.desired():
            if s.vendor_symbol.upper() == want:
                return s
        return None

    def _on_trade(self, arr) -> None:
        if not isinstance(arr, list) or len(arr) < _TRADE_MIN_FIELDS:
            return
        self._last_tick_at = time.time()
        # Data frames prove liveness too: TrueData suppresses heartbeat frames
        # while trades stream, so a heartbeat-frames-only watchdog tears down a
        # healthy connection every ~26s all session (observed 2026-08-17: the
        # forced flap cycle, ~2s outage each, was the source of the seq gaps).
        self._last_heartbeat_at = time.monotonic()
        vid = str(arr[_T_SYMBOL_ID])

        ref = self._ids.reference_for(vid)
        if ref is not None:
            # Index / continuous future: price context only. It must NEVER become
            # a row — option_type would be 'IDX'/'FUT', which overflows CHAR(2)
            # and takes down the whole flush batch.
            ltp = _f(arr[_T_LTP])
            if ltp and ltp > 0:
                self._set_spot(ref, ltp)
            return

        token = self._ids.token_for(vid)
        if token is None:
            # A vendor id we never mapped — almost always a straggler from a
            # previous subscription. Guessing would attribute one contract's OI
            # to another, so drop and count.
            self.dropped_unmapped += 1
            return

        st = self._state.get(token)
        if st is None:
            slot = next((s for s in self._budget.desired() if s.token == token), None)
            parsed = ident.token_to_contract_key(token)
            if slot is None or parsed is None:
                self.dropped_unmapped += 1
                return
            sym, yymmdd, strike, ot = parsed.split(":")
            st = _ContractState(
                symbol=sym,
                expiry=datetime.strptime(yymmdd, "%y%m%d").date(),
                strike=int(strike),
                option_type=ot,
            )
            self._state[token] = st

        self._roll_day_caches()
        self._track_seq(vid, arr)

        oi = _i(arr[_T_OI]) * self._oi_scale(st.symbol)
        # Guard carried over verbatim from the XTS client: a subscribed, trading
        # option does not drop to literally zero OI intraday. A 0 here is a
        # partial or post-reconnect artifact — keep the last known value.
        if oi > 0:
            st.oi = oi
        elif st.oi <= 0:
            return  # never persist a zero-OI row

        ltp = _f(arr[_T_LTP])
        if ltp is not None and ltp > 0:
            st.ltp = ltp
        # else: keep the last known price. NULL, never 0 — a zero is
        # indistinguishable from a real quote downstream.

        vol = _i(arr[_T_VOLUME])
        st.volume = max(st.volume, vol)   # cumulative day volume is monotonic

        vts = _parse_vendor_ts(arr[_T_TIMESTAMP])
        now = datetime.now(timezone.utc)
        st.ts = now
        self._emit(token, st, now, vts)

    def _track_seq(self, vid: str, arr: list) -> None:
        """Sequence-gap counting — the only observable for silent feed loss.

        The docs do not say whether TickSeqNo is per-symbol or per-connection, so
        this counts it per-symbol; the probe settles which interpretation gives
        near-zero anomalies. Reported, never acted on: a gap means data was lost
        upstream, and there is nothing useful to do about it in-band.
        """
        if len(arr) <= _T_SEQ:
            return
        try:
            seq = int(arr[_T_SEQ])
        except (TypeError, ValueError):
            return
        prev = self._last_seq.get(vid)
        if prev is not None and seq != prev + 1:
            self.seq_gaps += 1
        self._last_seq[vid] = seq

    @staticmethod
    def _oi_scale(symbol: str) -> int:
        from ..market.symbols import get_registry

        entry = get_registry().get(symbol)
        exch = (entry.exchange if entry else "NSE").upper()
        if exch == "BSE":
            return max(1, int(settings.truedata_oi_scale_bse))
        if exch == "MCX":
            return max(1, int(settings.truedata_oi_scale_mcx))
        return max(1, int(settings.truedata_oi_scale_nse))

    def _roll_day_caches(self) -> None:
        today = datetime.now(IST).date()
        if self._px_day == today:
            return
        self._px_day = today
        for st in self._state.values():
            st.ltp = None
            st.volume = 0
            # st.oi deliberately survives: open interest carries across sessions.

    def _emit(self, token: str, st: _ContractState, ts: datetime,
              vendor_ts: datetime | None) -> None:
        tick = Tick(
            ts=ts,
            token=token,
            symbol=st.symbol,
            expiry=st.expiry,
            strike=st.strike,
            option_type=st.option_type,
            ltp=st.ltp,
            oi=st.oi,
            volume=st.volume,
            underlying=self._spot_by_symbol.get(st.symbol),
            origin="ws",       # transport, NOT vendor — the steward keys on this
            vendor=self._vendor,
            vendor_ts=vendor_ts,
        )
        try:
            self._queue.put_nowait(tick)
        except asyncio.QueueFull:
            self.dropped_queue_full += 1

    # ----------------------------------------------------------- background
    async def _heartbeat_watch(self) -> None:
        while not self._stopping.is_set() and self._connected.is_set():
            await asyncio.sleep(HEARTBEAT_WATCH_INTERVAL_S)
            if not self._last_heartbeat_at:
                continue
            silent = time.monotonic() - self._last_heartbeat_at
            if silent > HEARTBEAT_TIMEOUT_S:
                log.warning("td_feed.heartbeat.timeout", silent_s=round(silent, 1))
                self._connected.clear()
                self._sess.mark_disconnected("heartbeat timeout")
                return

    async def _hold_last(self) -> None:
        """Re-emit last-known state for contracts that did not trade.

        TrueData has NO periodic push: a frame arrives only on a trade. XTS's
        1510 event arrived roughly once a minute for every subscribed instrument
        whether it traded or not, which is why every strike currently gets a row
        every minute. Without this loop the illiquid wings simply vanish from the
        table between trades, and every wing analytic silently reads holes rather
        than flat values.

        Gated on the HEARTBEAT, not on tick arrival: heartbeat alive => socket
        alive => "nothing traded" is the honest truth. A dead socket stops the
        heartbeat, which stops this loop, so it can never manufacture freshness
        for a feed that is actually down — the steward judges on WS-origin rows,
        and this must not be able to lie to it.
        """
        period = max(1.0, _bucket_seconds() * 0.9)
        while not self._stopping.is_set():
            await asyncio.sleep(period)
            if not self._connected.is_set():
                continue
            if self.heartbeat_age_s is None or self.heartbeat_age_s > HEARTBEAT_TIMEOUT_S:
                continue
            if not self._in_session_window():
                # A closed market has no "last state to hold" — emitting here
                # manufactured option rows 24/7 (observed: rows at 00:00 and
                # on Sundays, ~3× table growth and polluted session replays).
                continue
            now = datetime.now(timezone.utc)
            emitted = 0
            for token in self._budget.live_tokens():
                st = self._state.get(token)
                if st is None or st.oi <= 0 or st.ts is None:
                    continue      # never traded today -> no row to hold
                if (now - st.ts).total_seconds() < period:
                    continue      # a real tick already covered this bucket
                st.ts = now
                self._emit(token, st, now, None)
                emitted += 1
            if emitted:
                log.debug("td_feed.hold_last", emitted=emitted)

    def _in_session_window(self, now: datetime | None = None) -> bool:
        """Is the ACTIVE symbol's market open?
        NSE/BSE equity-derivatives: the configured session (09:15 up to but not
        including the date's close — 15:40 since 2026-08-03) on weekdays that
        are not NSE holidays; commodities (MCX): 09:00–23:55 IST weekdays.
        Gates the hold-last refresher only — real vendor frames are always
        ingested.

        The old literal ``15:35`` end dropped the final five minutes of every
        session (incl. the settlement window) for every strike that did not
        trade, and the missing holiday check manufactured option rows all day
        on a weekday holiday whenever the socket was up."""
        from ..core.holidays import is_nse_holiday
        from ..core.time_utils import SESSION_OPEN_MIN, session_close_min
        from ..market.symbols import get_registry

        now = now or datetime.now(IST)
        if now.weekday() > 4:
            return False
        entry = get_registry().get(self._active_symbol)
        hm = now.hour * 60 + now.minute
        if entry is not None and entry.kind == "commodity":
            return 9 * 60 <= hm <= 23 * 60 + 55
        if is_nse_holiday(now.date()):
            return False
        return SESSION_OPEN_MIN <= hm < session_close_min(now.date())

    # --------------------------------------------------------- subscriptions
    async def _resubscribe_all(self) -> None:
        tokens, spot = await self._tokens_provider()
        if not tokens:
            # Holding a connected-but-empty socket indefinitely looks healthy
            # to every liveness check while delivering nothing. Drop the
            # connection so the supervisor retries (and the provider gets a
            # fresh chance to resolve the universe).
            log.warning("td_feed.resubscribe.empty_universe_reconnect")
            self._connected.clear()
            self._sess.mark_disconnected("empty universe at subscribe")
            return
        await self.swap_subscription(tokens, None, self._active_symbol, None, spot=spot)

    async def swap_subscription(
        self,
        new_tokens: list,
        new_index_token: str | None = None,
        new_symbol: str | None = None,
        new_index_segment: int | None = None,
        spot: float | None = None,
    ) -> None:
        """Replace the desired set and reconcile — a diff, not a teardown.

        Under XTS this had to unsubscribe-then-resubscribe through REST while
        sniffing 400 bodies for a masked cap error. Here it is a set difference
        against the budget, and the ceiling is known in advance.
        """
        if new_symbol:
            self._active_symbol = new_symbol.upper()
        if spot is not None:
            self._spot_by_symbol[self._active_symbol] = spot
        self._active_tokens = list(new_tokens or [])
        try:
            await self._refresh_pinned()
        except Exception as e:  # a pinned chain must never block the active one
            log.warning("td_feed.pinned_refresh_error", error=str(e)[:160])
        await self._reconcile()

    async def _reconcile(self) -> None:
        """Plan active + pinned chains against the budget and send the diff.
        Serialised: the pinned watcher and a symbol switch must not interleave
        their add/remove batches."""
        async with self._reconcile_lock:
            await self._reconcile_locked()

    async def _reconcile_locked(self) -> None:
        slots = self._slots_for(self._active_tokens)
        self._budget.plan(slots)
        # Prune per-contract state for contracts no longer wanted, so an ATM
        # re-centre cannot leave the hold-last loop re-emitting a strike we
        # stopped subscribing to (which would look like live data forever).
        wanted = {s.token for s in self._budget.desired()}
        for token in list(self._state):
            if token not in wanted:
                self._state.pop(token, None)

        if not self._connected.is_set():
            return  # the next connect replays the full desired set

        add, remove = self._budget.diff()
        if remove:
            # remove = (token, vendor_symbol) pairs: the VENDOR string goes on
            # the wire, the token is bookkept. (Sending internal 'td:…' tokens
            # here was a silent no-op that leaked every re-centred edge strike
            # on the vendor side — fixed 2026-08-18.)
            await self._send_batched("removesymbol", [v for _, v in remove])
            self._budget.mark_dropped([t for t, _ in remove])
            for t, _v in remove:
                # Unbind stale vendor-id mappings so stragglers count as
                # unmapped drops rather than resurrecting a removed contract.
                self._ids.drop_token(t)
        if add:
            await self._send_batched("addsymbol", [s.vendor_symbol for s in add])
        log.info("td_feed.swap", symbol=self._active_symbol,
                 desired=len(self._budget.desired()), added=len(add), removed=len(remove),
                 pinned=self._pinned_summary())

    def _pinned_summary(self) -> dict[str, dict[str, int]]:
        """Per pinned symbol: contracts wanted vs actually planned (budget)."""
        planned: dict[str, int] = {}
        for s in self._budget.desired():
            if s.priority is Priority.ELASTIC:
                planned[s.symbol] = planned.get(s.symbol, 0) + 1
        return {
            sym: {"wanted": len(toks) + 1, "planned": planned.get(sym, 0)}
            for sym, toks in self._pinned_tokens.items()
        }

    async def _refresh_pinned(self) -> bool:
        """Resolve each pinned chain around its OWN spot. True when anything
        changed. Re-centres like the ATM drift watcher: once the spot has
        moved DRIFT_THRESHOLD_FRACTION of the window away from the centre."""
        from ..market.scripmaster_td import resolve_td_option_universe
        from ..market.symbols import get_registry
        from ..services.spot_fallback import db_last_underlying
        from .atm_drift_watch import DRIFT_THRESHOLD_FRACTION, _effective_window

        wanted = [s for s in settings.truedata_pinned_symbol_list if s != self._active_symbol]
        changed = False
        for sym in list(self._pinned_tokens):
            if sym not in wanted:
                self._pinned_tokens.pop(sym, None)
                self._pinned_centre.pop(sym, None)
                changed = True
        if not wanted:
            return changed

        window = await _effective_window()
        for sym in wanted:
            entry = get_registry().get(sym)
            if entry is None or not entry.fno_eligible:
                continue
            spot = self._spot_by_symbol.get(sym)
            if not spot:
                try:
                    spot = await db_last_underlying(sym)
                except Exception:
                    spot = None
            have = sym in self._pinned_tokens
            if not spot:
                if not have:
                    # Subscribe the index alone; its first tick gives the spot
                    # the next watch pass centres the chain on.
                    self._pinned_tokens[sym] = []
                    self._pinned_centre[sym] = 0.0
                    changed = True
                continue
            step = (entry.strike_step if entry.strike_step > 0 else None) or settings.strike_step or 50
            centre = self._pinned_centre.get(sym) or 0.0
            if have and self._pinned_tokens[sym] and centre and \
                    abs(spot - centre) < window * step * DRIFT_THRESHOLD_FRACTION:
                continue
            try:
                tokens, _expiries = await resolve_td_option_universe(spot=spot, symbol=sym)
            except Exception as e:
                log.warning("td_feed.pinned_resolve_error", symbol=sym, error=str(e)[:160])
                continue
            self._pinned_tokens[sym] = tokens
            self._pinned_centre[sym] = float(spot)
            changed = True
            log.info("td_feed.pinned_centred", symbol=sym, spot=round(float(spot), 2),
                     contracts=len(tokens))
        return changed

    async def _pinned_watch(self) -> None:
        """Keep pinned chains centred while the socket is up."""
        while not self._stopping.is_set() and self._connected.is_set():
            await asyncio.sleep(max(15.0, float(settings.truedata_pinned_recentre_s)))
            if not self._connected.is_set():
                return
            try:
                if await self._refresh_pinned():
                    await self._reconcile()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("td_feed.pinned_watch_error", error=str(e)[:160])

    def _slots_for(self, tokens: list) -> list[Slot]:
        """Budget slots: the active chain (REFERENCE/ACTIVE), then every pinned
        chain at ELASTIC — pinned chains only ever take what is left over, so a
        small plan keeps the viewed chain exactly as it was."""
        slots = self._chain_slots(self._active_symbol, tokens, pinned=False)
        for sym, toks in self._pinned_tokens.items():
            if sym != self._active_symbol:
                slots.extend(self._chain_slots(sym, toks, pinned=True))
        return slots

    def _chain_slots(self, symbol: str, tokens: list, *, pinned: bool) -> list[Slot]:
        """InstrumentToken list -> budget slots, plus the reference instrument."""
        from ..market.symbols import get_registry

        entry = get_registry().get(symbol)
        slots: list[Slot] = []

        # Reference price first. For the active chain it is priority 0 and
        # never evicted, because without spot there is no ATM and the whole
        # chain is unanchored; for a pinned chain it is the first elastic slot
        # (rank -1 sorts ahead of every strike).
        if entry is not None and entry.kind == "commodity":
            ref_name = ident.continuous_future_name(symbol)
        else:
            ref_name = ident.index_ws_name(symbol)
        slots.append(Slot(
            vendor_symbol=ref_name,
            token=ident.index_token(symbol),
            priority=Priority.ELASTIC if pinned else Priority.REFERENCE,
            symbol=symbol,
            moneyness_rank=-1,
        ))

        # Rank strikes by distance from the money so a budget squeeze truncates
        # symmetrically instead of amputating one wing.
        spot = self._spot_by_symbol.get(symbol) or 0.0
        step = (entry.strike_step if entry else None) or settings.strike_step or 50
        atm = round(spot / step) * step if spot else 0
        option_priority = Priority.ELASTIC if pinned else Priority.ACTIVE

        for t in tokens:
            vendor_symbol = getattr(t, "vendor_symbol", None)
            if not vendor_symbol:
                # The TrueData scripmaster attaches the vendor's own string. A
                # token without one cannot be subscribed: we never CONSTRUCT
                # option names, because the convention is undocumented and a
                # guessed name fails silently as "invalid symbol".
                continue
            rank = int(abs(t.strike - atm) // step) if atm and step else 0
            slots.append(Slot(
                vendor_symbol=vendor_symbol,
                token=ident.option_token(t.name, t.expiry, t.strike, t.option_type),
                priority=option_priority,
                symbol=t.name,
                moneyness_rank=rank,
            ))
        return slots

    async def _send_batched(self, method: str, symbols: list[str]) -> None:
        ws = self._ws
        if ws is None or not symbols:
            return
        batch = max(1, int(settings.truedata_subscribe_batch))
        for i in range(0, len(symbols), batch):
            chunk = symbols[i:i + batch]
            try:
                await ws.send(json.dumps({"method": method, "symbols": chunk}))
            except Exception as e:
                log.warning("td_feed.send.error", method=method, error=str(e)[:160])
                self._connected.clear()
                return

    # --------------------------------------------------------------- health
    def feed_stats(self) -> dict:
        st = self._budget.state()
        return {
            "vendor": self._vendor,
            "shadow": self._shadow,
            "connected": self.is_feed_connected,
            "supervisor_alive": self.supervisor_alive,
            "heartbeat_age_s": (
                round(self.heartbeat_age_s, 1) if self.heartbeat_age_s is not None else None
            ),
            "frames_seen": self.frames_seen,
            "seq_gaps": self.seq_gaps,
            "dropped_queue_full": self.dropped_queue_full,
            "dropped_unmapped": self.dropped_unmapped,
            "budget": {
                "capacity": st.capacity, "reserved": st.reserved,
                "elastic": st.elastic, "dropped": st.dropped, "live": st.live,
            },
            "active_symbol": self._active_symbol,
            "pinned": self._pinned_summary(),
            "spot_by_symbol": {k: round(v, 2) for k, v in self._spot_by_symbol.items()},
            "session": self._sess.snapshot(),
        }


def _bucket_seconds() -> float:
    return {"1s": 1.0, "5s": 5.0, "1min": 60.0}.get(settings.persist_bucket, 60.0)
