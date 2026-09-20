"""Automated market-data integrity report, per (symbol, IST day).

Ten categories, each ``{count, samples, detail}``. Everything is bounded to
ONE session (``[09:15, close)`` of that day, ``session_close_min``) so the
report is cheap enough to run after every session catch-up and on demand.
Nothing here repairs or synthesises data — it only names what is wrong so the
gap-fill / nightly pull can be pointed at it and so the operator can see it.

Categories
  1. missing_expected_intervals   session minutes with no CE/PE row in live ∪ archive
  2. duplicates                   archive∩live overlap the unified view would double-serve;
                                  restored (src=1) rows that also have a live row (invariant)
  3. out_of_order                 feed sequence gaps + late-bucket drops (arrival-time store)
  4. boundary_mismatch            restored→live handoff: OI / LTP jump at the seam
  5. incomplete_candles           archive bars with a NULL OHLC leg in a traded minute
  6. invalid_ohlc                 high < max(open, close), low > min(open, close), high < low, close <= 0
  7. missing_or_stale_oi          ATM±3 strikes: oi NULL/0, or unchanged for N session minutes
  8. tz_session_errors            first bar outside 09:12–09:20, bars past the close, pre-tz-fix rows
  9. unsupported_instruments      option_type outside CE/PE/IDX/FUT, non-1min archive spacing, strike<=0
 10. insufficient_warmup          UMP gate inputs for the day's active expiry (3 daily + weekly + 1H)

The pure classifiers (``ohlc_invalid``, ``stale_runs``, ``boundary_mismatch``,
``warmup_verdict``) are what the unit tests pin; the SQL only collects rows.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import structlog
from sqlalchemy import text

from ..core.config import settings
from ..core.db import AsyncSessionLocal
from ..core.holidays import is_nse_holiday
from ..core.time_utils import IST, SESSION_OPEN_MIN, ist_naive_to_utc, now_ist, session_close_min

log = structlog.get_logger(__name__)

CATEGORIES = (
    "missing_expected_intervals",
    "duplicates",
    "out_of_order",
    "boundary_mismatch",
    "incomplete_candles",
    "invalid_ohlc",
    "missing_or_stale_oi",
    "tz_session_errors",
    "unsupported_instruments",
    "insufficient_warmup",
)

_last_summary: dict[str, Any] = {}


def last_summary() -> dict[str, Any]:
    return _last_summary


# ------------------------------------------------------------ pure classifiers

def ohlc_invalid(o: Optional[float], h: Optional[float], l: Optional[float], c: Optional[float]) -> Optional[str]:
    """Reason string when an OHLC tuple is impossible, else None. NULL legs are
    'incomplete', not 'invalid' (category 5 handles those)."""
    if c is None or o is None or h is None or l is None:
        return None
    if c <= 0:
        return "close<=0"
    if h < l:
        return "high<low"
    if h < max(o, c):
        return "high<max(open,close)"
    if l > min(o, c):
        return "low>min(open,close)"
    return None


def stale_runs(series: list[tuple[int, Optional[int]]], min_len: int) -> list[tuple[int, int, int]]:
    """``series`` = [(minute_of_day, oi)] sorted by minute for ONE strike.
    Returns runs ``(start_min, end_min, length)`` where the OI value is
    unchanged for at least ``min_len`` consecutive minutes (missing minutes
    break a run). NULL/0 OI never forms a run — that is category 7's other
    half."""
    out: list[tuple[int, int, int]] = []
    if not series:
        return out
    run_start, prev_m, prev_v, n = None, None, None, 0
    for m, v in series:
        if v is None or v <= 0 or prev_m is None or m != prev_m + 1 or v != prev_v:
            if run_start is not None and n >= min_len:
                out.append((run_start, prev_m, n))
            run_start, n = (m, 1) if (v is not None and v > 0) else (None, 0)
        else:
            n += 1
        prev_m, prev_v = m, v
    if run_start is not None and n >= min_len:
        out.append((run_start, prev_m, n))
    return out


def boundary_mismatch(
    restored: tuple[int, Optional[float], Optional[int]],
    live: tuple[int, Optional[float], Optional[int]],
    *, oi_tol: float = 0.05, ltp_tol: float = 0.20, max_gap_min: int = 2,
) -> Optional[str]:
    """Compare the LAST restored minute ``(minute, ltp, oi)`` with the FIRST
    live minute after it for the same token. Reason or None."""
    rm, rl, ro = restored
    lm, ll, lo = live
    if lm - rm > max_gap_min:
        return f"seam gap {lm - rm} min"
    if ro and lo and abs(lo - ro) / ro > oi_tol:
        return f"oi jump {abs(lo - ro) / ro:.1%}"
    if rl and ll and abs(ll - rl) / rl > ltp_tol:
        return f"ltp jump {abs(ll - rl) / rl:.1%}"
    return None


def warmup_verdict(daily_days: int, weekly_days: int, h1_minutes: int) -> dict[str, Any]:
    """UMP data-ready gate inputs: ≥3 completed daily candles, ≥1 completed
    ISO week (≥5 prior session days), ≥1 completed 1H bar (≥60 minutes)."""
    ok_daily = daily_days >= 3
    ok_weekly = weekly_days >= 5
    ok_h1 = h1_minutes >= 60
    return {
        "ok": ok_daily and ok_weekly and ok_h1,
        "daily_days": daily_days, "need_daily": 3,
        "prior_session_days": weekly_days, "need_for_weekly": 5,
        "h1_minutes": h1_minutes, "need_h1_minutes": 60,
    }


def expected_minutes(day: date) -> list[int]:
    if day.weekday() >= 5 or is_nse_holiday(day):
        return []
    return list(range(SESSION_OPEN_MIN, session_close_min(day)))


def runs_of(mins: list[int]) -> list[list[Any]]:
    out: list[list[Any]] = []
    if not mins:
        return out
    s = p = mins[0]
    for m in mins[1:]:
        if m == p + 1:
            p = m
            continue
        out.append([f"{s // 60:02d}:{s % 60:02d}", f"{p // 60:02d}:{p % 60:02d}", p - s + 1])
        s = p = m
    out.append([f"{s // 60:02d}:{s % 60:02d}", f"{p // 60:02d}:{p % 60:02d}", p - s + 1])
    return out


# --------------------------------------------------------------------- SQL

_MINUTES_COVERED_SQL = text(
    """
    SELECT DISTINCT time_bucket('1 minute', ts) AS m
    FROM option_oi_snapshots
    WHERE symbol = :symbol AND option_type IN ('CE','PE')
      AND ts >= :open_utc AND ts < :close_utc
    UNION
    SELECT DISTINCT ts AS m
    FROM oi_archive_bars
    WHERE symbol = :symbol AND option_type IN ('CE','PE')
      AND ts >= :open_utc AND ts < :close_utc
    """
)

_DUP_RESTORED_INVARIANT_SQL = text(
    """
    SELECT count(*) FROM option_oi_snapshots r
    WHERE r.symbol = :symbol AND r.src = 1
      AND r.ts >= :open_utc AND r.ts < :close_utc
      AND EXISTS (
          SELECT 1 FROM option_oi_snapshots l
          WHERE l.token = r.token AND l.src IS NULL
            AND l.ts >= r.ts AND l.ts < r.ts + INTERVAL '1 minute'
      )
    """
)

_BOTH_ARMS_TODAY_SQL = text(
    """
    SELECT
      (SELECT count(DISTINCT time_bucket('1 minute', ts)) FROM option_oi_snapshots
        WHERE symbol = :symbol AND option_type IN ('CE','PE') AND src IS NULL
          AND ts >= :open_utc AND ts < :close_utc) AS live_minutes,
      (SELECT count(DISTINCT ts) FROM oi_archive_bars
        WHERE symbol = :symbol AND option_type IN ('CE','PE')
          AND ts >= :open_utc AND ts < :close_utc) AS arch_minutes,
      (SELECT winner FROM oi_day_stats WHERE symbol = :symbol AND day = :day) AS winner
    """
)

_RESTORED_SEAMS_SQL = text(
    """
    WITH r AS (
      SELECT token, max(ts) AS last_restored
      FROM option_oi_snapshots
      WHERE symbol = :symbol AND src = 1 AND ts >= :open_utc AND ts < :close_utc
      GROUP BY token
    ),
    rrow AS (
      SELECT s.token, s.ts, s.ltp, s.oi FROM option_oi_snapshots s
      JOIN r ON r.token = s.token AND r.last_restored = s.ts
      WHERE s.src = 1
    ),
    lrow AS (
      SELECT DISTINCT ON (s.token) s.token, s.ts, s.ltp, s.oi
      FROM option_oi_snapshots s
      JOIN r ON r.token = s.token
      WHERE s.src IS NULL AND s.ts > r.last_restored AND s.ts < :close_utc
      ORDER BY s.token, s.ts
    )
    SELECT rrow.token, rrow.ts AS r_ts, rrow.ltp AS r_ltp, rrow.oi AS r_oi,
           lrow.ts AS l_ts, lrow.ltp AS l_ltp, lrow.oi AS l_oi
    FROM rrow JOIN lrow ON lrow.token = rrow.token
    """
)

_ARCH_BARS_SQL = text(
    """
    SELECT token, ts, open, high, low, close, volume, source, strike, option_type
    FROM oi_archive_bars
    WHERE symbol = :symbol AND option_type IN ('CE','PE')
      AND ts >= :day_start AND ts < :day_end
    """
)

_ATM_STRIKES_OI_SQL = text(
    """
    WITH spot AS (
      SELECT underlying FROM option_oi_snapshots
      WHERE symbol = :symbol AND underlying IS NOT NULL
        AND ts >= :open_utc AND ts < :close_utc
      ORDER BY ts DESC LIMIT 1
    ),
    atm AS (SELECT round((SELECT underlying FROM spot) / :step) * :step AS k)
    SELECT strike, option_type, time_bucket('1 minute', ts) AS m, last(oi, ts) AS oi
    FROM option_oi_snapshots
    WHERE symbol = :symbol AND option_type IN ('CE','PE')
      AND ts >= :open_utc AND ts < :close_utc
      AND strike BETWEEN (SELECT k FROM atm) - 3 * :step AND (SELECT k FROM atm) + 3 * :step
    GROUP BY 1, 2, 3
    ORDER BY 1, 2, 3
    """
)

_FIRST_LAST_SQL = text(
    """
    SELECT min(ts) AS first_ts, max(ts) AS last_ts,
           count(*) FILTER (WHERE ts < :open_utc OR ts >= :close_utc) AS outside
    FROM (
      SELECT ts FROM option_oi_snapshots
      WHERE symbol = :symbol AND option_type IN ('CE','PE') AND ts >= :day_start AND ts < :day_end
      UNION ALL
      SELECT ts FROM oi_archive_bars
      WHERE symbol = :symbol AND option_type IN ('CE','PE') AND ts >= :day_start AND ts < :day_end
    ) t
    """
)

_UNSUPPORTED_SQL = text(
    """
    SELECT
      (SELECT count(*) FROM option_oi_snapshots
        WHERE symbol = :symbol AND ts >= :day_start AND ts < :day_end
          AND option_type NOT IN ('CE','PE')) AS live_bad_type,
      (SELECT count(*) FROM oi_archive_bars
        WHERE symbol = :symbol AND ts >= :day_start AND ts < :day_end
          AND option_type NOT IN ('CE','PE','IDX','FUT')) AS arch_bad_type,
      (SELECT count(*) FROM option_oi_snapshots
        WHERE symbol = :symbol AND ts >= :day_start AND ts < :day_end AND strike <= 0) AS live_bad_strike,
      (SELECT count(*) FROM oi_archive_bars
        WHERE symbol = :symbol AND option_type IN ('CE','PE')
          AND ts >= :day_start AND ts < :day_end AND strike <= 0) AS arch_bad_strike,
      (SELECT count(*) FROM oi_archive_bars
        WHERE symbol = :symbol AND ts >= :day_start AND ts < :day_end
          AND EXTRACT(SECOND FROM ts) <> 0) AS arch_non_minute
    """
)

_WARMUP_SQL = text(
    """
    WITH exp AS (
      SELECT min(expiry) AS e FROM (
        SELECT expiry FROM oi_archive_bars
         WHERE symbol = :symbol AND option_type IN ('CE','PE') AND expiry >= :day
        UNION
        SELECT expiry FROM option_oi_snapshots
         WHERE symbol = :symbol AND option_type IN ('CE','PE') AND expiry >= :day
           AND ts >= :day_start - INTERVAL '30 days'
      ) x
    )
    SELECT (SELECT e FROM exp) AS expiry,
           (SELECT count(DISTINCT (ts AT TIME ZONE 'Asia/Kolkata')::date)
              FROM oi_archive_bars
             WHERE symbol = :symbol AND option_type IN ('CE','PE')
               AND expiry = (SELECT e FROM exp)
               AND ts < :day_start) AS prior_days,
           (SELECT count(*) FROM eod_bars
             WHERE symbol = :symbol AND expiry = (SELECT e FROM exp)
               AND source = 'td_bhavcopy' AND trade_date < :day) AS bhav_rows,
           (SELECT count(DISTINCT m) FROM (
               SELECT time_bucket('1 minute', ts) AS m FROM option_oi_snapshots
                WHERE symbol = :symbol AND option_type IN ('CE','PE')
                  AND ts >= :open_utc AND ts < :close_utc
               UNION
               SELECT ts AS m FROM oi_archive_bars
                WHERE symbol = :symbol AND option_type IN ('CE','PE')
                  AND ts >= :open_utc AND ts < :close_utc
           ) u) AS today_minutes
    """
)

_PERSIST_SQL = text(
    """
    INSERT INTO data_integrity_runs (symbol, day, generated_at, report)
    VALUES (:symbol, :day, now(), CAST(:report AS JSONB))
    ON CONFLICT (symbol, day) DO UPDATE
      SET generated_at = now(), report = EXCLUDED.report
    """
)


def _cat(count: int, samples: list[Any], detail: Any = None) -> dict[str, Any]:
    return {"count": int(count), "samples": samples[:20], "detail": detail}


def _hm(ts: datetime) -> str:
    return ts.astimezone(IST).strftime("%H:%M")


async def day_report(symbol: str, day: date, *, persist: bool = True) -> dict[str, Any]:
    """Compute (and persist) the integrity report for one (symbol, IST day)."""
    symbol = symbol.upper()
    trading = day.weekday() < 5 and not is_nse_holiday(day)
    base = datetime.combine(day, datetime.min.time())
    day_start = ist_naive_to_utc(base)
    day_end = ist_naive_to_utc(base + timedelta(days=1))
    open_utc = ist_naive_to_utc(base + timedelta(minutes=SESSION_OPEN_MIN))
    close_min = session_close_min(day)
    close_utc = ist_naive_to_utc(base + timedelta(minutes=close_min))
    step = 100 if symbol == "SENSEX" else 50
    rep: dict[str, Any] = {
        "symbol": symbol, "day": day.isoformat(), "trading_day": trading,
        "session": {"open": "09:15", "close": f"{close_min // 60:02d}:{close_min % 60:02d}"},
        "generated_at": now_ist().isoformat(),
        "categories": {},
    }
    p = {"symbol": symbol, "open_utc": open_utc, "close_utc": close_utc,
         "day_start": day_start, "day_end": day_end, "day": day, "step": step}
    cats = rep["categories"]

    async with AsyncSessionLocal() as s:
        # 1. missing expected intervals
        exp = expected_minutes(day)
        covered: set[int] = set()
        for (m,) in (await s.execute(_MINUTES_COVERED_SQL, p)).all():
            if m is None:
                continue
            if getattr(m, "tzinfo", None) is None:
                m = m.replace(tzinfo=timezone.utc)
            mi = m.astimezone(IST)
            covered.add(mi.hour * 60 + mi.minute)
        if not trading:
            cats["missing_expected_intervals"] = _cat(0, [], "no-trading day (weekend/holiday)")
        else:
            missing = [m for m in exp if m not in covered]
            cats["missing_expected_intervals"] = _cat(
                len(missing), runs_of(missing),
                "whole_day_missing" if len(missing) == len(exp) and exp else
                {"expected": len(exp), "covered": len(exp) - len(missing)},
            )

        # 2. duplicates
        both = (await s.execute(_BOTH_ARMS_TODAY_SQL, p)).mappings().first() or {}
        inv = (await s.execute(_DUP_RESTORED_INVARIANT_SQL, p)).scalar() or 0
        live_m, arch_m = int(both.get("live_minutes") or 0), int(both.get("arch_minutes") or 0)
        winner = both.get("winner")
        double_served = live_m > 0 and arch_m > 0 and winner != "live"
        cats["duplicates"] = _cat(
            int(inv) + (arch_m if double_served else 0),
            [],
            {"restored_with_live_row": int(inv), "live_minutes": live_m, "arch_minutes": arch_m,
             "oi_day_stats_winner": winner,
             "unified_view_double_serves_day": double_served},
        )

        # 3. out of order — the live store keeps ARRIVAL time only (vendor time
        # is never persisted outside the shadow pipeline, which stays invisible
        # to production reads by design). What can be reported: the feed's own
        # per-contract sequence-gap counter for the current session, and the
        # aggregator's late-bucket drops.
        try:
            from ..runtime import get_runtime

            rt = get_runtime()
            feed = rt.feed_client
            agg = rt.aggregator
            live_today = day == now_ist().date()
            stats = feed.feed_stats() if (live_today and feed is not None and hasattr(feed, "feed_stats")) else {}
            seq_gaps = int(stats.get("seq_gaps") or 0) if stats else None
            late = int(getattr(agg, "late_dropped", 0) or 0) if (live_today and agg is not None) else None
            cats["out_of_order"] = _cat(
                (seq_gaps or 0) + (late or 0), [],
                {"seq_gaps_today": seq_gaps, "late_ticks_dropped_today": late,
                 "vendor_ts": "not stored in the live table (arrival-time stamping)"},
            )
        except Exception as e:  # noqa: BLE001
            cats["out_of_order"] = _cat(0, [], f"n/a: {e}")

        # 4. boundary mismatch (restored → live seams)
        seams = (await s.execute(_RESTORED_SEAMS_SQL, p)).mappings().all()
        bad: list[Any] = []
        for r in seams:
            r_ts, l_ts = r["r_ts"], r["l_ts"]
            rm = r_ts.astimezone(IST).hour * 60 + r_ts.astimezone(IST).minute
            lm = l_ts.astimezone(IST).hour * 60 + l_ts.astimezone(IST).minute
            why = boundary_mismatch(
                (rm, float(r["r_ltp"]) if r["r_ltp"] is not None else None, int(r["r_oi"] or 0)),
                (lm, float(r["l_ltp"]) if r["l_ltp"] is not None else None, int(r["l_oi"] or 0)),
            )
            if why:
                bad.append({"token": r["token"], "restored": _hm(r_ts), "live": _hm(l_ts), "why": why})
        cats["boundary_mismatch"] = _cat(len(bad), bad, {"seams_checked": len(seams)})

        # 5/6/9(partial). archive bars of the day
        bars = (await s.execute(_ARCH_BARS_SQL, p)).mappings().all()
        incomplete: list[Any] = []
        invalid: list[Any] = []
        legacy_tz = 0
        for b in bars:
            o, h, l, c = b["open"], b["high"], b["low"], b["close"]
            if any(v is None for v in (o, h, l, c)):
                incomplete.append({"token": b["token"], "ts": _hm(b["ts"])})
                continue
            why = ohlc_invalid(float(o), float(h), float(l), float(c))
            if why:
                invalid.append({"token": b["token"], "ts": _hm(b["ts"]), "why": why})
            if b["source"] == "td_getbars":
                legacy_tz += 1
        cats["incomplete_candles"] = _cat(len(incomplete), incomplete, {"archive_bars": len(bars)})
        cats["invalid_ohlc"] = _cat(len(invalid), invalid)

        # 7. missing / stale OI on ATM±3
        rows = (await s.execute(_ATM_STRIKES_OI_SQL, p)).mappings().all()
        by_strike: dict[tuple[int, str], list[tuple[int, Optional[int]]]] = {}
        zero_oi = 0
        for r in rows:
            m = r["m"]
            if getattr(m, "tzinfo", None) is None:
                m = m.replace(tzinfo=timezone.utc)
            mi = m.astimezone(IST)
            oi = int(r["oi"]) if r["oi"] is not None else None
            if not oi:
                zero_oi += 1
            by_strike.setdefault((int(r["strike"]), r["option_type"]), []).append((mi.hour * 60 + mi.minute, oi))
        stale: list[Any] = []
        for (k, ot), series in by_strike.items():
            for a, b_, n in stale_runs(series, int(settings.integrity_stale_oi_minutes)):
                stale.append({"strike": k, "type": ot, "from": f"{a // 60:02d}:{a % 60:02d}",
                              "to": f"{b_ // 60:02d}:{b_ % 60:02d}", "minutes": n})
        cats["missing_or_stale_oi"] = _cat(
            zero_oi + len(stale), stale, {"zero_or_null_oi_minutes": zero_oi, "strikes_checked": len(by_strike)},
        )

        # 8. tz / session errors
        fl = (await s.execute(_FIRST_LAST_SQL, p)).mappings().first() or {}
        tz_issues: list[Any] = []
        first_ts, last_ts = fl.get("first_ts"), fl.get("last_ts")
        if trading and first_ts is not None:
            fm = first_ts.astimezone(IST).hour * 60 + first_ts.astimezone(IST).minute
            if not (SESSION_OPEN_MIN - 3 <= fm <= SESSION_OPEN_MIN + 5):
                tz_issues.append({"first_bar": _hm(first_ts), "expected": "09:12–09:20"})
            lm = last_ts.astimezone(IST).hour * 60 + last_ts.astimezone(IST).minute
            if lm >= close_min:
                tz_issues.append({"last_bar": _hm(last_ts), "close": rep["session"]["close"]})
        outside = int(fl.get("outside") or 0)
        if outside:
            tz_issues.append({"rows_outside_session": outside})
        if legacy_tz:
            tz_issues.append({"archive_rows_pre_tz_fix": legacy_tz})
        cats["tz_session_errors"] = _cat(len(tz_issues), tz_issues)

        # 9. unsupported instruments / timeframes
        u = (await s.execute(_UNSUPPORTED_SQL, p)).mappings().first() or {}
        u_items = [{k: int(v)} for k, v in u.items() if v]
        cats["unsupported_instruments"] = _cat(sum(int(v or 0) for v in u.values()), u_items)

        # 10. warm-up
        w = (await s.execute(_WARMUP_SQL, p)).mappings().first() or {}
        prior_days = int(w.get("prior_days") or 0)
        verdict = warmup_verdict(
            daily_days=max(prior_days, int(w.get("bhav_rows") or 0) // 2 if prior_days == 0 else prior_days),
            weekly_days=prior_days,
            h1_minutes=int(w.get("today_minutes") or 0),
        )
        verdict["active_expiry"] = w.get("expiry").isoformat() if w.get("expiry") else None
        cats["insufficient_warmup"] = _cat(0 if verdict["ok"] else 1, [], verdict)

    rep["ok"] = all(c["count"] == 0 for c in cats.values())
    if persist:
        try:
            async with AsyncSessionLocal() as s:
                await s.execute(_PERSIST_SQL, {"symbol": symbol, "day": day, "report": json.dumps(rep, default=str)})
                await s.commit()
        except Exception as e:  # noqa: BLE001
            log.warning("data_integrity.persist_failed", error=str(e))
        try:
            out = Path(__file__).resolve().parents[3] / "logs" / "integrity"
            out.mkdir(parents=True, exist_ok=True)
            (out / f"{symbol}_{day.isoformat()}.json").write_text(json.dumps(rep, default=str, indent=1), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    return rep


async def run_integrity(symbols: list[str], days: list[date]) -> dict[str, Any]:
    """Batch: reports for every (symbol, day); updates ``last_summary``."""
    global _last_summary
    summary: dict[str, Any] = {"generated_at": now_ist().isoformat(), "reports": {}}
    for sym in symbols:
        for d in days:
            try:
                rep = await day_report(sym, d)
                summary["reports"][f"{sym}:{d.isoformat()}"] = {
                    "ok": rep["ok"],
                    "counts": {k: v["count"] for k, v in rep["categories"].items()},
                }
            except Exception as e:  # noqa: BLE001
                summary["reports"][f"{sym}:{d.isoformat()}"] = {"error": str(e)}
                log.warning("data_integrity.report_failed", symbol=sym, day=d.isoformat(), error=str(e))
    _last_summary = summary
    return summary
