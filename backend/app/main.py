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
from .core.tasks import cancel_supervised, spawn_supervised
from .ingest.atm_drift_watch import run_atm_drift_watch
from .ingest.feed_factory import ensure_live_ingestion
from .ingest.market_session_watch import run_nse_session_open_watch
from .ingest.session_steward import SessionSteward, set_steward
from .ingest.universe_poller import UniversePoller
from .runtime import get_runtime
from .ws import algo_ws_router, ws_router

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

    # ---- TrueData boot path -------------------------------------------------
    # TrueData needs no session restore and no login here: credentials ride in
    # the websocket URL, so the feed authenticates itself on connect. What it
    # DOES need is the opposite of a login — a defensive logout.
    #
    # A container restart is, from the vendor's point of view, a dirty
    # disconnect: the server still believes the previous process is connected
    # and rejects us with "User Already Connected" for ~60s. Since restarts are
    # routine here (deploys, the sentinel, the steward's own backstop), the boot
    # path MUST assume the last exit was dirty and clear the session before the
    # feed's first connect attempt — otherwise every restart begins with a
    # guaranteed lockout.
    if settings.run_mode == "live" and settings.feed_vendor == "truedata":
        from .auth.td_session import get_td_session
        from .core import proxy_health

        await proxy_health.probe(force=True)
        if not proxy_health.is_up():
            log.error("app.startup.proxy_down", detail=proxy_health.last_error(),
                      hint="TrueData is only reachable through the WARP SOCKS proxy; "
                           "check warp-svc and warp-socks-bridge on the host.")
        td_sess = get_td_session()
        try:
            td_sess.require_credentials()
            await asyncio.wait_for(td_sess.logout_request("boot", force=True), timeout=25.0)
            log.info("app.startup.td_boot_logout_done",
                     cooldown_s=round(td_sess.cooldown_remaining_s, 1))
        except Exception as e:
            log.warning("app.startup.td_boot_logout_failed", error=str(e))

    # 1) Restore the last persisted XTS session when possible.
    # 2) Else log in fresh from the appKey/secretKey in .env.
    # Skipped entirely under FEED_VENDOR=truedata: there is no XTS session to
    # hold, and logging in would pointlessly consume the broker's single seat.
    if settings.run_mode == "live" and settings.feed_vendor == "xts":
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
            # Token renewal is the steward's job now (daily pre-open + TTL
            # backstop) — there is no interval refresh loop to start.
            log.info("app.startup.session_restored_from_db")
        elif (settings.xts_md_secret_key or "").strip() and (settings.xts_md_app_key or "").strip():
            # No usable session restored (missing, expired, or broker-rejected on
            # the validation probe) — log in fresh so the feed comes up unattended.
            try:
                await asyncio.wait_for(
                    sess.login(force=True, actor="startup"),
                    timeout=STARTUP_LOGIN_TIMEOUT_S,
                )
                log.info("app.startup.auto_login_ok")
            except asyncio.TimeoutError:
                log.warning(
                    "app.startup.login_timeout",
                    seconds=STARTUP_LOGIN_TIMEOUT_S,
                    hint="XTS market-data login did not respond; the steward keeps retrying.",
                )
            except Exception as e:
                # Wrong appKey/secretKey / network / circuit parked at boot —
                # backend stays up; the steward's ladder takes it from here.
                log.warning("app.startup.login_deferred", reason=str(e))
                from .core.notify import notify

                notify("startup_login_failed", f"⚠️ Startup broker login failed: {e}")

    poller: UniversePoller | None = None

    try:
        if settings.run_mode == "live":
            # Aggregator + feed client + spot refresher, via the ONE factory path
            # (shared with the login endpoint's bootstrap and the steward rebuild).
            await ensure_live_ingestion(fresh_token_minted=False)

            spawn_supervised(run_nse_session_open_watch, "nse-session-watch")
            spawn_supervised(run_atm_drift_watch, "atm-drift-watch")

            # The single recovery authority. Which one depends on the vendor,
            # because the two have OPPOSITE premises: the XTS steward exists to
            # avoid logging in too often (a login kills the live session), while
            # the TrueData steward exists to avoid logging OUT too often (a
            # logout costs a ~60s lockout). Running the XTS ladder against
            # TrueData would be a lockout generator, so this is a hard branch,
            # never a shared class with flags.
            if settings.feed_vendor == "truedata":
                from .core.proxy_health import run_proxy_watch
                from .ingest.truedata_steward import get_td_steward

                td_steward = get_td_steward()
                rt.steward = td_steward
                spawn_supervised(td_steward.run, "session-steward")
                spawn_supervised(run_proxy_watch, "proxy-watch")
            elif settings.feed_vendor == "td_relay":
                # Follower: there is no vendor session to rotate or log out, so
                # NO steward. The relay client reconnects on its own; running
                # the TrueData ladder here would try to log the OWNER out.
                rt.steward = None
                log.info("app.startup.td_relay_follower", relay=settings.td_relay_url.split("?")[0])
            else:
                steward = SessionSteward()
                set_steward(steward)
                rt.steward = steward
                spawn_supervised(steward.run, "session-steward")

            # REST snapshotter: "failover" covers the active symbol while the WS
            # feed is down; "full" additionally polls the whole F&O universe.
            # The transports are vendor-specific (XTS batch-quotes vs TrueData
            # per-contract getlastnbars / whole-segment getAllBars), so the
            # implementation is chosen here while the MODE semantics stay shared.
            if settings.effective_poller_mode != "off":
                # The REST failover transport is the vendor's, and a relay
                # follower still holds REST credentials — so it fails over the
                # same way the owner does.
                if settings.feed_vendor in ("truedata", "td_relay"):
                    from .ingest.td_failover_poller import TdFailoverPoller, TdSegmentSweeper

                    if settings.effective_poller_mode == "segment_sweep":
                        poller = TdSegmentSweeper(rt.tick_queue)
                    else:
                        poller = TdFailoverPoller(rt.tick_queue)
                else:
                    poller = UniversePoller(rt.tick_queue)
                await poller.start()
                rt.universe_poller = poller

            # Persist ATM IV for the active symbol so IVR/IVP accumulate over days.
            spawn_supervised(_iv_history_loop, "iv-history-snapshot")
            # Persist per-strike greeks so replay/exports can show live-computed greeks.
            spawn_supervised(_greeks_history_loop, "greeks-history-snapshot")

            # The Algo Config trading orchestrator: one decision pass per
            # closed minute during the session (clock-driven — it reads the
            # already-persisted buckets, deliberately NOT the flush hook, so
            # the hardened ingestion path stays untouched). Routes to the
            # paper simulator until the live broker milestone lands.
            from .algo.orchestrator import run_orchestrator_loop

            spawn_supervised(run_orchestrator_loop, "algo-orchestrator")

        # The Algo Config live stream — deliberately OUTSIDE the live-mode
        # branch. Under RUN_MODE=replay there is no feed and no orchestrator,
        # but the page must still render the last stored session and SAY that
        # it is replaying rather than look broken. The loop costs nothing with
        # zero subscribers, so running it unconditionally is free.
        from .algo.live_stream import run_algo_stream_loop

        spawn_supervised(run_algo_stream_loop, "algo-stream")

        # Keeps oi_day_stats / live_days current. Independent of the nightly
        # shell script on purpose: that script's refresh line was uncommitted
        # for weeks, so anything deployed from git never refreshed at all and
        # oi_snapshots_unified silently served the wrong arm. Also runs in
        # replay — a stale index is stale regardless of the feed.
        from .services.data_health import run_data_health_loop

        spawn_supervised(run_data_health_loop, "data-health")

        # Heals multi-day outages: if this backend was down for N trading days,
        # pull them from the TrueData REST archive (same puller as the nightly
        # cron) and refresh the day index. Live mode only — a replay box has
        # no business writing history it did not observe.
        if settings.run_mode == "live":
            from .ingest.gapfill import run_gapfill_loop

            spawn_supervised(run_gapfill_loop, "gapfill", respawn=False)

        yield

    finally:
        log.info("app.shutdown")
        for name in (
            "session-steward", "nse-session-watch", "atm-drift-watch",
            "iv-history-snapshot", "greeks-history-snapshot", "spot-refresher",
            "proxy-watch", "algo-orchestrator", "algo-stream", "data-health",
            # was missing: an in-flight vendor pull subprocess outlived shutdown
            "gapfill",
        ):
            cancel_supervised(name)
        # Stop producers (feed + poller) before the aggregator so no producer
        # outlives the consumer draining the queue.
        if poller is not None:
            await poller.stop()
        if rt.feed_client is not None:
            # Under TrueData this is load-bearing, not tidiness: stop() sends the
            # on-socket logout, and skipping it leaves the session dirty so the
            # NEXT boot starts inside a ~60s "User Already Connected" window.
            # docker-compose's stop_grace_period is raised to 45s for this.
            await rt.feed_client.stop()
        if rt.shadow_feed_client is not None:
            await rt.shadow_feed_client.stop()
        if rt.aggregator is not None:
            await rt.aggregator.stop()
        if rt.shadow_aggregator is not None:
            await rt.shadow_aggregator.stop()
        if settings.run_mode == "live" and settings.feed_vendor == "xts":
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
    resolve on hard refresh when FastAPI serves the built SPA
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
    app.include_router(algo_ws_router)
    # Relay owner endpoint — inert unless TD_RELAY_ENABLED (it answers with an
    # error frame and closes), so mounting it unconditionally is safe.
    from .ws.td_relay import router as td_relay_router

    app.include_router(td_relay_router)

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
