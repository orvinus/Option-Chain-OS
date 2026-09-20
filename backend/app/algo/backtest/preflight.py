"""Data-integrity preflight — the honesty gate before any backtest runs.

Every known wart in the 6-month archive is checked and materialized as a
per-day verdict, so a run can never silently produce results from data we
know is broken:

- TZ-suspect days (the −23-minute ``td_getbars`` incident; the archive was
  repaired to ``td_getbars_tzfixed``, this guards against regressions).
- ``live_days`` staleness: a day present in BOTH the live table and the
  archive but missing from the matview would be served TWICE by the unified
  view.
- The operator-catalogued OI-collapse days (validate_20260811_1429.md) —
  excluded by default, user-overridable per run.
- Spot-less sessions (40 catalogued): the run proceeds, flagged — the ATM
  basket degrades to the full stored chain exactly as live would.
- Per-day coverage below ``min_minutes_per_day`` → skipped.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Optional

from sqlalchemy import text

from ...core.db import AsyncSessionLocal
from ...core.time_utils import SESSION_OPEN_MIN, ist_naive_to_utc, session_close_min
from ..config_models import AlgoConfig
from .data import ExpiryMap, load_expiry_map

# Intra-day OI-collapse days from logs/backfill/validate_20260811_1429.md
# (Issue-5 class: closing OI at 0–20% of the day's max — vendor data damage,
# not market behavior). Excluded by default; a run's settings may re-include
# them by omitting them from excluded_days when use_default_exclusions=False.
DEFAULT_EXCLUDED_DAYS: dict[str, tuple[str, ...]] = {
    "NIFTY": (
        "2026-08-04",
        "2026-08-05",
    ),
    "SENSEX": (
        "2026-02-19",
        "2026-02-27",
        "2026-03-04",
        "2026-03-05",
        "2026-03-13",
        "2026-03-20",
        "2026-03-23",
        "2026-03-24",
        "2026-03-27",
        "2026-04-13",
        "2026-04-20",
    ),
}

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")

# One archive scan yields BOTH the day list and the tz-suspect verdicts
# (first bar outside 09:12–09:20 IST) — the archive is 22M rows, so a second
# full GROUP BY for the suspects alone would double the preflight cost.
_ARCHIVE_DAY_FIRSTS_SQL = text(
    """
    SELECT (ts AT TIME ZONE 'Asia/Kolkata')::date AS day,
           min((ts AT TIME ZONE 'Asia/Kolkata')::time) AS first_t
    FROM oi_archive_bars
    WHERE symbol = :symbol AND option_type IN ('CE','PE')
    GROUP BY 1
    ORDER BY 1
    """
)
_LIVE_DAYS_SQL = text(
    """
    SELECT DISTINCT (ts AT TIME ZONE 'Asia/Kolkata')::date AS d
    FROM option_oi_snapshots
    WHERE symbol = :symbol AND option_type IN ('CE','PE')
    ORDER BY 1
    """
)
# Days the QUALITY-aware index knows about (migration 0013). A day present in
# both tables but absent here means the matview is stale — only then would the
# unified view serve both arms (the old presence-based rule skipped every
# archive-won day as a "duplication risk"; QA 2026-09-02 H3).
_STATS_DAYS_SQL = text("SELECT day FROM oi_day_stats WHERE symbol = :symbol")
_STATS_REFRESHED_SQL = text(
    "SELECT refreshed_at FROM data_health_refresh WHERE name = 'oi_day_stats'"
)
# Whole-range coverage in ONE query instead of one per day (each per-day query
# repaid the unified view's full planning cost). The time-of-day filter equals
# the old per-day [09:15:00, 15:40:00] inclusive session bounds exactly, and
# minute buckets never cross IST dates, so per-(day, expiry) counts are
# identical to the per-day query's.
_RANGE_COVERAGE_SQL = text(
    """
    SELECT (ts AT TIME ZONE 'Asia/Kolkata')::date AS day,
           expiry,
           count(DISTINCT time_bucket('1 minute', ts)) AS minutes,
           count(underlying) AS spot_rows
    FROM oi_snapshots_unified
    WHERE symbol = :symbol
      AND option_type IN ('CE','PE')
      AND ts >= :range_open AND ts <= :range_close
      AND (ts AT TIME ZONE 'Asia/Kolkata')::time
          BETWEEN TIME '09:15' AND TIME '15:40'
    GROUP BY 1, 2
    """
)


async def _range_coverage(
    sym: str, from_date: date, to_date: date
) -> dict[tuple[date, date], tuple[int, int]]:
    """Coverage for the whole window, sharded by calendar month and run on
    concurrent connections — the scan is decompression-bound, so parallel
    connections cut a 6-month sweep from ~30s to a handful. Shard edges sit
    at IST midnights: an IST day is always wholly inside one shard, so the
    per-(day, expiry) groups never split and the merge is a plain update."""
    shards: list[tuple[datetime, datetime]] = []
    m0 = date(from_date.year, from_date.month, 1)
    while m0 <= to_date:
        next_m = (m0 + timedelta(days=32)).replace(day=1)
        lo = max(from_date, m0)
        hi = min(to_date, next_m - timedelta(days=1))
        shards.append(
            (
                ist_naive_to_utc(
                    datetime.combine(lo, time(9, 15))
                    if lo == from_date else datetime.combine(lo, time(0, 0))
                ),
                ist_naive_to_utc(
                    datetime.combine(hi, time(15, 40))
                    if hi == to_date else datetime.combine(hi, time(23, 59, 59))
                ),
            )
        )
        m0 = next_m

    sem = asyncio.Semaphore(4)

    async def one(open_ts: datetime, close_ts: datetime):
        async with sem:
            async with AsyncSessionLocal() as s:
                return (
                    await s.execute(
                        _RANGE_COVERAGE_SQL,
                        {
                            "symbol": sym,
                            "range_open": open_ts,
                            "range_close": close_ts,
                        },
                    )
                ).all()

    out: dict[tuple[date, date], tuple[int, int]] = {}
    for rows in await asyncio.gather(*[one(o, c) for o, c in shards]):
        for r in rows:
            out[(r[0], r[1])] = (int(r[2] or 0), int(r[3] or 0))
    return out


@dataclass
class DayPlan:
    trade_date: str
    symbol: str
    expiry: Optional[str]
    minutes: int = 0
    spotless: bool = False
    planned: bool = True
    skip_reason: str = ""
    expected_minutes: int = 0        # the date's own session length
    coverage_pct: float = 0.0


@dataclass
class PreflightReport:
    from_date: str
    to_date: str
    tz_suspect_days: dict[str, list[str]] = field(default_factory=dict)
    live_days_dup_risk: dict[str, list[str]] = field(default_factory=dict)
    default_exclusions: dict[str, list[str]] = field(default_factory=dict)
    days: list[DayPlan] = field(default_factory=list)
    planned: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "from_date": self.from_date,
            "to_date": self.to_date,
            "tz_suspect_days": self.tz_suspect_days,
            "live_days_dup_risk": self.live_days_dup_risk,
            "default_exclusions": self.default_exclusions,
            "days": [vars(d) for d in self.days],
            "planned": self.planned,
            "skipped": self.skipped,
            "warnings": self.warnings,
        }


async def run_preflight(
    cfg: AlgoConfig,
    settings: dict[str, Any],
    from_date: date,
    to_date: date,
) -> PreflightReport:
    report = PreflightReport(from_date=from_date.isoformat(), to_date=to_date.isoformat())

    # Which symbols does this config actually trade (per weekday)?
    day_symbols: dict[str, str] = {}
    for wd, day_cfg in cfg.days.items():
        day_symbols[wd] = day_cfg.index_symbol
    symbols = sorted(set(day_symbols.values()))
    if not symbols:
        report.warnings.append("config has no trading days — nothing to plan")
        return report

    symbol_filter = [s for s in (settings.get("symbols") or []) if s]
    use_defaults = bool(settings.get("use_default_exclusions", True))
    extra_excluded = set(settings.get("excluded_days") or [])
    min_minutes = int(settings.get("min_minutes_per_day", 300))
    allow_suspect_tz = bool(settings.get("allow_suspect_tz", False))

    holidays = {h.date for h in cfg.global_.holidays}
    # Frozen platform holidays (settings["platform_holidays"], snapshotted at
    # run creation) — unioned exactly as the live orchestrator does.
    platform_holidays = set(settings.get("platform_holidays") or [])
    expiry_map: ExpiryMap = await load_expiry_map(symbols)

    data_days: dict[str, set[date]] = {}
    tz_suspect: dict[str, set[str]] = {}
    dup_risk: dict[str, set[str]] = {}
    coverage: dict[str, dict[tuple[date, date], tuple[int, int]]] = {}
    async with AsyncSessionLocal() as s:
        for sym in symbols:
            firsts = (await s.execute(_ARCHIVE_DAY_FIRSTS_SQL, {"symbol": sym})).all()
            arch = {r[0] for r in firsts}
            tz_suspect[sym] = {
                r[0].isoformat()
                for r in firsts
                if r[1] < time(9, 12) or r[1] > time(9, 20)
            }
            live = {r[0] for r in (await s.execute(_LIVE_DAYS_SQL, {"symbol": sym})).all()}
            data_days[sym] = arch | live
            known = {r[0] for r in (await s.execute(_STATS_DAYS_SQL, {"symbol": sym})).all()}
            dup_risk[sym] = {d.isoformat() for d in (live & arch) - known}
        refreshed = (await s.execute(_STATS_REFRESHED_SQL)).scalar()
    for sym in symbols:
        coverage[sym] = await _range_coverage(sym, from_date, to_date)

    report.tz_suspect_days = {k: sorted(v) for k, v in tz_suspect.items() if v}
    report.live_days_dup_risk = {k: sorted(v) for k, v in dup_risk.items() if v}
    report.default_exclusions = {k: sorted(v) for k, v in DEFAULT_EXCLUDED_DAYS.items()}
    if report.live_days_dup_risk:
        report.warnings.append(
            "oi_day_stats does not know some days present in BOTH tables "
            f"(last refreshed {refreshed.isoformat() if refreshed else 'never'}) — "
            "refresh the day index; affected days are skipped"
        )
    if report.tz_suspect_days and not allow_suspect_tz:
        report.warnings.append(
            "archive days with first bar outside 09:12–09:20 IST detected — "
            "verify scripts/repair_archive_tz.py was applied; affected days are skipped"
        )

    # Config-level warnings (legitimate scenario inputs, never blockers).
    if cfg.global_.master_kill:
        report.warnings.append("master_kill is ON in this config — the run will place zero trades")
    for wd, day_cfg in cfg.days.items():
        if day_cfg.day_kill:
            report.warnings.append(f"{wd}: day_kill is ON — no trades that weekday")

    # ── enumerate the plan, ascending ──
    d = from_date
    while d <= to_date:
        iso = d.isoformat()
        wd_index = d.weekday()
        if wd_index > 4:
            d += timedelta(days=1)
            continue
        weekday = _WEEKDAYS[wd_index]
        sym = day_symbols.get(weekday)
        plan = DayPlan(trade_date=iso, symbol=sym or "", expiry=None)
        if sym is None:
            plan.planned = False
            plan.skip_reason = f"no {weekday} config"
        elif symbol_filter and sym not in symbol_filter:
            plan.planned = False
            plan.skip_reason = f"symbol {sym} filtered out"
        elif iso in holidays:
            plan.planned = False
            plan.skip_reason = "config holiday"
        elif iso in platform_holidays:
            plan.planned = False
            plan.skip_reason = "platform holiday (nse_holidays.json)"
        elif iso in extra_excluded:
            plan.planned = False
            plan.skip_reason = "excluded by run settings"
        elif use_defaults and iso in DEFAULT_EXCLUDED_DAYS.get(sym, ()):
            plan.planned = False
            plan.skip_reason = "catalogued bad-data day (OI collapse)"
        elif d not in data_days.get(sym, set()):
            plan.planned = False
            plan.skip_reason = "no stored data"
        elif iso in dup_risk.get(sym, set()):
            plan.planned = False
            plan.skip_reason = "live/archive duplication risk (refresh live_days)"
        elif not allow_suspect_tz and iso in tz_suspect.get(sym, set()):
            plan.planned = False
            plan.skip_reason = "suspect timestamps (tz repair unverified)"
        else:
            exp = expiry_map.for_day(sym, d)
            if exp is None:
                plan.planned = False
                plan.skip_reason = "no stored expiry ≥ day"
            else:
                plan.expiry = exp.isoformat()
                minutes, spot_rows = coverage.get(sym, {}).get((d, exp), (0, 0))
                plan.minutes = minutes
                plan.expected_minutes = session_close_min(d) - SESSION_OPEN_MIN
                plan.coverage_pct = round(100.0 * minutes / plan.expected_minutes, 1) if plan.expected_minutes else 0.0
                plan.spotless = spot_rows == 0
                if plan.minutes < min_minutes:
                    plan.planned = False
                    plan.skip_reason = (
                        f"insufficient coverage ({plan.minutes} min < {min_minutes})"
                    )
        report.days.append(plan)
        d += timedelta(days=1)

    report.planned = sum(1 for p in report.days if p.planned)
    report.skipped = sum(1 for p in report.days if not p.planned)
    return report
