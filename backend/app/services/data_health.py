"""Archive/live day-coverage health: refresh, and the read models over it.

``oi_day_stats`` and ``live_days`` (migration 0013) are materialized views, so
they are only as correct as their last REFRESH. Before this module the ONLY
refresh anywhere was one line in ``scripts/nightly_topup_vps.sh`` — and that
line was an uncommitted working-tree edit, so anything deployed from git never
refreshed at all. A stale index silently corrupts what
``oi_snapshots_unified`` serves, which is what every backtest reads.

So the refresh gets three independent homes and this module is the one
implementation: a backend loop (here), the nightly script, and preflight's
self-heal. Forgetting any one of them is survivable.

TRANSACTION GOTCHA: ``REFRESH MATERIALIZED VIEW CONCURRENTLY`` cannot run
inside a transaction block, and SQLAlchemy opens one implicitly on a normal
session — ``AsyncSessionLocal`` would raise. Hence the explicit AUTOCOMMIT
connection below. Do not "simplify" this back to a session.
"""
from __future__ import annotations

import asyncio
import time
from datetime import date
from typing import Any, Optional

import structlog
from sqlalchemy import text

from ..core.db import AsyncSessionLocal, async_engine
from ..core.holidays import is_nse_holiday
from ..core.time_utils import now_ist

log = structlog.get_logger(__name__)

# Order matters: live_days is derived FROM oi_day_stats.
_REFRESH_ORDER = ("oi_day_stats", "live_days")


async def refresh_data_health() -> dict[str, Any]:
    """Refresh both matviews and record when. Safe to call concurrently with
    readers (CONCURRENTLY needs the unique indexes 0013 creates)."""
    out: dict[str, Any] = {}
    async with async_engine.connect() as conn:
        auto = await conn.execution_options(isolation_level="AUTOCOMMIT")
        for name in _REFRESH_ORDER:
            t0 = time.perf_counter()
            try:
                await auto.execute(
                    text(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {name}")
                )
            except Exception as e:
                # A never-populated matview cannot be refreshed CONCURRENTLY.
                if "concurrently" in str(e).lower():
                    await auto.execute(text(f"REFRESH MATERIALIZED VIEW {name}"))
                else:
                    raise
            ms = int((time.perf_counter() - t0) * 1000)
            rows = (await auto.execute(text(f"SELECT count(*) FROM {name}"))).scalar() or 0
            await auto.execute(
                text(
                    """
                    INSERT INTO data_health_refresh (name, refreshed_at, duration_ms, rows_n)
                    VALUES (:n, now(), :ms, :r)
                    ON CONFLICT (name) DO UPDATE
                      SET refreshed_at = now(), duration_ms = :ms, rows_n = :r
                    """
                ),
                {"n": name, "ms": ms, "r": rows},
            )
            out[name] = {"duration_ms": ms, "rows": rows}
    log.info("data_health.refreshed", **{k: v["rows"] for k, v in out.items()})
    return out


def last_trading_day(today: Optional[date] = None) -> date:
    """The most recent weekday that is not an NSE holiday, strictly before
    today. Holiday-aware on purpose: comparing against "yesterday" would page
    every Saturday morning and every holiday."""
    d = (today or now_ist().date())
    for _ in range(14):
        d = date.fromordinal(d.toordinal() - 1)
        if d.weekday() < 5 and not is_nse_holiday(d):
            return d
    return d


_COVERAGE_SQL = text(
    """
    SELECT symbol,
           MIN(day) FILTER (WHERE winner <> 'none')                       AS first_day,
           MAX(day) FILTER (WHERE winner <> 'none')                       AS last_day,
           count(*) FILTER (WHERE winner <> 'none' AND NOT is_weekend)    AS usable_days,
           count(*) FILTER (WHERE winner <> 'none' AND usable_minutes < :good
                              AND NOT is_weekend)                         AS thin_days,
           count(*) FILTER (WHERE is_weekend AND winner <> 'none')        AS weekend_days
    FROM oi_day_stats
    GROUP BY symbol
    ORDER BY symbol
    """
)

_REFRESHED_SQL = text(
    "SELECT name, refreshed_at FROM data_health_refresh"
)


async def coverage(good_enough_minutes: int = 300) -> dict[str, Any]:
    """Per-symbol true coverage bounds, read from ``oi_day_stats`` ONLY.

    Never scans ``oi_snapshots_unified``: four stacked DISTINCTs over that
    union exhausted the DB pool and starved the tick consumer on 2026-08-11,
    and this endpoint is polled by an open browser tab.
    """
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(_COVERAGE_SQL, {"good": good_enough_minutes})).mappings().all()
        refreshed = {r["name"]: r["refreshed_at"] for r in
                     (await s.execute(_REFRESHED_SQL)).mappings().all()}

    symbols: dict[str, Any] = {}
    overall_first: Optional[date] = None
    overall_last: Optional[date] = None
    for r in rows:
        if r["first_day"] is None:
            continue
        symbols[r["symbol"]] = {
            "first_day": r["first_day"].isoformat(),
            "last_day": r["last_day"].isoformat(),
            "usable_days": int(r["usable_days"] or 0),
            "thin_days": int(r["thin_days"] or 0),
            "weekend_days": int(r["weekend_days"] or 0),
        }
        overall_first = r["first_day"] if overall_first is None else min(overall_first, r["first_day"])
        overall_last = r["last_day"] if overall_last is None else max(overall_last, r["last_day"])

    ltd = last_trading_day()
    stats_at = refreshed.get("oi_day_stats")
    return {
        # The client must NEVER compute "today" itself — doing it with
        # toISOString() yields the UTC date, which before 05:30 IST is
        # yesterday. That bug is why the To field silently showed the wrong day.
        "server_today_ist": now_ist().date().isoformat(),
        "last_trading_day": ltd.isoformat(),
        "stats_refreshed_at": stats_at.isoformat() if stats_at else None,
        "stale": bool(overall_last and overall_last < ltd),
        "bounds": {
            "min": overall_first.isoformat() if overall_first else None,
            "max": overall_last.isoformat() if overall_last else None,
        },
        "default_from": overall_first.isoformat() if overall_first else None,
        # Defaults to the last day that actually HAS data, not today — a `to`
        # of today fills every fresh preflight with "no stored data" rows every
        # morning until the nightly pull lands.
        "default_to": overall_last.isoformat() if overall_last else None,
        "symbols": symbols,
    }


async def run_data_health_loop() -> None:
    """Refresh shortly after boot, then after the close and before the open.

    Independent of the nightly shell script by design: "the timer is dead but
    the backend is alive" must still converge.
    """
    await asyncio.sleep(90.0)
    last_run_day: Optional[tuple[date, int]] = None
    while True:
        try:
            ist = now_ist()
            slot = 0 if ist.hour < 12 else 1          # pre-open / post-close
            key = (ist.date(), slot)
            due = last_run_day != key and (
                (slot == 0 and ist.hour >= 6) or (slot == 1 and ist.hour >= 16)
            )
            if last_run_day is None or due:
                await refresh_data_health()
                last_run_day = key
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.warning("data_health.loop_failed", error=str(e))
        try:
            await asyncio.sleep(300.0)
        except asyncio.CancelledError:
            return
