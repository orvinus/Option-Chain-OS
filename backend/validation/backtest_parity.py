"""Backtest fidelity parity — the load-bearing check that the in-memory
day-frame math is EXACTLY the live SQL path.

Run (inside the backend container, DB required):
    cd /app && PYTHONPATH=. python validation/backtest_parity.py [YYYY-MM-DD ...]

For each test day (default: the three most recent archived NIFTY days) and a
grid of cursor minutes, compares:

  1. ``oi_change_pair_at``  vs ``series.build_oi_change_pair(from,to,now)``
  2. ``ratio_pair_at``      vs ``series.build_ratio_pair(from,to,now)``
     — timestamps, values, basket bounds and spot must be EXACTLY equal
     (both paths end in the same pure transform, so any diff is a day-frame
     assembly bug).
  3. ``pick_strike_in_band`` vs a direct SQL twin of the live
     ``_LATEST_LTP_PER_STRIKE_SQL`` anchored at the cursor (closed minutes).

Exit code 0 = full parity.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime, time, timedelta

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from sqlalchemy import text  # noqa: E402

from app.algo import series as ser  # noqa: E402
from app.algo.backtest.data import (  # noqa: E402
    load_day_frame,
    load_expiry_map,
    oi_change_pair_at,
    pick_strike_in_band,
    ratio_pair_at,
)
from app.core.db import AsyncSessionLocal  # noqa: E402
from app.core.time_utils import ist_naive_to_utc  # noqa: E402

CURSOR_MINUTES = [1, 5, 15, 30, 60, 90, 121, 180, 240, 300, 340, 374, 384]
ATM_WINDOWS = [10, -1]

_RECENT_DAYS_SQL = text(
    """
    SELECT DISTINCT (ts AT TIME ZONE 'Asia/Kolkata')::date AS d
    FROM oi_archive_bars
    WHERE symbol = 'NIFTY' AND option_type IN ('CE','PE')
    ORDER BY d DESC LIMIT 3
    """
)

# The live picker's SQL, re-anchored at a historical cursor over CLOSED
# minutes (the backtest's documented no-lookahead refinement means we compare
# candidate SETS via last closes, not the live forming-minute reads).
_HIST_PICK_SQL = text(
    """
    SELECT strike, last(ltp, ts) AS ltp
    FROM oi_snapshots_unified
    WHERE symbol = :symbol
      AND expiry = :expiry
      AND option_type = :option_type
      AND ltp IS NOT NULL
      AND ts >= :from_ts
      AND ts < :to_ts
    GROUP BY strike
    """
)

checks = 0
failures: list[str] = []


def check(ok: bool, label: str) -> None:
    global checks
    checks += 1
    if not ok:
        failures.append(label)
        print(f"FAIL  {label}")


async def parity_for_day(day: date) -> None:
    em = await load_expiry_map(["NIFTY"])
    expiry = em.for_day("NIFTY", day)
    if expiry is None:
        print(f"SKIP  {day}: no expiry ≥ day")
        return
    frame = await load_day_frame("NIFTY", expiry, day, 50)
    if frame is None:
        print(f"SKIP  {day}: no frame data")
        return
    open_ist = datetime.combine(day, time(9, 15))

    for m in CURSOR_MINUTES:
        cursor_ist = open_ist + timedelta(minutes=m)
        cursor_utc = ist_naive_to_utc(cursor_ist)
        # The closed-data window ends an ε before the cursor: on live-tick
        # days that keeps the FULL final closed minute (ticks run to :59.x);
        # on archive days it excludes the bar stamped exactly AT the cursor
        # (which aggregates the whole still-forming minute — pure future).
        closed_to = cursor_utc - timedelta(microseconds=1)
        from_utc = ist_naive_to_utc(open_ist)
        for w in ATM_WINDOWS:
            mine = oi_change_pair_at(frame, w, cursor_ist)
            live = await ser.build_oi_change_pair(
                "NIFTY", expiry, w,
                from_ts=from_utc, to_ts=closed_to, now=cursor_utc,
            )
            tag = f"{day} +{m}m w={w} oi_change"
            if mine is None or live is None:
                check(mine is None and live is None, f"{tag} presence {mine is None}/{live is None}")
            else:
                check(mine.strike_min == live.strike_min and mine.strike_max == live.strike_max,
                      f"{tag} basket {mine.strike_min}-{mine.strike_max} vs {live.strike_min}-{live.strike_max}")
                check(mine.spot == live.spot, f"{tag} spot {mine.spot} vs {live.spot}")
                check(mine.timestamps == live.timestamps,
                      f"{tag} timestamps len {len(mine.timestamps)} vs {len(live.timestamps)}")
                check(mine.call_change_cr == live.call_change_cr, f"{tag} call series")
                check(mine.put_change_cr == live.put_change_cr, f"{tag} put series")
                # Multi-TF's whole-unit copy (2026-09-23) must match exactly too.
                check(mine.call_change_full_cr == live.call_change_full_cr,
                      f"{tag} call series (whole units)")
                check(mine.put_change_full_cr == live.put_change_full_cr,
                      f"{tag} put series (whole units)")
                check(bool(mine.call_change_full_cr),
                      f"{tag} whole-unit series missing from the backtest day-frame")

            mine_r = ratio_pair_at(frame, w, cursor_ist)
            live_r = await ser.build_ratio_pair(
                "NIFTY", expiry, w,
                from_ts=from_utc, to_ts=closed_to, now=cursor_utc,
            )
            tag = f"{day} +{m}m w={w} ratio"
            if mine_r is None or live_r is None:
                check(mine_r is None and live_r is None,
                      f"{tag} presence {mine_r is None}/{live_r is None}")
            else:
                check(mine_r.timestamps == live_r.timestamps, f"{tag} timestamps")
                check(mine_r.green_pcr == live_r.green_pcr, f"{tag} green")
                check(mine_r.yellow_ratio == live_r.yellow_ratio, f"{tag} yellow")

    # Strike picker vs SQL twin at a few cursors.
    for m in (30, 121, 300):
        cursor_ist = open_ist + timedelta(minutes=m)
        last_closed_end = ist_naive_to_utc(cursor_ist.replace(second=0, microsecond=0))
        from_ts = last_closed_end - timedelta(minutes=15)
        async with AsyncSessionLocal() as s:
            rows = (
                await s.execute(
                    _HIST_PICK_SQL,
                    {"symbol": "NIFTY", "expiry": expiry, "option_type": "CE",
                     "from_ts": from_ts, "to_ts": last_closed_end},
                )
            ).mappings().all()
        band = (60.0, 180.0)
        mid = sum(band) / 2
        best = None
        best_d = None
        for r in rows:
            ltp = float(r["ltp"]) if r["ltp"] is not None else None
            if ltp is None or not (band[0] <= ltp <= band[1]):
                continue
            d = abs(ltp - mid)
            if best_d is None or d < best_d or (d == best_d and int(r["strike"]) < best[0]):
                best_d, best = d, (int(r["strike"]), ltp)
        mine_pick = pick_strike_in_band(frame, "CE", band[0], band[1], cursor_ist)
        check(mine_pick == best, f"{day} +{m}m strike pick {mine_pick} vs SQL {best}")


async def main() -> None:
    days = [date.fromisoformat(a) for a in sys.argv[1:]]
    if not days:
        async with AsyncSessionLocal() as s:
            days = [r[0] for r in (await s.execute(_RECENT_DAYS_SQL)).all()]
    for d in sorted(days):
        print(f"── {d} ──")
        await parity_for_day(d)
    print(f"\n{checks - len(failures)}/{checks} parity checks passed")
    if failures:
        raise SystemExit(f"{len(failures)} parity failure(s)")


if __name__ == "__main__":
    asyncio.run(main())
