"""The ONE place a live feed client (and its aggregator) is built, started, rebuilt.

Before this module, feed construction was duplicated between ``main.py``'s
lifespan and ``api/auth.py``'s post-login bootstrap — and the two disagreed: the
bootstrap dropped ``index_token``/``index_segment``, silently defaulting a
SENSEX/MCX active symbol back to NIFTY-on-NSECM. Worse, NOTHING could rebuild a
feed whose supervisor task had died; the only remedy was a process restart.

The factory gives the steward a real escalation step: ``rebuild_feed_client()``
tears down whatever exists (bounded) and stands up a fresh client + supervisor
task, correctly parameterised, at any point in the process's life.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from ..auth import get_session_manager
from ..core.config import settings
from ..core.logging import get_logger
from ..core.notify import notify
from ..core.tasks import spawn_supervised
from ..market.scripmaster import resolve_option_universe
from ..market.symbols import get_registry
from ..market_data import xts_client
from ..runtime import get_runtime
from .aggregator import MinuteAggregator
from .symbol_controller import _resolve_spot_token, _spot_segment
from .ws_client import OptionFeedClient

log = get_logger("feed_factory")

REBUILD_STOP_TIMEOUT_S = 10.0


async def _initial_spot() -> float | None:
    """Fetch a starting spot for the active symbol via an XTS REST quote.

    Required because we need the spot to resolve the strike window *before* the
    websocket has produced any ticks. If the quote fails (throttled boot, no
    session yet), fall back to the last stored underlying for the symbol; the
    hardcoded constant is a last resort only. A wrong value here mis-centers
    the subscribed strike window AND is served as ``latest_spot`` until the
    live index tick arrives.
    """
    from ..services.spot_fallback import db_last_underlying

    rt = get_runtime()
    reg_entry = get_registry().get(rt.active_symbol)
    spot_token = (reg_entry.spot_token if reg_entry else None) or (
        settings.nifty_index_token if rt.active_symbol == "NIFTY" else None
    )
    # Reference-price segment: NSECM/BSECM for index/equity, MCXFO for a commodity
    # (its "spot" is the near-month future). Centralised in symbol_controller.
    segment = _spot_segment(reg_entry) if reg_entry is not None else xts_client.SEG_NSECM
    sess = get_session_manager()
    # Resolve a missing spot token (BSE index, MCX near-future, or a stock) so the
    # boot symbol centres correctly instead of falling through to the constant.
    if sess.authenticated and not spot_token and reg_entry is not None:
        try:
            spot_token = await _resolve_spot_token(reg_entry)
        except Exception as e:
            log.warning("initial_spot.resolve_error", symbol=rt.active_symbol, error=str(e))
    if sess.authenticated and spot_token:
        try:
            ltp = await xts_client.quote_ltp(sess.token, segment, spot_token)
            if ltp:
                return float(ltp)
        except Exception as e:
            log.warning("initial_spot.fallback", symbol=rt.active_symbol, error=str(e))
    else:
        log.warning("initial_spot.no_session", symbol=rt.active_symbol)
    db_spot = await db_last_underlying(rt.active_symbol)
    if db_spot:
        log.info("initial_spot.db_fallback", symbol=rt.active_symbol, spot=db_spot)
        return db_spot
    # Last-resort constant. It is a NIFTY-level number, so applying it to any other
    # symbol (SENSEX ~80k, a stock ~500, an MCX future) would centre the strike window
    # on a price that instrument never trades at and resolve a garbage/empty universe.
    # Only use it for NIFTY; otherwise report "no spot" and let the caller retry.
    if rt.active_symbol == "NIFTY":
        return 24000.0
    log.warning("initial_spot.unresolved", symbol=rt.active_symbol)
    return None


async def _resubscribe_provider() -> tuple[list, float | None]:
    """Resolve (tokens, spot) for the WS client to subscribe."""
    rt = get_runtime()
    spot = rt.latest_spot or await _initial_spot()
    if spot is None:
        # No trustworthy spot for this symbol yet — subscribing a window centred on a
        # guess would pull the wrong strikes. Return empty so the caller retries.
        log.warning("resubscribe.no_spot", symbol=rt.active_symbol)
        return [], None
    tokens, expiries = await resolve_option_universe(spot=spot, symbol=rt.active_symbol)
    if not tokens:
        # rt.latest_spot can belong to the PREVIOUS symbol after a failed
        # switch (e.g. SENSEX window centred on NIFTY's spot -> zero
        # contracts, endless resubscribe loop). Re-resolve with a spot
        # fetched for the active symbol itself before giving up.
        fresh = await _initial_spot()
        if fresh and fresh != spot:
            log.warning(
                "resubscribe.empty_universe_respot",
                symbol=rt.active_symbol, stale_spot=spot, fresh_spot=fresh,
            )
            spot = fresh
            tokens, expiries = await resolve_option_universe(spot=spot, symbol=rt.active_symbol)
    if not tokens:
        # NEVER publish an empty universe. Assigning [] here wipes a perfectly good
        # subscription list on one bad resolve, and it also empties /api/expiries and
        # _expiry_utils, so the whole app reports "no contracts" while the feed spins.
        # Keep last-known-good and let the supervisor retry.
        log.warning(
            "resubscribe.empty_universe_kept_previous",
            symbol=rt.active_symbol, spot=spot, previous_tokens=len(rt.tokens),
        )
        return [], None
    rt.tokens = tokens
    rt.expiries = expiries
    rt.latest_spot = spot
    return tokens, spot


async def resolve_index_ref() -> tuple[str, int]:
    """(index_token, index_segment) for the active symbol, with a valid fallback.

    Falling back to the NIFTY constant paired with a non-NSE segment (26000 on
    MCXFO/BSECM) is an invalid instrument that yields no spot tick — token and
    segment must stay consistent, so they are resolved TOGETHER, here, for every
    construction path (startup, post-login bootstrap, steward rebuild alike).
    """
    rt = get_runtime()
    sess = get_session_manager()
    reg_entry = get_registry().get(rt.active_symbol)
    index_token = reg_entry.spot_token if reg_entry else None
    if not index_token and reg_entry is not None and sess.authenticated:
        try:
            index_token = await _resolve_spot_token(reg_entry)
        except Exception as e:
            log.warning("feed.spot_resolve_error", symbol=rt.active_symbol, error=str(e))
    if index_token and reg_entry is not None:
        return index_token, _spot_segment(reg_entry)
    # Consistent valid fallback: NIFTY index token on its own cash segment.
    return settings.nifty_index_token, xts_client.SEG_NSECM


def _log_supervisor_death(task: asyncio.Task) -> None:
    """Attached to every ws-supervisor task: a dead supervisor must SCREAM.

    No respawn here — the steward owns recovery (it polls ``supervisor_alive``
    and rebuilds the whole client); this callback exists purely so the death is
    visible, which during outage #2 it was not: the retained task reference
    suppressed even asyncio's default unretrieved-exception warning.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is None:
        return
    log.error("ws.supervisor.died", error=str(exc), exc_info=exc)
    notify("ws_supervisor_died", f"⚠️ ws-supervisor task died: {exc!r} — steward will rebuild.")


