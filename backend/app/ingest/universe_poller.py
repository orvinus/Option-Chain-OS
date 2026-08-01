"""Universe poller — REST-quote OI snapshotter for the WHOLE F&O universe.

The live WebSocket feed (``ws_client.OptionFeedClient``) can only carry ~50
instruments, so it streams the ONE symbol the user is viewing. This poller covers
*every other* F&O symbol by periodically reading their option chains over XTS REST
``POST /instruments/quotes`` (a read, not a capped subscription) and enqueuing the
results as ``Tick`` objects onto the SAME ``runtime.tick_queue``. The existing
``MinuteAggregator`` then persists them into ``option_oi_snapshots`` exactly like
live ticks — no aggregator or read-API change is needed.

Design invariants (see the approved plan / xts-broker-gotchas):
  * NEVER call ``login()``. XTS is single-session-per-appKey; a second login would
    kill the live feed's token. The poller only reads ``get_session_manager().token``
    and, on a 401, raises ``TokenStale`` to abort the sweep and waits for the feed's
    own self-heal to mint a fresh token (picked up on the next cycle).
  * Set ``underlying`` on every Tick (the aggregator copies it through; it does not
    fill it) so ``/api/option-chain`` shows spot for polled symbols.
  * Skip ``oi <= 0`` (mirror the live feed's OI-zero guard) so a throttled read
    never writes a false "OI crashed to 0" row.
  * Skip the active symbol (the live feed already covers it, fresher).
  * Route ATM math through ``resolve_option_universe`` (per-symbol strike step) —
    the poller does not inherit the global-step ``atm_drift_watch`` bug.

Cadence is tiered by liquidity via ``SymbolEntry.poll_tier`` (fast/mid/slow).
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field

import httpx

from ..auth import get_session_manager
from ..core.config import settings
from ..core.logging import get_logger
from ..market.scripmaster import InstrumentToken, get_scripmaster, resolve_option_universe
from ..market.symbols import SymbolEntry, get_registry
from ..market_data import xts_client
from ..runtime import get_runtime
from .symbol_controller import _is_commodity, _resolve_spot_token, _spot_segment
from .types import Tick

log = get_logger("universe_poller")

# Longest a previously-seen LTP may be re-stamped onto a new row when this sweep's
# touchline data is missing. Past this the price is written as NULL ("unknown")
# instead of being presented as a current quote.
_LTP_CARRY_MAX = timedelta(minutes=5)

# Per-tier delay before the first sweep, so the three tiers don't all fire on the
# same second and burst the broker at startup.
_TIER_START_STAGGER = {"fast": 0.0, "mid": 5.0, "slow": 10.0}

_BACKOFF_INITIAL_S = 1.0
_BACKOFF_MAX_S = 30.0


class TokenStale(RuntimeError):
    """Raised when the stored token is rejected (401). Abort the sweep; do NOT login."""


@dataclass
class _PollTier:
    name: str
    interval_s: float
    symbols: list[str] = field(default_factory=list)


class UniversePoller:
    def __init__(self, out_queue: "asyncio.Queue[Tick]") -> None:
        self._queue = out_queue
        self._stopping = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._client: httpx.AsyncClient | None = None
        self._sem = asyncio.Semaphore(max(1, settings.poller_max_concurrency))
        # Last-known LTP per token, to carry forward across a missed touchline chunk.
        # token -> (last good price, when it was observed). The timestamp bounds the
        # carry-forward: an unbounded carry re-stamped a price from an arbitrarily
        # earlier sweep (or a previous session) onto a brand-new minute row, which then
        # looked like a fresh quote.
        self._last_ltp: dict[str, tuple[float, datetime]] = {}
        # Cumulative volume never decreases within a session, so the last known value
        # is a safe floor when a sweep's touchline chunk is missing.
        self._last_volume: dict[str, int] = {}

    # ------------------------------------------------------------------ lifecycle

    def _build_tiers(self) -> list[_PollTier]:
        intervals = {
            "fast": settings.poller_tier_fast_interval_s,
            "mid": settings.poller_tier_mid_interval_s,
            "slow": settings.poller_tier_slow_interval_s,
        }
        buckets: dict[str, list[str]] = {"fast": [], "mid": [], "slow": []}
        for e in get_registry().all():
            if not e.fno_eligible:
                continue
            tier = e.poll_tier if e.poll_tier in buckets else "slow"
            buckets[tier].append(e.symbol)
        return [_PollTier(name=t, interval_s=intervals[t], symbols=syms)
                for t, syms in buckets.items() if syms]

    async def start(self) -> None:
        self._stopping.clear()
        self._client = httpx.AsyncClient(timeout=30.0)
        tiers = self._build_tiers()
        # Best-effort master pre-warm so many per-symbol resolves don't each trigger
        # the full FO dump. Safe to skip if not yet authenticated (first sweep warms it).
        try:
            if get_session_manager().authenticated:
                await get_scripmaster(settings.underlying_symbol)
        except Exception as e:  # pragma: no cover - warmup is best-effort
            log.debug("poller.prewarm.skip", error=str(e))
        for tier in tiers:
            self._tasks.append(asyncio.create_task(self._run_tier(tier), name=f"poller-{tier.name}"))
        log.info(
            "poller.started",
            tiers={t.name: {"n": len(t.symbols), "interval_s": t.interval_s} for t in tiers},
            window=settings.poller_strike_window,
            expiries=settings.poller_expiry_policies,
        )

    async def stop(self) -> None:
        self._stopping.set()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ scheduler

    async def _run_tier(self, tier: _PollTier) -> None:
        await asyncio.sleep(_TIER_START_STAGGER.get(tier.name, 0.0))
        while not self._stopping.is_set():
            t0 = time.monotonic()
            try:
                await self._poll_symbols(tier.symbols)
            except TokenStale:
                log.warning("poller.token_stale", tier=tier.name)  # no login; wait for feed self-heal
            except asyncio.CancelledError:
                return
            except Exception as e:  # a bad sweep never kills the tier loop
                log.warning("poller.tier_error", tier=tier.name, error=str(e))
            elapsed = time.monotonic() - t0
            try:
                await asyncio.sleep(max(1.0, tier.interval_s - elapsed))
            except asyncio.CancelledError:
                return

    # ------------------------------------------------------------------ one sweep

    async def _poll_symbols(self, symbols: list[str]) -> None:
        sess = get_session_manager()
        if not sess.authenticated:
            log.info("poller.no_session")
            return
        token = sess.token
        reg = get_registry()
        active = get_runtime().active_symbol

        entries: list[SymbolEntry] = []
        for s in symbols:
            e = reg.get(s)
            if e is None or not e.fno_eligible:
                continue
            if settings.poller_skip_active_symbol and e.symbol == active:
                continue
            entries.append(e)
        if not entries:
            return

        await self._ensure_spot_tokens(entries, token)
        # Re-read so newly-cached spot tokens are visible.
        entries = [reg.get(e.symbol) for e in entries]
        entries = [e for e in entries if e is not None]

        spot_by_symbol = await self._quote_spots(entries, token)

        ticks_enqueued = 0
        now = datetime.now(timezone.utc)
        for e in entries:
            spot = spot_by_symbol.get(e.symbol)
            if not spot or spot <= 0:
                log.debug("poller.spot_missing", symbol=e.symbol)
                continue
            try:
                tokens, _expiries = await resolve_option_universe(
                    spot=spot,
                    symbol=e.symbol,
                    window=settings.poller_strike_window,
                    policies=settings.poller_expiry_policies,
                )
            except Exception as ex:
                log.warning("poller.resolve_error", symbol=e.symbol, error=str(ex))
                continue
            if not tokens:
                log.debug("poller.symbol.master_missing", symbol=e.symbol)
                continue

            # NOTE: we key DB rows on the bare ExchangeInstrumentID (tok.token), same
            # as the live feed. Verified 2026-07-13: NSE/BSE/MCX option ids occupy
            # DISJOINT ranges (NSE <157k, MCX 510-579k, BSE >820k) → zero cross-exchange
            # collisions across ~138k instruments. If the broker ever issues overlapping
            # ids, prefix the token with the segment here AND in ws_client.
            insts: list[dict] = []
            meta: dict[int, InstrumentToken] = {}
            for t in tokens:
                try:
                    iid = int(t.token)
                except (TypeError, ValueError):
                    log.debug("poller.bad_token", symbol=e.symbol, token=t.token)
                    continue  # one bad token must not abort the whole sweep
                insts.append({"exchangeSegment": t.exchange_type, "exchangeInstrumentID": iid})
                meta[iid] = t
            ltp_q = await self._batch_quote(token, insts, xts_client.MSG_TOUCHLINE)
            oi_q = await self._batch_quote(token, insts, xts_client.MSG_OPENINTEREST)

            now = datetime.now(timezone.utc)
            for iid, tok in meta.items():
                oi = xts_client.quote_oi(oi_q.get(iid, {}))
                if oi is None or oi <= 0:
                    continue  # never persist a false 0-OI row (mirror ws_client._emit)
                ltp, vol = xts_client.quote_ltp_volume(ltp_q.get(iid, {}))
                # Carry forward the last known LTP if this sweep's touchline chunk
                # missed this instrument (a transient 1501 failure while 1510 has OI),
                # so we never stamp a false ltp=0 over a strike with valid OI.
                # BOUNDED: only reuse a recent, same-session price. Beyond that we
                # persist NULL ("price unknown") rather than dressing an old price as
                # current — and we still write the row, because dropping it would
                # silently remove that strike's real OI from every total.
                if ltp is None:
                    prev = self._last_ltp.get(tok.token)
                    if prev is not None and (now - prev[1]) <= _LTP_CARRY_MAX:
                        ltp = prev[0]
                    else:
                        ltp = None
                else:
                    self._last_ltp[tok.token] = (float(ltp), now)
                if vol is None:
                    vol = self._last_volume.get(tok.token, 0)
                else:
                    vol = max(int(vol), self._last_volume.get(tok.token, 0))
                self._last_volume[tok.token] = int(vol)
                tick = Tick(
                    ts=now,
                    token=tok.token,
                    symbol=tok.name,
                    expiry=tok.expiry,
                    strike=tok.strike,
                    option_type=tok.option_type,
                    ltp=float(ltp) if ltp is not None else None,
                    oi=int(oi),
                    volume=int(vol or 0),
                    underlying=float(spot),
                )
                try:
                    self._queue.put_nowait(tick)
                    ticks_enqueued += 1
                except asyncio.QueueFull:
                    log.warning("poller.queue_full", symbol=e.symbol)
                    break

        rt = get_runtime()
        rt.poller_last_sweep_at = now
        rt.poller_last_ticks = ticks_enqueued
        log.info("poller.sweep.done", symbols=len(entries), ticks=ticks_enqueued)

    async def _ensure_spot_tokens(self, entries: list[SymbolEntry], token: str) -> None:
        """Resolve spot/near-future tokens (paced; cached on the registry).

        Indices/stocks are resolved once (stable). Commodities are ALWAYS re-resolved
        so the near-month future token rolls at monthly expiry (cheap: in-memory
        master filter, no extra network beyond the eventual spot quote).
        """
        pace = settings.poller_pace_ms / 1000.0
        for e in entries:
            if e.spot_token and not _is_commodity(e):
                continue
            try:
                await _resolve_spot_token(e)
            except Exception as ex:
                log.debug("poller.spot_token_error", symbol=e.symbol, error=str(ex))
            await asyncio.sleep(pace)

    async def _quote_spots(self, entries: list[SymbolEntry], token: str) -> dict[str, float]:
        """Batch-quote every symbol's reference (index/equity spot or MCX near-future).

        Grouped by segment and keyed by (segment, iid) so cross-segment instrument-id
        collisions can never mis-assign a spot to the wrong symbol.
        """
        by_seg: dict[int, list[dict]] = {}
        key_to_sym: dict[tuple[int, int], str] = {}
        for e in entries:
            st = e.spot_token
            if not st:
                continue
            try:
                iid = int(st)
            except (TypeError, ValueError):
                continue
            seg = _spot_segment(e)
            by_seg.setdefault(seg, []).append(
                {"exchangeSegment": seg, "exchangeInstrumentID": iid}
            )
            key_to_sym[(seg, iid)] = e.symbol

        out: dict[str, float] = {}
        for seg, insts in by_seg.items():
            quotes = await self._batch_quote(token, insts, xts_client.MSG_TOUCHLINE)
            for iid, obj in quotes.items():
                sym = key_to_sym.get((seg, iid))
                if not sym:
                    continue
                ltp, _vol = xts_client.quote_ltp_volume(obj)
                if ltp:
                    out[sym] = float(ltp)
        return out

    # ------------------------------------------------------------------ REST quotes

    async def _batch_quote(self, token: str, instruments: list[dict], msg_code: int) -> dict[int, dict]:
        """POST /instruments/quotes in paced chunks. Returns {ExchangeInstrumentID: quote}.

        On 401 (or a 400 "Invalid Token" body) raises ``TokenStale`` (abort sweep,
        never login). On 429 it backs off and retries the SAME chunk, but with a
        per-chunk retry cap so a persistent throttle can't livelock the sweep, and
        an interruptible wait so a graceful stop() breaks promptly. Other HTTP
        errors skip the chunk. ``backoff`` is a method-local (each retry chain has
        its own; no cross-tier interference from a shared field).
        """
        assert self._client is not None
        quotes: dict[int, dict] = {}
        chunk = max(1, settings.poller_quote_chunk)
        pace = settings.poller_pace_ms / 1000.0
        base = xts_client.base_url()
        headers = xts_client.authed_headers(token)
        i = 0
        backoff = _BACKOFF_INITIAL_S
        retries_429 = 0
        while i < len(instruments):
            if self._stopping.is_set():
                break
            batch = instruments[i:i + chunk]
            body = {"instruments": batch, "xtsMessageCode": int(msg_code), "publishFormat": "JSON"}
            try:
                async with self._sem:
                    resp = await self._client.post(f"{base}/instruments/quotes", json=body, headers=headers)
            except Exception as e:
                log.warning("poller.quote_batch_error", error=str(e), code=msg_code)
                i += chunk
                retries_429 = 0
                continue
            if resp.status_code == 401:
                raise TokenStale()
            if resp.status_code == 429:
                retries_429 += 1
                if retries_429 > settings.poller_max_429_retries:
                    log.warning("poller.rate_limited.skip_chunk", code=msg_code, retries=retries_429)
                    i += chunk  # give up on this chunk; keep the sweep moving
                    retries_429 = 0
                    backoff = _BACKOFF_INITIAL_S
                    continue
                backoff = min(_BACKOFF_MAX_S, backoff * 2)
                log.warning("poller.rate_limited", backoff_s=backoff, retries=retries_429, code=msg_code)
                try:
                    # Interruptible backoff: a stop() (sets _stopping) breaks promptly.
                    await asyncio.wait_for(self._stopping.wait(), timeout=backoff)
                    break
                except asyncio.TimeoutError:
                    pass
                continue  # retry the same chunk
            if resp.status_code >= 400:
                # XTS signals a stale/foreign session token as HTTP 400 with body
                # code e-session-0007 "Invalid Token" (NOT 401). Treat it like a
                # stale token so the sweep aborts and waits for the feed self-heal
                # to refresh the in-memory token, instead of silently skipping.
                body = (resp.text or "").lower()
                if "invalid token" in body or "e-session" in body:
                    raise TokenStale()
                log.warning("poller.quote_batch_http", status=resp.status_code, code=msg_code)
                i += chunk
                retries_429 = 0
                continue
            try:
                for obj in xts_client.parse_quote_entries(resp.json()):
                    iid = obj.get("ExchangeInstrumentID")
                    if iid is not None:
                        quotes[int(iid)] = obj
            except Exception as e:
                log.warning("poller.quote_parse_error", error=str(e), code=msg_code)
            i += chunk
            retries_429 = 0
            backoff = _BACKOFF_INITIAL_S
            if i < len(instruments):
                await asyncio.sleep(pace)
        return quotes
