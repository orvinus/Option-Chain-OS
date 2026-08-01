"""FastAPI application factory + lifespan.

Boot sequence (lifespan):
    1.  Configure logging.
    2.  Restore the XTS market-data session from ``auth_sessions``, or log in with
        appKey/secretKey when ``XTS_LOGIN_AT_STARTUP`` is set.
    3.  Bootstrap the option universe (uses /quote LTP for an initial spot).
    4.  Start the XTS Socket.IO ingestion client and IST session-open watch.
    5.  Start the 1-minute aggregator (which feeds the WS hub on each flush).
    6.  Yield to the request-handling phase.
    7.  On shutdown, stop everything in reverse order.
"""
from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from .api import api_router
from .auth import get_session_manager
from .core.config import settings
from .core.db import AsyncSessionLocal
from .core.logging import configure_logging, get_logger
from .ingest.aggregator import MinuteAggregator
from .ingest.atm_drift_watch import run_atm_drift_watch
from .ingest.market_session_watch import run_nse_session_open_watch
from .ingest.symbol_controller import _resolve_spot_token, _spot_segment
from .ingest.universe_poller import UniversePoller
from .ingest.ws_client import OptionFeedClient
from .market.scripmaster import resolve_option_universe
from .market.symbols import get_registry
from .market_data import xts_client
from .runtime import get_runtime
from .services import get_oi_engine
from .services.spot_fallback import db_last_underlying
from .ws import get_hub, ws_router

log = get_logger("main")

# Startup `sess.login()` calls Angel over the network; without a cap, a stalled
# SmartAPI keeps lifespan from reaching `yield` and nothing listens on :8000.
STARTUP_SMARTAPI_LOGIN_TIMEOUT_S = 30.0


async def _initial_spot() -> float | None:
    """Fetch a starting spot for the active symbol via an XTS REST quote.

    Required because we need the spot to resolve the strike window *before* the
    websocket has produced any ticks. If the quote fails (throttled boot, no
    session yet), fall back to the last stored underlying for the symbol; the
    hardcoded constant is a last resort only. A wrong value here mis-centers
    the subscribed strike window AND is served as ``latest_spot`` until the
    live index tick arrives.
    """
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
    rt.tokens = tokens
    rt.expiries = expiries
    rt.latest_spot = spot
    return tokens, spot


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    configure_logging("INFO")
    log.info("app.startup", run_mode=settings.run_mode)
    rt = get_runtime()
    sess = get_session_manager()

    # 1) Restore last persisted Angel session (refresh JWT/feed) when possible.
    # 2) Else optional MPIN+TOTP login when ANGEL_LOGIN_AT_STARTUP=true.
    if settings.run_mode == "live" and settings.auth_mode == "totp":
        restored = False
        try:
            restored = await asyncio.wait_for(
                sess.try_restore_session_from_db(),
                timeout=STARTUP_SMARTAPI_LOGIN_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            log.warning(
                "app.startup.restore_session_timeout",
                seconds=STARTUP_SMARTAPI_LOGIN_TIMEOUT_S,
            )
        except Exception as e:
            log.warning("app.startup.restore_session_error", error=str(e))
        if restored:
            await sess.start_refresh_loop()
            log.info("app.startup.session_restored_from_db")
            # A TTL-valid restored token can still be dead (XTS daily expiry /
            # single-session invalidation). If so, the feed's self-heal will
            # auto re-login on the first 'Invalid Token' — no manual click needed.
        elif (settings.xts_md_secret_key or "").strip() and (settings.xts_md_app_key or "").strip():
            # No usable session restored — auto-login at startup so the feed comes
            # up live without a human clicking the dashboard button. force=True
            # guarantees a fresh token.
            try:
                await asyncio.wait_for(
                    sess.login(force=True),
                    timeout=STARTUP_SMARTAPI_LOGIN_TIMEOUT_S,
                )
                await sess.start_refresh_loop()
                log.info("app.startup.auto_login_ok")
            except asyncio.TimeoutError:
                log.warning(
                    "app.startup.login_timeout",
                    seconds=STARTUP_SMARTAPI_LOGIN_TIMEOUT_S,
                    hint="XTS market-data login did not respond; authenticate via the dashboard.",
                )
            except Exception as e:
                # Wrong appKey/secretKey / network — backend stays up for UI login.
                log.warning("app.startup.login_deferred", reason=str(e))

    feed: OptionFeedClient | None = None
    aggregator: MinuteAggregator | None = None
    session_watch: asyncio.Task[None] | None = None
    atm_watch: asyncio.Task[None] | None = None
    poller: UniversePoller | None = None

    try:
        if settings.run_mode == "live":
            engine = get_oi_engine()
            hub = get_hub()

            async def on_flush(bucket: datetime, rows: int) -> None:
                rt.last_flush_at = bucket
                rt.last_flush_rows = rows
                engine.on_aggregator_flush(bucket)
                await hub.publish_flush(bucket, rows)

            aggregator = MinuteAggregator(rt.tick_queue, on_flush=on_flush)
            await aggregator.start()
            rt.aggregator = aggregator

            # Resolve the reference-price token BEFORE building the feed so token and
            # segment stay consistent. Falling back to the NIFTY constant paired with
            # a non-NSE segment (e.g. a commodity/BSE-index boot symbol → 26000 on
            # MCXFO/BSECM) is an invalid instrument and yields no spot tick.
            reg_entry = get_registry().get(rt.active_symbol)
            index_token = reg_entry.spot_token if reg_entry else None
            if not index_token and reg_entry is not None and sess.authenticated:
                try:
                    index_token = await _resolve_spot_token(reg_entry)
                except Exception as e:
                    log.warning("feed.spot_resolve_error", symbol=rt.active_symbol, error=str(e))
            if index_token and reg_entry is not None:
                index_segment = _spot_segment(reg_entry)
            else:
                # Consistent valid fallback: NIFTY index token on its own cash segment.
                index_token = settings.nifty_index_token
                index_segment = xts_client.SEG_NSECM
            feed = OptionFeedClient(
                rt.tick_queue,
                _resubscribe_provider,
                index_token=index_token,
                active_symbol=rt.active_symbol,
                index_segment=index_segment,
            )
            await feed.start()
            rt.feed_client = feed

            # Background task: refresh latest_spot from feed every second
            asyncio.create_task(_spot_refresher(feed), name="spot-refresher")
            session_watch = asyncio.create_task(
                run_nse_session_open_watch(feed),
                name="nse-session-watch",
            )
            atm_watch = asyncio.create_task(
                run_atm_drift_watch(),
                name="atm-drift-watch",
            )

            # All-symbol OI snapshotter (opt-in). Reuses the same tick_queue →
            # aggregator → option_oi_snapshots path as the live feed.
            if settings.poller_enabled:
                poller = UniversePoller(rt.tick_queue)
                await poller.start()
                rt.universe_poller = poller

            # Persist ATM IV for the active symbol so IVR/IVP accumulate over days.
            asyncio.create_task(_iv_history_loop(), name="iv-history-snapshot")
            # Persist per-strike greeks so replay/exports can show live-computed greeks.
            asyncio.create_task(_greeks_history_loop(), name="greeks-history-snapshot")

        yield

    finally:
        log.info("app.shutdown")
        for task in (session_watch, atm_watch):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        # Stop producers (feed + poller) before the aggregator so no producer
        # outlives the consumer draining the queue.
        if poller is not None:
            await poller.stop()
        if feed is not None:
            await feed.stop()
        if aggregator is not None:
            await aggregator.stop()
        if settings.run_mode == "live":
            await sess.stop()


def _frontend_dist_dir() -> Path | None:
    """Vite ``dist`` next to the exe (bundled) or ``frontend/dist`` at repo root (dev)."""
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "frontend_dist")
    else:
        repo = Path(__file__).resolve().parents[2]
        candidates.append(repo / "frontend" / "dist")
    for p in candidates:
        if p.is_dir() and (p / "index.html").is_file():
            return p
    return None


