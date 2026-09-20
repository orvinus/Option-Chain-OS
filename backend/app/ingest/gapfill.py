"""Historical gap fill + same-day SESSION catch-up from the TrueData REST archive.

Two problems, one task:

1. **Multi-day outage** (dev PC off, VPS outage): the live table has no rows
   for those days and every history view, engine warm-up and backtest quietly
   loses them. Trading days in the last ``GAPFILL_LOOKBACK_DAYS`` whose
   ``oi_day_stats.usable_minutes`` is below ``GAPFILL_MIN_MINUTES`` are pulled
   with the existing archive puller (``scripts/truedata_backfill.py pull``),
   then ``oi_day_stats`` / ``live_days`` are refreshed so the unified view
   serves the new rows immediately.

2. **Same-day hole** (boot at 12:00, a socket outage at 11:20): the live feed
   only knows what it saw. ``session_catchup`` lists the session minutes
   (09:15 → now−2min) with no live row, pulls THAT session from the vendor's
   same-day 1-minute bars (``pull --day``), and PROMOTES the restored minutes
   into ``option_oi_snapshots`` with ``src = 1`` wherever the live feed has no
   row for that token in that minute (``ON CONFLICT DO NOTHING`` — a live
   minute always wins, deterministically). Every reader of the live table
   then sees the whole session with zero query changes; the premium path
   additionally prefers the archive's true O/H/L/C for those minutes
   (``series._PREMIUM_MINUTES_TEMPLATE``).

Scheduling (2026-09-09 rewrite): the historical pull is deferred to
``close + GAPFILL_POST_CLOSE_DELAY_MIN`` on a trading day — it competes with
the live socket for the vendor path — UNLESS the lookback has no usable day at
all (fresh install). The session catch-up runs at boot, every
``GAPFILL_CATCHUP_CHECK_S`` while the session is open, and immediately when
the steward reports a recovered feed (``request_catchup``).

Nothing here synthesises market data: a minute the vendor does not have stays
missing and is reported as such (``last_catchup[sym]["missing_after"]``,
``/api/health/data``).

Single-flight: overlapping runs are refused, never queued. Failures are LOUD:
a non-zero puller exit code marks the report ``error`` and pages once.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time as _time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import text

from ..core.config import settings
from ..core.db import AsyncSessionLocal
from ..core.holidays import is_nse_holiday
from ..core.logging import get_logger
from ..core.notify import notify
from ..core.time_utils import (
    IST,
    SESSION_OPEN_MIN,
    ist_naive_to_utc,
    now_ist,
    session_close_min,
)

log = get_logger("gapfill")

_running = asyncio.Lock()
last_report: dict[str, Any] = {}
last_catchup: dict[str, Any] = {}
_catchup_event: Optional[asyncio.Event] = None
_catchup_reason: str = ""
_last_catchup_mono: dict[str, float] = {}
_historical_done_for: Optional[date] = None
_deferred_until: Optional[datetime] = None

_USABLE_DAYS_SQL = text(
    """
    SELECT day
    FROM oi_day_stats
    WHERE symbol = :symbol
      AND day >= :from_day AND day <= :to_day
      AND usable_minutes >= :min_minutes
    """
)

# A day whose ARCHIVE is short while the LIVE table proves the session was
# complete. That pairing is the exact signature of an interrupted nightly
# top-up, and it is invisible to ``_USABLE_DAYS_SQL`` because usable_minutes
# takes the better of the two sources — so the archive stayed truncated
# forever and nothing ever re-pulled it (found 2026-09-12: 10 and 11 Sep were
# archived only to 13:11 and 11:54 for EVERY contract).
#
# Requiring live to be complete is what keeps this from looping: a day the
# vendor genuinely cannot serve is never flagged, and a day that IS re-pulled
# stops matching as soon as its archive fills.
_ARCHIVE_SHORT_DAYS_SQL = text(
    """
    SELECT day
    FROM oi_day_stats
    WHERE symbol = :symbol
      AND day >= :from_day AND day <= :to_day
      AND arch_minutes < :min_minutes
      AND live_minutes >= :min_minutes
    ORDER BY day
    """
)

_LIVE_MINUTES_SQL = text(
    """
    SELECT DISTINCT time_bucket('1 minute', ts) AS m
    FROM option_oi_snapshots
    WHERE symbol = :symbol
      AND option_type IN ('CE', 'PE')
      AND ts >= :open_utc AND ts < :upper_utc
    """
)

# Restored minutes go into the live table ONLY where the live feed has no row
# for that token in that minute. ``src = 1`` marks them (auditable, deletable,
# and excluded from the premium path's live arm). ``oi > 0`` mirrors the
# feed's never-persist-zero-OI rule.
_PROMOTE_SQL = text(
    """
    INSERT INTO option_oi_snapshots
        (ts, symbol, expiry, strike, option_type, token, oi, ltp, volume, underlying, src)
    SELECT a.ts, a.symbol, a.expiry, a.strike, a.option_type, a.token,
           a.oi, a.close, a.volume_cum, a.underlying, 1
    FROM oi_archive_bars a
    WHERE a.symbol = :symbol
      AND a.option_type IN ('CE', 'PE')
      AND a.oi > 0
      AND a.ts >= :from_utc AND a.ts < :to_utc
      AND NOT EXISTS (
          SELECT 1 FROM option_oi_snapshots l
          WHERE l.token = a.token
            AND l.src IS NULL
            AND l.ts >= a.ts AND l.ts < a.ts + INTERVAL '1 minute'
      )
    ON CONFLICT (ts, token) DO NOTHING
    """
)


def _script_path() -> Optional[Path]:
    """Locate scripts/truedata_backfill.py both from a source checkout
    (repo/backend/app/ingest → repo/scripts) and inside the image (/app/scripts)."""
    here = Path(__file__).resolve()
    candidates = [
        here.parents[3] / "scripts" / "truedata_backfill.py" if len(here.parents) > 3 else None,
        Path("/app/scripts/truedata_backfill.py"),
        Path.cwd() / "scripts" / "truedata_backfill.py",
    ]
    for c in candidates:
        if c is not None and c.exists():
            return c
    return None


def _settlement_script_path() -> Optional[Path]:
    """Locate scripts/nse_settlement_backfill.py (same two layouts)."""
    here = Path(__file__).resolve()
    candidates = [
        here.parents[3] / "scripts" / "nse_settlement_backfill.py" if len(here.parents) > 3 else None,
        Path("/app/scripts/nse_settlement_backfill.py"),
        Path.cwd() / "scripts" / "nse_settlement_backfill.py",
    ]
    for c in candidates:
        if c is not None and c.exists():
            return c
    return None


def _package_root() -> Path:
    """Directory that contains the ``app`` package (repo/backend or /app) —
    exported to the puller subprocess as PYTHONPATH so the script's own path
    resolution is not the only thing standing between it and ImportError."""
    return Path(__file__).resolve().parents[2]


def expected_trading_days(from_day: date, to_day: date) -> list[date]:
    out: list[date] = []
    d = from_day
    while d <= to_day:
        if d.weekday() < 5 and not is_nse_holiday(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and not is_nse_holiday(d)


def expected_session_minutes(day: date, upper_min: Optional[int] = None) -> list[int]:
    """Minute-of-day grid of the session on ``day``: 09:15 up to (exclusive)
    ``min(close, upper_min)``. Empty on a non-trading day — those minutes are
    "no trading", never "missing"."""
    if not is_trading_day(day):
        return []
    end = session_close_min(day)
    if upper_min is not None:
        end = min(end, upper_min)
    return list(range(SESSION_OPEN_MIN, max(SESSION_OPEN_MIN, end)))


def missing_minutes(expected: list[int], covered: set[int]) -> list[int]:
    """Pure helper (tested): expected minute-of-day grid minus covered."""
    return [m for m in expected if m not in covered]


def minute_runs(mins: list[int]) -> list[tuple[str, str, int]]:
    """Collapse sorted minute-of-day values into ``(HH:MM, HH:MM, count)`` runs."""
    runs: list[tuple[str, str, int]] = []
    if not mins:
        return runs
    start = prev = mins[0]
    for m in mins[1:]:
        if m == prev + 1:
            prev = m
            continue
        runs.append((f"{start // 60:02d}:{start % 60:02d}", f"{prev // 60:02d}:{prev % 60:02d}", prev - start + 1))
        start = prev = m
    runs.append((f"{start // 60:02d}:{start % 60:02d}", f"{prev // 60:02d}:{prev % 60:02d}", prev - start + 1))
    return runs


def historical_allowed(now: datetime, usable_days_in_lookback: int) -> tuple[bool, Optional[datetime]]:
    """Market-hours policy for the months-long pull.

    Returns ``(allowed, deferred_until)``. In session on a trading day the
    pull is deferred to ``close + GAPFILL_POST_CLOSE_DELAY_MIN`` — unless the
    lookback holds NO usable day at all (fresh install: nothing to protect,
    everything to gain). Outside the session it always runs."""
    d = now.date()
    if not is_trading_day(d):
        return True, None
    hm = now.hour * 60 + now.minute
    close = session_close_min(d) + int(settings.gapfill_post_close_delay_min)
    if SESSION_OPEN_MIN <= hm < close:
        if usable_days_in_lookback == 0:
            return True, None
        until = now.replace(hour=close // 60, minute=close % 60, second=0, microsecond=0)
        return False, until
    return True, None


async def missing_days(symbol: str, lookback_days: int, min_minutes: int) -> list[date]:
    today = now_ist().date()
    to_day = today - timedelta(days=1)          # today is the session catch-up's job
    from_day = today - timedelta(days=lookback_days)
    expected = expected_trading_days(from_day, to_day)
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(
                _USABLE_DAYS_SQL,
                {"symbol": symbol, "from_day": from_day, "to_day": to_day, "min_minutes": min_minutes},
            )
        ).scalars().all()
    have = {r for r in rows}
    return [d for d in expected if d not in have]


async def archive_short_days(symbol: str, lookback_days: int, min_minutes: int) -> list[date]:
    """Past sessions whose vendor archive is truncated even though the live
    table holds a full day (see ``_ARCHIVE_SHORT_DAYS_SQL``).

    These need a targeted re-pull: the archive is the durable, exchange-grade
    per-minute store, while live ticks are retained for ~120 days and fold
    from LTP rather than carrying real O/H/L/C.
    """
    today = now_ist().date()
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(
                _ARCHIVE_SHORT_DAYS_SQL,
                {
                    "symbol": symbol,
                    "from_day": today - timedelta(days=lookback_days),
                    "to_day": today - timedelta(days=1),   # today is the catch-up's job
                    "min_minutes": min_minutes,
                },
            )
        ).scalars().all()
    return list(rows)


async def usable_days_count(symbol: str, lookback_days: int, min_minutes: int) -> int:
    today = now_ist().date()
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(
                _USABLE_DAYS_SQL,
                {
                    "symbol": symbol,
                    "from_day": today - timedelta(days=lookback_days),
                    "to_day": today,
                    "min_minutes": min_minutes,
                },
            )
        ).scalars().all()
    return len(rows)


# Re-open checkpoints for still-trading expiries, the recent index/futures
# chunks, the (once-only) EOD series units, and every unit that ended in
# 'error' — those are exactly the quota-exhausted / transient-failure units and
# must be retried, not retired.
_RESET_LEDGER_SQL = text(
    """
    DELETE FROM oi_backfill_progress
    WHERE (
            split_part(unit_key, ':', 1) = :symbol
            AND (
                 (split_part(unit_key, ':', 2) ~ '^[0-9]{6}$'
                  AND to_date(split_part(unit_key, ':', 2), 'YYMMDD') >= :expiry_floor)
              OR (split_part(unit_key, ':', 2) IN ('IDX', 'FUT')
                  AND to_date(split_part(unit_key, ':', 3), 'YYMMDD') >= :day_floor)
            )
          )
       OR unit_key LIKE 'eod:td:' || :symbol || ':%'
       OR (status = 'error'
           AND (split_part(unit_key, ':', 1) = :symbol OR unit_key LIKE 'bhav:%'))
    """
)


async def _reset_recent_ledger(symbol: str, lookback_days: int) -> int:
    """Re-open the puller's checkpoints for still-trading expiries.

    The puller marks an option-chain unit 'done' per EXPIRY, so a chain that was
    pulled mid-life (e.g. on the 21st for an expiry on the 8th of next month)
    is never revisited and the days after the 21st stay missing forever. The
    nightly VPS script deletes the recent ledger rows for exactly this reason;
    the gap fill must do the same or it silently pulls nothing.
    """
    today = now_ist().date()
    async with AsyncSessionLocal() as s:
        res = await s.execute(
            _RESET_LEDGER_SQL,
            {
                "symbol": symbol,
                "expiry_floor": today - timedelta(days=lookback_days),
                "day_floor": today - timedelta(days=lookback_days + 7),
            },
        )
        await s.commit()
        n = res.rowcount or 0
    log.info("gapfill.ledger_reset", symbol=symbol, rows=n)
    return n


async def _settlement_pass() -> dict[str, Any]:
    """Top up ``eod_bars`` from NSE's own UDiFF settlement file.

    The importer skips dates it already has, so this is cheap on every boot
    but a no-op after the first. It is deliberately tolerant: any failure is
    reported and swallowed, because the vendor gap-fill below is the more
    important job and must still run."""
    script = _settlement_script_path()
    if script is None:
        log.warning("gapfill.settlement_skipped", why="scripts/nse_settlement_backfill.py not found")
        return {"status": "skipped_no_script"}
    out: dict[str, Any] = {"symbols": {}}
    symbols = [s.strip().upper() for s in settings.gapfill_symbols.split(",") if s.strip()]
    # NSE only. This is the NSE F&O file; a BSE underlying (SENSEX) is simply
    # not in it, and because nothing is ever written the "already present"
    # check can never short-circuit -- it would re-download ~55 MB of archive
    # on every single boot to find nothing.
    try:
        from ..market.symbols import get_registry

        reg = get_registry()
        skipped = [s for s in symbols
                   if (reg.get(s) is not None and reg.get(s).exchange != "NSE")]
        symbols = [s for s in symbols if s not in skipped]
        if skipped:
            out["skipped_non_nse"] = skipped
    except Exception as e:  # noqa: BLE001
        log.warning("gapfill.settlement_registry_unavailable", error=str(e))
    for sym in symbols:
        try:
            rc, tail = await _run_puller(
                sym, script, "--symbol", sym,
                "--days", str(settings.settlement_backfill_days),
            )
        except Exception as e:  # noqa: BLE001
            log.error("gapfill.settlement_failed", symbol=sym, error=str(e))
            out["symbols"][sym] = {"error": str(e)}
            continue
        out["symbols"][sym] = {"rc": rc, "tail": tail[-3:] if rc != 0 else tail[-1:]}
    return out


async def _run_puller(symbol: str, script: Path, *extra: str) -> tuple[int, list[str]]:
    """Run one puller sub-command → ``(exit_code, last_output_lines)``.

    Default = the 1-minute chain archive (``pull``); ``extra`` overrides the
    sub-command and its flags. ``-1`` = timeout. A cancellation kills the
    child (it used to outlive a graceful shutdown)."""
    args = list(extra) if extra else ["pull", "--symbol", symbol, "--include-live-days"]
    cmd = [sys.executable, "-u", str(script), *args]
    log.info("gapfill.pull_start", symbol=symbol, cmd=" ".join(cmd))
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    pkg = str(_package_root())
    env["PYTHONPATH"] = pkg + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(script.parents[1]),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    assert proc.stdout is not None
    tail: list[str] = []
    try:
        async for line in proc.stdout:
            txt = line.decode("utf-8", "replace").rstrip()
            if txt:
                log.info("gapfill.pull", symbol=symbol, line=txt[:300])
                tail.append(txt[:300])
                if len(tail) > 20:
                    del tail[0]
        rc = await asyncio.wait_for(proc.wait(), timeout=settings.gapfill_timeout_s)
        if rc != 0:
            log.error("gapfill.pull_failed", symbol=symbol, rc=rc, tail=tail[-3:])
        return rc, tail
    except asyncio.TimeoutError:
        proc.kill()
        log.error("gapfill.pull_timeout", symbol=symbol)
        return -1, tail
    except asyncio.CancelledError:
        proc.kill()
        raise


# --------------------------------------------------------------------- catch-up

def request_catchup(reason: str) -> None:
    """Wake the scheduler for an immediate session catch-up (feed recovery)."""
    global _catchup_reason
    _catchup_reason = reason
    if _catchup_event is not None:
        _catchup_event.set()


async def missing_session_minutes(symbol: str, day: date, now: datetime) -> list[int]:
    """Minute-of-day values in ``[09:15, min(now−2min, close))`` with no live
    CE/PE row for ``symbol`` (the hold-last refresher guarantees a row per
    connected minute, so absence means the feed was not there)."""
    now_ist_ = now.astimezone(IST)
    upper_min = now_ist_.hour * 60 + now_ist_.minute - 2
    expected = expected_session_minutes(day, upper_min)
    if not expected:
        return []
    open_utc = ist_naive_to_utc(datetime.combine(day, datetime.min.time()) + timedelta(minutes=SESSION_OPEN_MIN))
    upper_utc = ist_naive_to_utc(datetime.combine(day, datetime.min.time()) + timedelta(minutes=expected[-1] + 1))
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(
                _LIVE_MINUTES_SQL, {"symbol": symbol, "open_utc": open_utc, "upper_utc": upper_utc}
            )
        ).scalars().all()
    covered: set[int] = set()
    for m in rows:
        if m is None:
            continue
        if getattr(m, "tzinfo", None) is None:
            m = m.replace(tzinfo=timezone.utc)
        mi = m.astimezone(IST)
        covered.add(mi.hour * 60 + mi.minute)
    return missing_minutes(expected, covered)


async def _promote_restored_minutes(symbol: str, day: date, from_min: int, to_min: int) -> int:
    base = datetime.combine(day, datetime.min.time())
    from_utc = ist_naive_to_utc(base + timedelta(minutes=from_min))
    to_utc = ist_naive_to_utc(base + timedelta(minutes=to_min))
    async with AsyncSessionLocal() as s:
        res = await s.execute(
            _PROMOTE_SQL, {"symbol": symbol, "from_utc": from_utc, "to_utc": to_utc}
        )
        await s.commit()
        return res.rowcount or 0


async def session_catchup(symbol: str, *, reason: str, script: Optional[Path] = None) -> dict[str, Any]:
    """Restore today's missing session minutes for ``symbol`` (see module doc)."""
    now = datetime.now(timezone.utc)
    today = now.astimezone(IST).date()
    rep: dict[str, Any] = {"reason": reason, "day": today.isoformat(), "at": now_ist().isoformat()}
    if not is_trading_day(today):
        rep["status"] = "not_trading_day"
        return rep
    hm = now.astimezone(IST).hour * 60 + now.astimezone(IST).minute
    if hm < SESSION_OPEN_MIN + 3:
        rep["status"] = "before_open"
        return rep
    last = _last_catchup_mono.get(symbol)
    if last is not None and _time.monotonic() - last < settings.gapfill_catchup_min_interval_s and reason != "feed_recovered":
        rep["status"] = "throttled"
        return rep
    try:
        missing = await missing_session_minutes(symbol, today, now)
    except Exception as e:  # noqa: BLE001
        rep["status"] = "scan_failed"
        rep["error"] = str(e)
        log.error("gapfill.catchup_scan_failed", symbol=symbol, error=str(e))
        return rep
    rep["missing_before"] = len(missing)
    rep["missing_before_runs"] = minute_runs(missing)
    if len(missing) < settings.gapfill_catchup_min_missing:
        rep["status"] = "complete"
        return rep
    script = script or _script_path()
    if script is None:
        rep["status"] = "skipped_no_script"
        return rep
    _last_catchup_mono[symbol] = _time.monotonic()
    log.warning("gapfill.session_gap", symbol=symbol, minutes=len(missing), runs=rep["missing_before_runs"][:5])
    rc, tail = await _run_puller(
        symbol, script, "pull", "--symbol", symbol, "--day", today.isoformat(),
        "--max-expiries", str(settings.gapfill_catchup_max_expiries),
    )
    rep["puller_rc"] = rc
    if rc not in (0, 2):
        rep["status"] = "error"
        rep["tail"] = tail[-5:]
        return rep
    try:
        # Promote from 2 minutes before the first hole to the scan's upper bound.
        upper = missing[-1] + 1
        promoted = await _promote_restored_minutes(symbol, today, max(SESSION_OPEN_MIN, missing[0] - 2), upper)
        rep["promoted_rows"] = promoted
        after = await missing_session_minutes(symbol, today, datetime.now(timezone.utc))
        rep["missing_after"] = len(after)
        rep["missing_after_runs"] = minute_runs(after)
        rep["status"] = "ok" if rc == 0 else "ok_with_unit_errors"
        log.info("gapfill.catchup_done", symbol=symbol, promoted=promoted,
                 missing_before=len(missing), missing_after=len(after))
    except Exception as e:  # noqa: BLE001
        rep["status"] = "promote_failed"
        rep["error"] = str(e)
        log.error("gapfill.promote_failed", symbol=symbol, error=str(e))
    return rep


