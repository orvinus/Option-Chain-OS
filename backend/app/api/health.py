"""Liveness / introspection endpoints.

Two probes with two audiences:

* ``GET /api/health`` — the dashboard's 3s poll. ALWAYS 200; a pure snapshot.
  Backward compatible: every pre-existing field keeps its exact meaning; the v2
  fields are additive.
* ``GET /api/health/strict`` — for machines that can only act on status codes
  (the VPS oi-sentinel, UptimeRobot-style monitors). Returns 503 when the
  process is genuinely failing its job DURING an open session, 200 otherwise.
  The body carries ``restart_recommended`` so the sentinel never restarts the
  backend for problems a restart cannot fix (broker down, rotation circuit
  parked, DB container dead).

Historical note: during both 2026-08 outages the old always-200 /api/health
reported ``status: "ok"`` for 13+ hours of dead feed — no external monitor could
ever have noticed. That is the whole reason /strict exists.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter
from sqlalchemy import text
from starlette.responses import JSONResponse

from ..auth import get_session_manager
from ..core.config import settings
from ..core.db import AsyncSessionLocal
from ..core.holidays import is_nse_holiday
from ..core.time_utils import (
    MARKET_CLOSE,
    MARKET_OPEN,
    is_nse_regular_session_open,
    now_ist,
)
from ..runtime import get_runtime
from .schemas import HealthResponse

router = APIRouter(tags=["health"])


async def _db_ok() -> bool:
    try:
        async def probe() -> None:
            async with AsyncSessionLocal() as s:
                await s.execute(text("SELECT 1"))

        await asyncio.wait_for(probe(), timeout=2.0)
        return True
    except Exception:
        return False


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    rt = get_runtime()
    sess = get_session_manager()
    feed = rt.feed_client
    steward = rt.steward
    feed_connected = (
        bool(getattr(feed, "is_feed_connected", False)) if feed is not None else False
    )
    ws_tick = getattr(feed, "last_ws_tick_at", 0.0) if feed is not None else 0.0
    from datetime import datetime, timezone

    return HealthResponse(
        status="ok",
        authenticated=sess.authenticated,
        latest_spot=rt.latest_spot,
        tokens_subscribed=len(rt.tokens),
        last_flush_at=_iso(rt.last_flush_at),
        expiries=[e.isoformat() for e in rt.expiries],
        run_mode=settings.run_mode,
        now_ist=now_ist().isoformat(),
        nse_session_open=is_nse_regular_session_open(),
        session_open_ist=MARKET_OPEN.strftime("%H:%M"),
        session_close_ist=MARKET_CLOSE.strftime("%H:%M"),
        feed_connected=feed_connected,
        active_symbol=rt.active_symbol,
        poller_enabled=rt.universe_poller is not None,
        poller_last_sweep_at=_iso(rt.poller_last_sweep_at),
        poller_last_ticks=rt.poller_last_ticks,
        # ---- v2 ----
        last_ws_flush_at=_iso(rt.last_ws_flush_at),
        ws_last_tick_at=(
            datetime.fromtimestamp(ws_tick, tz=timezone.utc).isoformat() if ws_tick else None
        ),
        live_subscriptions=getattr(feed, "live_subscription_count", 0) if feed is not None else 0,
        supervisor_alive=bool(getattr(feed, "supervisor_alive", False)) if feed is not None else False,
        watchdog_last_check_at=_iso(getattr(steward, "last_check_at", None)) if steward else None,
        last_login_at=_iso(sess.last_login_at),
        logins_last_hour=sess.logins_last_hour,
        circuit_state=sess.circuit_state,
        db_ok=await _db_ok(),
        poller_mode=settings.effective_poller_mode,
    )


@router.get("/health/strict")
async def health_strict() -> JSONResponse:
    """503 when the process is failing its job during an open session; else 200.

    Contract with the VPS sentinel (do not change casually — the sentinel greps
    this body): ``restart_recommended`` is false for every cause a backend
    restart cannot fix. Holiday stance: dates in data/nse_holidays.json return
    200 with ``note: "nse_holiday"``; a MISSING holiday causes capped, clearly-
    worded false pages (safe); a wrongly-listed date silences a real outage
    (dangerous) — so the file is maintained conservatively.

    While the REST failover keeps overall data fresh with the socket dead, this
    stays 200 with ``degraded: "rest_failover"``: the sentinel must not restart a
    process that is successfully limping, but the steward keeps escalating and
    the operator keeps getting reminded.
    """
    from datetime import datetime, timezone

    rt = get_runtime()
    sess = get_session_manager()
    steward = rt.steward

    t = now_ist()
    if is_nse_holiday(t.date()):
        return JSONResponse({"status": "ok", "note": "nse_holiday", "reasons": []})

    reasons: list[str] = []
    restart_recommended = False

    db_ok = await _db_ok()
    if not db_ok:
        reasons.append("db_down")

    circuit_open = sess.circuit_state == "open"
    if circuit_open:
        reasons.append("recovery_circuit_open")

    # ---- TrueData: the two causes a restart CANNOT fix -----------------------
    # Both must be reported with restart_recommended=false, and the sentinel has
    # a matching branch. This is the O-1 interaction: without it, the watchdog
    # becomes the wedge's accomplice — it restarts, the restart is itself a dirty
    # disconnect, the session re-wedges, the feed is still stale, and it restarts
    # again. The sentinel would keep a genuine 60-second problem alive for hours.
    wedged = False
    proxy_down = False
    if settings.feed_vendor == "truedata":
        from ..auth.td_session import SessionState, get_td_session
        from ..core import proxy_health

        td = get_td_session()
        wedged = td.state in (SessionState.WEDGED, SessionState.COOLDOWN)
        proxy_down = not proxy_health.is_up()
        if wedged:
            reasons.append("session_wedged")
        if proxy_down:
            reasons.append("proxy_down")

    stale = False
    if settings.run_mode == "live" and is_nse_regular_session_open():
        last = rt.last_flush_at
        if last is None:
            stale = True
        else:
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - last).total_seconds()
            stale = age > settings.strict_stale_after_s
        if stale:
            reasons.append("stale_data")
            # A restart helps only when nothing points at an external cause.
            # A wedged session and a dead proxy are BOTH made worse by one: the
            # restart re-wedges, and it cannot repair host networking.
            restart_recommended = db_ok and not circuit_open and not wedged and not proxy_down

    if reasons:
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "reasons": reasons,
                "restart_recommended": restart_recommended,
                "detail": (sess.circuit_reason if circuit_open else "")
                or ("TrueData session wedged — logoutRequest + cool-down in "
                    "progress; a restart re-wedges it" if wedged else "")
                or ("SOCKS proxy (WARP) unreachable — the vendor cannot be "
                    "reached at all from this host" if proxy_down else "")
                or ("no data flushed during an open session" if stale else "")
                or ("database probe failed" if not db_ok else ""),
            },
        )

    body: dict = {"status": "ok", "reasons": []}
    if steward is not None and not steward.ws_feed_healthy:
        # Data is fresh (that's why we're 200) but it is REST data — say so.
        body["degraded"] = "rest_failover"
    return JSONResponse(body)


@router.get("/health/feed")
async def health_feed() -> JSONResponse:
    """Vendor-specific detail — deliberately a SEPARATE route.

    /api/health and /api/health/strict have a frozen field contract because the
    VPS sentinel greps their bodies; adding vendor internals there risks changing
    a shape a shell script depends on. Everything migration-specific lives here
    instead: entitlements, wedge state, heartbeat age, sequence gaps, subscription
    budget, proxy status, and the shadow pipeline.
    """
    rt = get_runtime()
    body: dict = {
        "feed_vendor": settings.feed_vendor,
        "shadow_enabled": settings.truedata_shadow_enabled,
    }

    feed = rt.feed_client
    if feed is not None and hasattr(feed, "feed_stats"):
        body["live"] = feed.feed_stats()

    shadow = rt.shadow_feed_client
    if shadow is not None and hasattr(shadow, "feed_stats"):
        body["shadow"] = {
            **shadow.feed_stats(),
            "last_flush_at": _iso(rt.shadow_last_flush_at),
            "last_flush_rows": rt.shadow_last_flush_rows,
        }

    if settings.feed_vendor == "truedata" or settings.truedata_shadow_enabled:
        from ..core import proxy_health

        body["proxy"] = proxy_health.snapshot()

    steward = rt.steward
    if steward is not None:
        body["steward"] = {
            "healthy": steward.ws_feed_healthy,
            "stable": getattr(steward, "ws_feed_stable", None),
            "step": getattr(steward, "current_step", None),
            "unhealthy_since_s": getattr(steward, "unhealthy_since_s", None),
            "last_check_at": _iso(getattr(steward, "last_check_at", None)),
        }
    agg = rt.aggregator
    if agg is not None:
        body["aggregator"] = {
            "late_dropped": getattr(agg, "late_dropped", 0),
            "buckets_dropped": getattr(agg, "_dropped_buckets", 0),
        }
    # Gap-fill / session catch-up outcome. Before this the only trace of a
    # failed boot pull was a docker log line — the report had no reader at all.
    from ..ingest import gapfill

    body["gapfill"] = {"last_report": gapfill.last_report, "last_catchup": gapfill.last_catchup}
    return JSONResponse(body)


@router.get("/health/data")
async def health_data() -> JSONResponse:
    """Data-completeness view: day coverage (``oi_day_stats``), the last
    historical gap-fill and same-day catch-up outcomes, and the latest
    integrity summary. Separate route for the same reason as /health/feed."""
    from ..ingest import gapfill
    from ..services import data_integrity
    from ..services.data_health import coverage

    try:
        cov = await coverage()
    except Exception as e:  # noqa: BLE001
        cov = {"error": str(e)}
    return JSONResponse(
        {
            "coverage": cov,
            "gapfill": gapfill.last_report,
            "catchup": gapfill.last_catchup,
            "integrity": data_integrity.last_summary(),
        }
    )


@router.get("/health/data/integrity")
async def health_data_integrity(symbol: str = "NIFTY", day: str | None = None) -> JSONResponse:
    """Full integrity report for one (symbol, IST day) — computed on demand
    (bounded to that day) and persisted to ``data_integrity_runs``."""
    from datetime import date as _date

    from ..core.time_utils import now_ist
    from ..services import data_integrity

    try:
        d = _date.fromisoformat(day) if day else now_ist().date()
    except ValueError:
        return JSONResponse({"error": f"invalid day {day!r}"}, status_code=400)
    report = await data_integrity.day_report(symbol.upper(), d)
    return JSONResponse(report)