async def _td_resubscribe_provider() -> tuple[list, float | None]:
    """TrueData counterpart of ``_resubscribe_provider``.

    Same contract and the same never-publish-an-empty-universe rule; only the
    universe SOURCE differs. Kept separate rather than branching inside the XTS
    provider so neither vendor's failure modes can leak into the other's path.
    """
    from ..market.scripmaster_td import resolve_td_option_universe

    rt = get_runtime()
    spot = rt.latest_spot or await _initial_spot()
    if spot is None:
        log.warning("td_resubscribe.no_spot", symbol=rt.active_symbol)
        return [], None
    tokens, expiries = await resolve_td_option_universe(spot=spot, symbol=rt.active_symbol)
    if not tokens:
        log.warning(
            "td_resubscribe.empty_universe_kept_previous",
            symbol=rt.active_symbol, spot=spot, previous_tokens=len(rt.tokens),
        )
        return [], None
    rt.tokens = tokens
    rt.expiries = expiries
    rt.latest_spot = spot
    return tokens, spot


async def build_feed_client():
    """THE construction seam — where FEED_VENDOR selects the whole live stack.

    Every path that stands up a feed goes through here (startup, the login
    endpoint's bootstrap, and the steward's rebuild), so one branch covers all
    three and rollback really is a config flip plus a restart.
    """
    rt = get_runtime()
    if settings.feed_vendor == "td_relay":
        # Follower: ticks come from another backend's /ws/td-relay. No vendor
        # login happens in this process at all.
        from .td_relay_feed import TdRelayFeedClient

        return TdRelayFeedClient(
            rt.tick_queue,
            _td_resubscribe_provider,
            active_symbol=rt.active_symbol,
        )
    if settings.feed_vendor == "truedata":
        from .truedata_feed import TrueDataFeedClient

        out_queue = rt.tick_queue
        if settings.td_relay_enabled:
            # Owner: mirror every tick to relay followers. The local queue is
            # still written first, so the owner's own pipeline is unchanged.
            from ..ws.td_relay import TeeQueue, get_relay_hub

            out_queue = TeeQueue(rt.tick_queue, get_relay_hub())
            log.info("ingestion.td_relay_owner_enabled")
        return TrueDataFeedClient(
            out_queue,
            _td_resubscribe_provider,
            active_symbol=rt.active_symbol,
        )
    index_token, index_segment = await resolve_index_ref()
    return OptionFeedClient(
        rt.tick_queue,
        _resubscribe_provider,
        index_token=index_token,
        active_symbol=rt.active_symbol,
        index_segment=index_segment,
    )