async def run_session_catchup(*, reason: str) -> dict[str, Any]:
    """All configured symbols; refreshes the day index when anything landed."""
    global last_catchup
    if _running.locked():
        return {"status": "busy"}
    async with _running:
        if not settings.truedata_user or not settings.truedata_password:
            return {"status": "skipped_no_truedata_credentials"}
        symbols = [s.strip().upper() for s in settings.gapfill_symbols.split(",") if s.strip()]
        out: dict[str, Any] = {}
        landed = False
        for sym in symbols:
            try:
                out[sym] = await session_catchup(sym, reason=reason)
                landed = landed or bool(out[sym].get("promoted_rows"))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                out[sym] = {"status": "failed", "error": str(e)}
                log.error("gapfill.catchup_failed", symbol=sym, error=str(e))
        if landed:
            try:
                from ..services.data_health import refresh_data_health
                await refresh_data_health()
            except Exception as e:  # noqa: BLE001
                log.error("gapfill.refresh_failed", error=str(e))
        last_catchup = out
        return out


# ------------------------------------------------------------------ historical

async def run_gapfill_once(*, reason: str = "boot") -> dict[str, Any]:
    global last_report, _historical_done_for, _deferred_until
    if _running.locked():
        return {"status": "busy"}
    async with _running:
        report: dict[str, Any] = {"reason": reason, "started_at": now_ist().isoformat(), "symbols": {}}

        # ── NSE settlement bars ──
        # Runs FIRST and unconditionally, because its preconditions differ from
        # everything below: the archive is public (no vendor credentials) and
        # the rows it fills are for days that have no minute bars at all, so a
        # minute-gap scan can never detect them. They are what gives a young
        # option contract its daily and weekly history — the days it was listed
        # but never traded — which the UMP engine's DISCOVERY and BRIDGE levels
        # are built from. Failures are recorded and never block the vendor pass.
        if settings.settlement_backfill_on_boot:
            report["settlement"] = await _settlement_pass()

        if not settings.truedata_user or not settings.truedata_password:
            report["status"] = "skipped_no_truedata_credentials"
            last_report = report
            log.info("gapfill.skipped", why="no TrueData REST credentials")
            return report
        script = _script_path()
        if script is None:
            report["status"] = "skipped_no_script"
            last_report = report
            log.warning("gapfill.skipped", why="scripts/truedata_backfill.py not found")
            return report

        symbols = [s.strip().upper() for s in settings.gapfill_symbols.split(",") if s.strip()]
        any_gap = False
        errors: list[str] = []
        for sym in symbols:
            try:
                gaps = await missing_days(sym, settings.gapfill_lookback_days, settings.gapfill_min_minutes)
                usable = await usable_days_count(sym, settings.gapfill_lookback_days, settings.gapfill_min_minutes)
            except Exception as e:  # noqa: BLE001
                log.error("gapfill.scan_failed", symbol=sym, error=str(e))
                report["symbols"][sym] = {"error": str(e)}
                errors.append(f"{sym}: scan failed")
                continue
            try:
                short = await archive_short_days(
                    sym, settings.gapfill_lookback_days, settings.gapfill_min_minutes
                )
            except Exception as e:  # noqa: BLE001
                log.error("gapfill.archive_scan_failed", symbol=sym, error=str(e))
                short = []
            report["symbols"][sym] = {
                "missing_before": [d.isoformat() for d in gaps], "usable_days": usable,
                "archive_short": [d.isoformat() for d in short],
            }

            if not gaps and not short:
                continue
            # Both jobs are historical, so both wait for the same window: a
            # vendor pull competing with the live feed during the session is
            # the thing ``historical_allowed`` exists to prevent.
            allowed, until = historical_allowed(now_ist(), usable)
            if not allowed:
                _deferred_until = until
                report["symbols"][sym]["deferred_until"] = until.isoformat() if until else None
                log.info("gapfill.deferred", symbol=sym, days=len(gaps),
                         archive_short=len(short),
                         until=until.isoformat() if until else None)
                continue

            # A truncated archive day is repaired on its own, BEFORE the gap
            # branch below: the day is not "missing" (live covers it), so the
            # broad pull would never look at it, and left alone it stays
            # truncated forever. One targeted --day pull per short day.
            for d in short:
                any_gap = True
                log.warning("gapfill.archive_truncated", symbol=sym, day=d.isoformat())
                rc_s, tail_s = await _run_puller(
                    sym, script, "pull", "--symbol", sym, "--day", d.isoformat(),
                    "--max-expiries", "3",
                )
                report["symbols"][sym].setdefault("archive_repull", {})[d.isoformat()] = rc_s
                if rc_s not in (0, 2):
                    errors.append(f"{sym}: archive re-pull {d} rc={rc_s}")
                    report["symbols"][sym].setdefault("archive_tail", {})[d.isoformat()] = tail_s[-3:]

            if not gaps:
                continue
            any_gap = True
            log.warning("gapfill.gap_detected", symbol=sym, days=len(gaps), first=gaps[0].isoformat(), last=gaps[-1].isoformat())
            try:
                await _reset_recent_ledger(sym, settings.gapfill_lookback_days)
            except Exception as e:  # noqa: BLE001
                log.error("gapfill.ledger_reset_failed", symbol=sym, error=str(e))
            rc, tail = await _run_puller(sym, script)
            report["symbols"][sym]["puller_rc"] = rc
            if rc not in (0, 2):
                report["symbols"][sym]["tail"] = tail[-5:]
                errors.append(f"{sym}: pull rc={rc}")
            # Official daily closes for the same window (NSE bhavcopy; the
            # puller itself skips the bhavcopy pass for non-NSE symbols).
            # TradingView's daily bars — and therefore the UMP engine's
            # d1..d3 / weekly closes — carry this value, not the last trade.
            rc_eod, tail_eod = await _run_puller(
                sym, script, "pull-eod", "--symbol", sym,
                "--days", str(settings.gapfill_lookback_days + 7), "--years", "1",
            )
            report["symbols"][sym]["eod_rc"] = rc_eod
            if rc_eod not in (0, 2):
                report["symbols"][sym]["eod_tail"] = tail_eod[-5:]
                errors.append(f"{sym}: pull-eod rc={rc_eod}")

        if any_gap:
            try:
                from ..services.data_health import refresh_data_health
                await refresh_data_health()
            except Exception as e:  # noqa: BLE001
                log.error("gapfill.refresh_failed", error=str(e))
            for sym in symbols:
                try:
                    after = await missing_days(sym, settings.gapfill_lookback_days, settings.gapfill_min_minutes)
                    report["symbols"].setdefault(sym, {})["missing_after"] = [d.isoformat() for d in after]
                except Exception:  # noqa: BLE001
                    pass
            filled = {
                s: len(v.get("missing_before", [])) - len(v.get("missing_after", []))
                for s, v in report["symbols"].items() if "missing_before" in v
            }
            notify(
                "gapfill_done",
                "🧩 Historical gap-fill finished: "
                + ", ".join(f"{s} +{n} day(s)" for s, n in filled.items())
                + ". Remaining gaps: "
                + ", ".join(f"{s}={len(v.get('missing_after', []))}" for s, v in report["symbols"].items()),
            )
            _historical_done_for = now_ist().date()
        if errors:
            report["status"] = "error"
            report["errors"] = errors
            notify("gapfill_failed", "❌ Gap-fill puller failed: " + "; ".join(errors))
        else:
            report["status"] = "ok" if any_gap else "no_gaps"
        report["finished_at"] = now_ist().isoformat()
        last_report = report
        log.info("gapfill.done", status=report["status"])
        return report


