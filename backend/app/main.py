"""FastAPI application factory + lifespan.

Boot sequence (lifespan):
    1.  Configure logging.
    2.  Restore the XTS market-data session from ``auth_sessions``, else log in with
        appKey/secretKey. Both happen automatically whenever ``RUN_MODE=live`` and
        ``AUTH_MODE=totp`` — no dashboard click and no opt-in flag is involved.
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
from .core.tasks import spawn_supervised
from .ingest.atm_drift_watch import run_atm_drift_watch
from .ingest.feed_factory import ensure_live_ingestion
from .ingest.feed_watchdog import run_feed_watchdog
from .ingest.market_session_watch import run_nse_session_open_watch
from .ingest.universe_poller import UniversePoller
from .runtime import get_runtime
from .ws import ws_router

log = get_logger("main")

# Startup `sess.login()` is a network call to the broker; without a cap, a stalled
# response keeps lifespan from reaching `yield` and nothing listens on :8000.
STARTUP_LOGIN_TIMEOUT_S = 30.0


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    configure_logging("INFO")
    log.info("app.startup", run_mode=settings.run_mode)
    rt = get_runtime()
    sess = get_session_manager()

    # 1) Restore the last persisted XTS session when possible.
    # 2) Else log in fresh from the appKey/secretKey in .env.
    if settings.run_mode == "live":
        restored = False
        try:
            restored = await asyncio.wait_for(
                sess.try_restore_session_from_db(),
                timeout=STARTUP_LOGIN_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            log.warning(
                "app.startup.restore_session_timeout",
                seconds=STARTUP_LOGIN_TIMEOUT_S,
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
                    timeout=STARTUP_LOGIN_TIMEOUT_S,
                )
                await sess.start_refresh_loop()
                log.info("app.startup.auto_login_ok")
            except asyncio.TimeoutError:
                log.warning(
                    "app.startup.login_timeout",
                    seconds=STARTUP_LOGIN_TIMEOUT_S,
                    hint="XTS market-data login did not respond; authenticate via the dashboard.",
                )
            except Exception as e:
                # Wrong appKey/secretKey / network — backend stays up for UI login.
                log.warning("app.startup.login_deferred", reason=str(e))

    session_watch: asyncio.Task[None] | None = None
    atm_watch: asyncio.Task[None] | None = None
    feed_watch: asyncio.Task[None] | None = None
    poller: UniversePoller | None = None

    try:
        if settings.run_mode == "live":
            # Aggregator + feed client + spot refresher, via the ONE factory path
            # (shared with the login endpoint's bootstrap and the steward rebuild).
            await ensure_live_ingestion(fresh_token_minted=False)
            feed = rt.feed_client

            session_watch = asyncio.create_task(
                run_nse_session_open_watch(feed),
                name="nse-session-watch",
            )
            atm_watch = asyncio.create_task(
                run_atm_drift_watch(),
                name="atm-drift-watch",
            )
            # Outcome-based recovery: escalates until rows land again. Every other
            # recovery path needs a specific event to fire; this one only asks
            # whether data is still arriving.
            feed_watch = asyncio.create_task(
                run_feed_watchdog(feed),
                name="feed-watchdog",
            )

            # All-symbol OI snapshotter (opt-in). Reuses the same tick_queue →
            # aggregator → option_oi_snapshots path as the live feed.
            if settings.poller_enabled:
                poller = UniversePoller(rt.tick_queue)
                await poller.start()
                rt.universe_poller = poller

            # Persist ATM IV for the active symbol so IVR/IVP accumulate over days.
            spawn_supervised(_iv_history_loop, "iv-history-snapshot")
            # Persist per-strike greeks so replay/exports can show live-computed greeks.
            spawn_supervised(_greeks_history_loop, "greeks-history-snapshot")

        yield

    finally:
        log.info("app.shutdown")
        for task in (session_watch, atm_watch, feed_watch):
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
        if rt.feed_client is not None:
            await rt.feed_client.stop()
        if rt.aggregator is not None:
            await rt.aggregator.stop()
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
