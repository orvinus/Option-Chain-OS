"""The Charts / Ratio series must not dip on its last point (2026-09-14).

`time_bucket_gapfill` treats its finish bound as EXCLUSIVE while the row filter is
`ts <= :to_ts`. The window end was clamped with `min(to_ts, real_end + 1us)`, which
returns `to_ts` itself whenever a caller asks for exactly the last tick's instant —
every historical day requested to 15:40:00, every custom range ending on a whole
minute. Rows stamped at that instant then opened a bucket outside the gapfill
range with no locf carry, and the final point summed only the legs that ticked in
that exact second: NIFTY 2026-09-11 to 15:40 read Call OI 74,697,350 against the
true 85,639,255.

The fix keeps the inclusive bound for the row filter and passes a separate
`gap_to = to_ts + 1us` to gapfill.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

from app.services import oi_timeseries as ts_mod

EXPIRY = date(2026, 9, 15)
LAST_TICK = datetime(2026, 9, 11, 10, 10, tzinfo=timezone.utc)  # 15:40:00 IST


class _Result:
    def __init__(self, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._scalar


class _Session:
    def __init__(self, seen):
        self.seen = seen

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params):
        if sql is ts_mod._MAX_TS_SQL:
            return _Result(rows=[{"max_ts": LAST_TICK}])
        if sql is ts_mod._MAX_TS_IN_WINDOW_SQL:
            return _Result(scalar=LAST_TICK)
        if sql is ts_mod._TIMESERIES_SQL:
            self.seen["series"] = params
            return _Result(rows=[])
        if sql is ts_mod._STRIKE_DELTAS_SQL:
            self.seen["deltas"] = params
            return _Result(rows=[])
        raise AssertionError(f"unexpected query: {str(sql)[:80]}")


async def _with_fake(coro_factory):
    seen: dict = {}
    orig = ts_mod.AsyncSessionLocal
    ts_mod.AsyncSessionLocal = lambda: _Session(seen)  # type: ignore[assignment]
    try:
        await coro_factory()
    finally:
        ts_mod.AsyncSessionLocal = orig  # type: ignore[assignment]
    return seen


def test_gapfill_sql_uses_the_separate_finish_bound() -> None:
    for name in ("_TIMESERIES_SQL", "_STRIKE_DELTAS_SQL"):
        sql = str(getattr(ts_mod, name))
        assert ":gap_to) AS bucket" in sql, f"{name}: gapfill must finish at :gap_to"
        assert "ts <= :to_ts" in sql, f"{name}: the row filter stays inclusive"


async def test_series_end_exactly_on_last_tick_keeps_final_bucket_complete() -> None:
    seen = await _with_fake(lambda: ts_mod.fetch_oi_timeseries(
        "NIFTY", EXPIRY, 22900, 23900, "1m",
        from_ts=LAST_TICK - timedelta(hours=6, minutes=25), to_ts=LAST_TICK,
    ))
    p = seen["series"]
    assert p["to_ts"] == LAST_TICK, p["to_ts"]
    assert p["gap_to"] == LAST_TICK + timedelta(microseconds=1), (
        "gapfill must finish strictly AFTER the last included tick; finishing AT it "
        "drops the final bucket's locf carry and the last point dips"
    )


async def test_strike_deltas_end_exactly_on_last_tick() -> None:
    seen = await _with_fake(lambda: ts_mod.fetch_oi_strike_deltas(
        "NIFTY", EXPIRY, 22900, 23900,
        from_ts=LAST_TICK - timedelta(hours=6, minutes=25), to_ts=LAST_TICK,
    ))
    p = seen["deltas"]
    assert p["to_ts"] == LAST_TICK and p["gap_to"] == LAST_TICK + timedelta(microseconds=1), p


async def test_caller_end_after_last_tick_still_clamps_to_it() -> None:
    """Unchanged behaviour: a window past the data stops at the data, not at to_ts."""
    seen = await _with_fake(lambda: ts_mod.fetch_oi_timeseries(
        "NIFTY", EXPIRY, 22900, 23900, "1m",
        from_ts=LAST_TICK - timedelta(hours=6), to_ts=LAST_TICK + timedelta(hours=2),
    ))
    p = seen["series"]
    assert p["to_ts"] == LAST_TICK and p["gap_to"] == LAST_TICK + timedelta(microseconds=1), p


if __name__ == "__main__":
    test_gapfill_sql_uses_the_separate_finish_bound()
    for t in (test_series_end_exactly_on_last_tick_keeps_final_bucket_complete,
              test_strike_deltas_end_exactly_on_last_tick,
              test_caller_end_after_last_tick_still_clamps_to_it):
        asyncio.run(t())
    print("ok")
