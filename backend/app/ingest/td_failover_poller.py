"""REST failover + long-tail sweeping for the TrueData feed.

Two jobs, both replacing capabilities the XTS ``universe_poller`` provided
through an endpoint TrueData does not have.

XTS offers ``POST /instruments/quotes`` — 25 instruments per call. TrueData has
no batch-quote equivalent at all, and the arithmetic is brutal: 230 symbols x 46
contracts is 10,580 per-contract requests per sweep, which is ~35 minutes at the
documented 5 req/s ceiling and ~2.9 HOURS at the 1 req/s floor the vendor's own
error string quotes. Per-symbol polling of the universe is not slow, it is
impossible. Hence two narrower tools:

``TdFailoverPoller``
    Active symbol ONLY, and only while the websocket is unhealthy. ~46 requests
    per sweep at 4 rps is about 12 seconds, comfortably inside the 15-second
    failover interval. This is what stops the dashboard going dark during a
    reconnect; it is not universe coverage.

``TdSegmentSweeper``
    ``getAllBars`` — one request per minute returns EVERY symbol's 1-min bar for
    a whole segment, independent of universe size. This is the only viable path
    for the 212 long-tail stocks, and it is a paid add-on. Without it the tail
    simply has no live data under TrueData (it has almost none today either: the
    poller defaults to failover mode, so 229 of 230 symbols are already dark
    unless the socket is down).

Both emit ``origin="poller"`` — the exact string the aggregator's
ws-beats-poller precedence rule and the steward's WS-origin freshness rule
depend on. Changing it would make REST rows able to mask a dead socket.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from ..core.config import settings
from ..core.logging import get_logger
from ..core.time_utils import IST, ist_naive_to_utc
from ..market import td_identity as ident
from ..market_data.truedata_rest import TrueDataRest
from ..runtime import get_runtime
from .types import Tick

log = get_logger("td_poller")

# Carry a stale premium forward at most this long, mirroring the XTS poller.
# Beyond it the price is reported as NULL rather than as a confident-looking lie.
# How long a missing close may reuse the previous sweep's LTP before the row
# goes out with ltp=None ("NULL, never a confident-looking lie"). Was 300s —
# a 5-minute-old premium under a fresh-looking row is long enough to mislead
# an exit ladder; one minute is the honest ceiling for 1-min-bar data.
LTP_CARRY_MAX_S = 60.0


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


class TdFailoverPoller:
    """Keeps the ACTIVE symbol updating while the websocket is unhealthy."""

    def __init__(self, out_queue: "asyncio.Queue[Tick]") -> None:
        self._queue = out_queue
        self._stopping = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._td: TrueDataRest | None = None
        self._last_ltp: dict[str, tuple[float, datetime]] = {}
        self._last_volume: dict[str, int] = {}

    async def start(self) -> None:
        self._stopping.clear()
        self._td = TrueDataRest()
        self._task = asyncio.create_task(self._run(), name="td-poller-failover")
        log.info("td_poller.started", interval_s=settings.poller_failover_interval_s)

    async def stop(self) -> None:
        self._stopping.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._td is not None:
            await self._td.aclose()

    def _ws_delivering(self) -> bool:
        """Fail SAFE: when the steward does not exist yet, assume the feed works.

        A poller that runs during normal operation would double the vendor's REST
        load for no benefit, and at a 1 req/s floor that competes directly with
        the gap-fill it exists to serve.
        """
        steward = get_runtime().steward
        if steward is None:
            return True
        return bool(getattr(steward, "ws_feed_stable", True))

    async def _run(self) -> None:
        interval = max(5.0, settings.poller_failover_interval_s)
        while not self._stopping.is_set():
            try:
                if not self._ws_delivering():
                    await self._sweep()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("td_poller.sweep_error", error=str(e)[:180])
            await asyncio.sleep(interval)

    async def _sweep(self) -> None:
        from ..market.scripmaster_td import resolve_td_option_universe

        rt = get_runtime()
        td = self._td
        if td is None:
            return
        symbol = rt.active_symbol
        spot = rt.latest_spot
        if not spot:
            return

        tokens, _expiries = await resolve_td_option_universe(
            spot=spot, symbol=symbol,
            window=settings.poller_strike_window,
            policies=settings.poller_expiry_policies,
        )
        if not tokens:
            return

        now = datetime.now(timezone.utc)
        enqueued = 0
        for t in tokens:
            vendor_symbol = getattr(t, "vendor_symbol", None)
            if not vendor_symbol:
                continue
            try:
                bars = await td.get_last_n_bars(vendor_symbol, nbars=1, interval="1min")
            except Exception:
                continue
            if not bars:
                continue
            row = bars[-1]
            oi = _i(row.get("oi"))
            # Same guard as the socket path: never persist a zero-OI row.
            if oi <= 0:
                continue

            # Stamp the row with the BAR'S OWN time, not the sweep time: a
            # 1-min bar can be up to a couple of minutes old, and `ts=now`
            # dressed stale data as current for every freshness-sensitive
            # reader. Unparseable/absent timestamp falls back to `now`.
            row_ts = now
            raw_ts = str(row.get("timestamp") or "").strip()
            if raw_ts:
                try:
                    row_ts = ist_naive_to_utc(datetime.fromisoformat(raw_ts))
                except ValueError:
                    pass

            key = t.token
            ltp = _f(row.get("close"))
            if ltp and ltp > 0:
                self._last_ltp[key] = (ltp, now)
            else:
                prev = self._last_ltp.get(key)
                ltp = (
                    prev[0]
                    if prev and (now - prev[1]).total_seconds() <= LTP_CARRY_MAX_S
                    else None
                )
            vol = max(_i(row.get("volume")), self._last_volume.get(key, 0))
            self._last_volume[key] = vol

            try:
                self._queue.put_nowait(Tick(
                    ts=row_ts, token=key, symbol=t.name, expiry=t.expiry,
                    strike=t.strike, option_type=t.option_type,
                    ltp=ltp, oi=oi, volume=vol, underlying=spot,
                    origin="poller", vendor="truedata",
                ))
                enqueued += 1
            except asyncio.QueueFull:
                break

        rt.poller_last_sweep_at = datetime.now(IST)
        rt.poller_last_ticks = enqueued
        log.info("td_poller.sweep", symbol=symbol, contracts=len(tokens), ticks=enqueued)


class TdSegmentSweeper:
    """One ``getAllBars`` request per minute covers an ENTIRE segment.

    Requires the getAllBars add-on. Coverage for the long tail is 1-minute
    granularity rather than 1-second, which is an honest trade: those symbols
    currently receive no live data at all.
    """

    def __init__(self, out_queue: "asyncio.Queue[Tick]", segments: tuple[str, ...] = ("fo",)) -> None:
        self._queue = out_queue
        self._segments = segments
        self._stopping = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._td: TrueDataRest | None = None
        self._last_volume: dict[str, int] = {}

    async def start(self) -> None:
        self._stopping.clear()
        self._td = TrueDataRest()
        self._task = asyncio.create_task(self._run(), name="td-segment-sweeper")
        log.info("td_sweeper.started", segments=self._segments)

    async def stop(self) -> None:
        self._stopping.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._td is not None:
            await self._td.aclose()

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                # One minute BEHIND the wall clock: the bar for minute T is not
                # complete until T+1, and asking for an in-flight minute returns
                # either nothing or a partial bar.
                minute = (datetime.now(IST) - timedelta(minutes=1)).replace(second=0, microsecond=0)
                for seg in self._segments:
                    await self._sweep(seg, minute)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("td_sweeper.error", error=str(e)[:180])
            await asyncio.sleep(60.0)

    async def _sweep(self, segment: str, minute: datetime) -> None:
        td = self._td
        if td is None:
            return
        rows = await td.get_all_bars(segment, minute)
        if not rows:
            return

        rt = get_runtime()
        known = {e.symbol.upper() for e in _registry_symbols()}
        ts = ist_naive_to_utc(minute.replace(tzinfo=None))
        enqueued = 0
        unparseable = 0

        for row in rows:
            vendor_symbol = (row.get("symbol") or row.get("Symbol") or "").strip()
            if not vendor_symbol:
                continue
            oi = _i(row.get("oi"))
            if oi <= 0:
                continue
            underlying = _underlying_of(vendor_symbol, known)
            if underlying is None:
                continue          # not in our registry; not our problem
            parsed = _parse_any_expiry_contract(vendor_symbol, underlying)
            if parsed is None:
                unparseable += 1
                continue
            expiry, strike, opt = parsed

            token = ident.option_token(underlying, expiry, strike, opt)
            vol = max(_i(row.get("volume")), self._last_volume.get(token, 0))
            self._last_volume[token] = vol
            try:
                self._queue.put_nowait(Tick(
                    ts=ts, token=token, symbol=underlying, expiry=expiry,
                    strike=strike, option_type=opt,
                    ltp=_f(row.get("close")), oi=oi, volume=vol,
                    underlying=None, origin="poller", vendor="truedata",
                ))
                enqueued += 1
            except asyncio.QueueFull:
                break

        rt.poller_last_sweep_at = datetime.now(IST)
        rt.poller_last_ticks = enqueued
        log.info("td_sweeper.sweep", segment=segment, minute=str(minute),
                 rows=len(rows), ticks=enqueued, unparseable=unparseable)


def _registry_symbols():
    from ..market.symbols import get_registry

    return get_registry().all()


def _underlying_of(vendor_symbol: str, known: set[str]) -> str | None:
    """Longest registry symbol that prefixes this contract string.

    Longest-match matters: "NIFTY" prefixes "NIFTYNXT50", so a shortest-match
    would file every NIFTYNXT50 contract under NIFTY and corrupt both chains.
    """
    up = vendor_symbol.upper()
    best: str | None = None
    for sym in known:
        if up.startswith(sym) and (best is None or len(sym) > len(best)):
            best = sym
    return best


def _parse_any_expiry_contract(vendor_symbol: str, underlying: str):
    """(expiry, strike, type) from a contract string with an unknown expiry.

    The per-expiry path knows which expiry it asked for; a segment sweep does
    not, so the yymmdd embedded in the tail is read directly. Returns None for
    futures and anything unparseable — never a guess.
    """
    from ..market_data.truedata_rest import _OPT_TAIL_RE

    m = _OPT_TAIL_RE.search(vendor_symbol.strip())
    if not m:
        return None
    try:
        expiry = datetime.strptime(m.group(1), "%y%m%d").date()
    except ValueError:
        return None
    return expiry, int(round(float(m.group(2)))), m.group(3).upper()
