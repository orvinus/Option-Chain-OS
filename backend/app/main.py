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
from starlette.staticfiles import StaticFiles

from .api import api_router
from .auth import get_session_manager
from .core.config import settings
from .core.logging import configure_logging, get_logger
from .ingest.aggregator import MinuteAggregator
from .ingest.atm_drift_watch import run_atm_drift_watch
from .ingest.market_session_watch import run_nse_session_open_watch
from .ingest.ws_client import OptionFeedClient
from .market.scripmaster import resolve_option_universe
from .market.symbols import get_registry
from .market_data import xts_client
from .runtime import get_runtime
from .services import get_oi_engine
from .ws import get_hub, ws_router

log = get_logger("main")

# Startup `sess.login()` calls Angel over the network; without a cap, a stalled
# SmartAPI keeps lifespan from reaching `yield` and nothing listens on :8000.
STARTUP_SMARTAPI_LOGIN_TIMEOUT_S = 30.0


async def _initial_spot() -> float:
    """Fetch a starting spot for the active symbol via SmartAPI REST quote.

    Required because we need the spot to resolve the strike window *before* the
    websocket has produced any ticks.
    """
    rt = get_runtime()
    reg_entry = get_registry().get(rt.active_symbol)
    spot_token = (reg_entry.spot_token if reg_entry else None) or settings.nifty_index_token
    sess = get_session_manager()
    if not sess.authenticated:
        # No dashboard / env login yet — use fallback so WS resubscribe can still run.
        log.warning("initial_spot.no_session", symbol=rt.active_symbol)
        return 24000.0
    try:
        ltp = await xts_client.quote_ltp(sess.token, xts_client.SEG_NSECM, spot_token)
        if ltp:
            return float(ltp)
    except Exception as e:
        log.warning("initial_spot.fallback", symbol=rt.active_symbol, error=str(e))
    # Fallback: a sane value so the universe resolver can still iterate.
    return 24000.0


async def _resubscribe_provider() -> tuple[list, float]:
    """Resolve (tokens, spot) for the WS client to subscribe."""
    rt = get_runtime()
    spot = rt.latest_spot or await _initial_spot()
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
        elif settings.xts_login_at_startup and (settings.xts_md_secret_key or "").strip():
            try:
                await asyncio.wait_for(
                    sess.login(),
                    timeout=STARTUP_SMARTAPI_LOGIN_TIMEOUT_S,
                )
                await sess.start_refresh_loop()
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

            reg_entry = get_registry().get(rt.active_symbol)
            index_token = (reg_entry.spot_token if reg_entry else None) or settings.nifty_index_token
            index_segment = (
                xts_client.SEG_BSECM
                if reg_entry and (reg_entry.exchange or "").upper() == "BSE"
                else xts_client.SEG_NSECM
            )
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
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="frontend")
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