class _SPAStaticFiles(StaticFiles):
    """StaticFiles that falls back to ``index.html`` on 404 so client-side routes
    (e.g. ``/hidden``) resolve on hard refresh when FastAPI serves the built SPA
    directly (frozen-exe / local ``frontend/dist``). Inert under nginx/Vite, which
    already do history fallback. The ``/api`` and ``/ws`` routers are registered
    before the greedy ``/`` mount, so they always match first — this fallback only
    ever sees non-API paths, and turns their 404s into the SPA shell.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 404:
            return await super().get_response("index.html", scope)
        return response


async def _spot_refresher(feed: OptionFeedClient) -> None:
    """Mirror the feed's latest spot into runtime so REST can read it without a queue."""
    rt = get_runtime()
    while True:
        try:
            spot = feed.latest_underlying
            if spot is not None:
                rt.latest_spot = spot
            await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            return
        except Exception as e:  # pragma: no cover
            log.warning("spot_refresher.error", error=str(e))
            await asyncio.sleep(5.0)


async def _iv_history_loop() -> None:
    """Periodically snapshot ATM IV for the active symbol into ``iv_daily``."""
    from .services.iv_history import snapshot_atm_iv_for_symbol

    # Delay initial snapshot so the feed / DB have a chance to warm up.
    await asyncio.sleep(60.0)
    while True:
        try:
            rt = get_runtime()
            await snapshot_atm_iv_for_symbol(rt.active_symbol)
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.warning("iv_history_loop.error", error=str(e))
        try:
            await asyncio.sleep(max(60.0, float(settings.iv_history_snapshot_interval_s)))
        except asyncio.CancelledError:
            return


async def _greeks_history_loop() -> None:
    """Periodically persist per-strike greeks/IV for the active symbol.

    Feeds ``greeks_snapshots`` so replay/exports can show the greeks that were
    actually computed live (not recomputed on read). Runs at a modest cadence —
    greeks move slower than OI and this is a background enrichment, not the hot
    path.
    """
    from .services.greeks_history import snapshot_greeks_for_symbol

    await asyncio.sleep(75.0)  # warm up after the feed/DB (offset from IV loop)
    while True:
        try:
            rt = get_runtime()
            await snapshot_greeks_for_symbol(rt.active_symbol)
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.warning("greeks_history_loop.error", error=str(e))
        try:
            await asyncio.sleep(60.0)
        except asyncio.CancelledError:
            return


def create_app() -> FastAPI:
    app = FastAPI(
        title="NIFTY OI Analytics",
        version="1.0.0",
        description="Real-time NIFTY Open Interest Change analytics platform.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router)
    app.include_router(ws_router)

    dist = _frontend_dist_dir()
    if dist is not None:
        app.mount("/", _SPAStaticFiles(directory=str(dist), html=True), name="frontend")
    else:

        @app.get("/")
        async def root() -> dict:
            return {
                "name": "NIFTY OI Analytics",
                "version": "1.0.0",
                "now_utc": datetime.now(timezone.utc).isoformat(),
                "docs": "/docs",
                "health": "/api/health",
            }

    return app


app = create_app()