async def start_shadow_feed():
    """Run TrueData ALONGSIDE the live feed, into a separate store.

    The shadow pipeline is deliberately isolated at every point a live consumer
    could observe it:

      * its own queue and aggregator, writing td_shadow_snapshots;
      * published on rt.shadow_feed_client, NEVER rt.feed_client — /api/health,
        the spot refresher and the steward all read the latter;
      * a flush hook that does NOT advance rt.last_flush_at / last_ws_flush_at
        and does NOT call hub.publish_flush.

    That last point is the load-bearing one. If shadow rows advanced the WS
    freshness timestamp, a completely dead XTS socket would look healthy to the
    steward and the recovery ladder would never fire — the shadow run would
    manufacture the exact silent outage it exists to prevent.
    """
    from ..market.scripmaster_td import resolve_td_option_universe  # noqa: F401
    from .aggregator import SHADOW_TABLE
    from .truedata_feed import TrueDataFeedClient

    rt = get_runtime()
    if rt.shadow_feed_client is not None:
        return rt.shadow_feed_client

    async def _shadow_flush(bucket: datetime, rows: int, ws_rows: int) -> None:
        rt.shadow_last_flush_at = bucket
        rt.shadow_last_flush_rows = rows

    if rt.shadow_aggregator is None:
        agg = MinuteAggregator(
            rt.shadow_tick_queue, on_flush=_shadow_flush, table=SHADOW_TABLE
        )
        await agg.start()
        rt.shadow_aggregator = agg

    feed = TrueDataFeedClient(
        rt.shadow_tick_queue,
        _td_resubscribe_provider,
        active_symbol=rt.active_symbol,
        shadow=True,
    )
    await feed.start()
    rt.shadow_feed_client = feed
    log.warning("shadow.started", table=SHADOW_TABLE, symbol=rt.active_symbol)
    return feed


async def start_feed():
    """Build + start a feed client, publish it on the runtime, watch its task."""
    rt = get_runtime()
    feed = await build_feed_client()
    await feed.start()
    if getattr(feed, "_supervisor_task", None) is not None:
        feed._supervisor_task.add_done_callback(_log_supervisor_death)
    rt.feed_client = feed
    return feed


async def rebuild_feed_client(reason: str) -> OptionFeedClient:
    """Tear down whatever feed exists (bounded) and stand up a fresh one.

    The steward's heaviest in-process escalation: cures a dead supervisor task, a
    wedged socketio client object, and any poisoned in-flight state — everything
    short of a process restart. Tolerates ``rt.feed_client is None`` so it also
    works when startup never managed to build one.
    """
    rt = get_runtime()
    old = rt.feed_client
    rt.feed_client = None
    if old is not None:
        try:
            await asyncio.wait_for(old.stop(), timeout=REBUILD_STOP_TIMEOUT_S)
        except Exception as e:
            log.warning("feed_factory.old_feed_stop_failed", error=str(e))
            task = getattr(old, "_supervisor_task", None)
            if task is not None and not task.done():
                task.cancel()
    feed = await start_feed()
    log.warning("feed_factory.rebuilt", reason=reason)
    return feed