# ------------------------------------------------------------------- scheduler

async def _nightly_integrity() -> None:
    """Integrity report for today + the previous session (persisted)."""
    try:
        from ..services.data_health import last_trading_day
        from ..services.data_integrity import run_integrity

        today = now_ist().date()
        symbols = [s.strip().upper() for s in settings.gapfill_symbols.split(",") if s.strip()]
        await run_integrity(symbols, [today, last_trading_day(today)])
    except Exception as e:  # noqa: BLE001
        log.warning("gapfill.integrity_failed", error=str(e))


def _in_session_now(now: datetime) -> bool:
    d = now.date()
    hm = now.hour * 60 + now.minute
    return is_trading_day(d) and SESSION_OPEN_MIN <= hm < session_close_min(d)


async def run_gapfill_loop() -> None:
    """Supervised task. Boot: refresh the day index FIRST (the old order read
    the matview 30 s after data-health started refreshing it), then the session
    catch-up, then the historical scan. Afterwards a 60 s scheduler tick:
    catch-up checks while the session is open (and immediately on
    ``request_catchup``), the historical run once after the close, and the
    ``GAPFILL_RECHECK_H`` fallback."""
    global _catchup_event, _catchup_reason, _historical_done_for
    if not settings.gapfill_on_boot:
        log.info("gapfill.disabled")
        return
    _catchup_event = asyncio.Event()
    await asyncio.sleep(settings.gapfill_delay_s)
    try:
        from ..services.data_health import refresh_data_health
        await refresh_data_health()
    except Exception as e:  # noqa: BLE001
        log.warning("gapfill.boot_refresh_failed", error=str(e))
    if _in_session_now(now_ist()):
        try:
            await run_session_catchup(reason="boot")
        except Exception as e:  # noqa: BLE001
            log.error("gapfill.catchup_run_failed", error=str(e))
    try:
        await run_gapfill_once(reason="boot")
    except Exception as e:  # noqa: BLE001
        log.error("gapfill.run_failed", error=str(e))
    if _historical_done_for == now_ist().date():
        # The boot pass doubled as today's post-close run: produce the nightly
        # integrity report now (the scheduler's post-close branch will not).
        await _nightly_integrity()

    last_catchup_check = _time.monotonic()
    last_hist = _time.monotonic()
    while True:
        try:
            await asyncio.wait_for(_catchup_event.wait(), timeout=60.0)
        except asyncio.TimeoutError:
            pass
        try:
            now = now_ist()
            mono = _time.monotonic()
            if _catchup_event.is_set():
                _catchup_event.clear()
                reason, _catchup_reason = _catchup_reason or "requested", ""
                if _in_session_now(now):
                    await run_session_catchup(reason=reason)
                last_catchup_check = mono
            elif _in_session_now(now) and mono - last_catchup_check >= settings.gapfill_catchup_check_s:
                last_catchup_check = mono
                await run_session_catchup(reason="scheduled")

            post_close = (
                is_trading_day(now.date())
                and now.hour * 60 + now.minute
                >= session_close_min(now.date()) + int(settings.gapfill_post_close_delay_min)
                and _historical_done_for != now.date()
            )
            deferred_due = _deferred_until is not None and now >= _deferred_until
            fallback_due = mono - last_hist >= max(600.0, settings.gapfill_recheck_h * 3600.0)
            if post_close or deferred_due or fallback_due:
                last_hist = mono
                rep = await run_gapfill_once(reason="post_close" if post_close else "scheduled")
                if rep.get("status") in ("ok", "no_gaps", "error"):
                    _historical_done_for = now.date()
                if post_close:
                    await _nightly_integrity()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.error("gapfill.loop_failed", error=str(e))