async def _spot_refresher() -> None:
    """Mirror the CURRENT feed's latest spot into runtime every second.

    Reads ``rt.feed_client`` dynamically (never captures a feed reference) so it
    keeps working across steward rebuilds without needing a restart itself.
    """
    rt = get_runtime()
    while True:
        try:
            feed = rt.feed_client
            spot = feed.latest_underlying if feed is not None else None
            if spot is not None:
                rt.latest_spot = spot
            await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            return
        except Exception as e:  # pragma: no cover
            log.warning("spot_refresher.error", error=str(e))
            await asyncio.sleep(5.0)


def _make_on_flush():
    """The single flush hook (previously duplicated in main.py and api/auth.py)."""
    from ..services import get_oi_engine
    from ..ws import get_hub

    rt = get_runtime()
    engine = get_oi_engine()
    hub = get_hub()

    # Hub publishing is DECOUPLED from the flush path: publish_flush does a
    # full chain recompute per WS subscriber (measured 20–60 ms each,
    # serialised on the engine lock), and awaiting it here made the ingest
    # flush cadence — and therefore the freshness timestamps written below —
    # hostage to how many dashboard tabs are open. One worker task at a time;
    # intermediate buckets coalesce to the newest (subscribers only ever want
    # the latest state, and at 1 s buckets skipping one is invisible).
    _pub: dict = {"task": None, "pending": None}

    async def _publish_worker() -> None:
        while True:
            pending = _pub["pending"]
            _pub["pending"] = None
            if pending is None:
                break
            b, r = pending
            try:
                await hub.publish_flush(b, r)
            except Exception as e:
                log.warning("hub.publish_failed", error=str(e)[:160])
        _pub["task"] = None

    async def on_flush(bucket: datetime, rows: int, ws_rows: int) -> None:
        rt.last_flush_at = bucket
        rt.last_flush_rows = rows
        if ws_rows > 0:
            # WS-origin freshness — the steward's health signal. Poller rows
            # advance last_flush_at only.
            rt.last_ws_flush_at = bucket
        engine.on_aggregator_flush(bucket)
        _pub["pending"] = (bucket, rows)
        task = _pub["task"]
        if task is None or task.done():
            _pub["task"] = asyncio.create_task(_publish_worker())

    return on_flush


async def ensure_live_ingestion(fresh_token_minted: bool) -> None:
    """Idempotently make sure aggregator + feed + spot refresher are running.

    Called from startup and from the login endpoint's background bootstrap.
    ``fresh_token_minted`` matters: a rotation invalidates the token the live
    socket was opened with, so the feed must reconnect — but nudging on a login
    response that merely REUSED the existing token (the floor's common case)
    would drop a healthy socket for nothing.
    """
    if settings.run_mode != "live":
        log.info("ingestion.skipped_replay")
        return
    rt = get_runtime()
    if rt.aggregator is None:
        aggregator = MinuteAggregator(rt.tick_queue, on_flush=_make_on_flush())
        await aggregator.start()
        rt.aggregator = aggregator
        log.info("ingestion.aggregator_started")
    if rt.feed_client is None:
        await start_feed()
        spawn_supervised(_spot_refresher, "spot-refresher")
        log.info("ingestion.feed_started", vendor=settings.feed_vendor)
    elif fresh_token_minted:
        log.info("ingestion.reconnecting_feed_with_fresh_token")
        rt.feed_client.nudge_reconnect()

    # The shadow feed is independent of which vendor is live: it is the
    # parallel-validation mode, so it can run beside XTS (the normal case) or
    # beside TrueData itself (the post-cutover insurance run, where XTS becomes
    # the shadow instead).
    if settings.truedata_shadow_enabled and rt.shadow_feed_client is None:
        try:
            await start_shadow_feed()
        except Exception as e:
            # A shadow failure must NEVER take the live feed with it — the whole
            # point of the shadow run is that it is unobservable to production.
            log.error("shadow.start_failed", error=str(e))
