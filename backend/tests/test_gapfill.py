"""Gap-fill + same-day session catch-up (2026-09-09 rewrite).

Runnable without pytest:  PYTHONPATH=. python tests/test_gapfill.py
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingest import gapfill  # noqa: E402


def test_expected_trading_days_skips_weekends_and_holidays():
    hol = {date(2026, 10, 2)}  # Gandhi Jayanti — in data/nse_holidays.json
    orig = gapfill.is_nse_holiday
    gapfill.is_nse_holiday = lambda d: d in hol
    try:
        days = gapfill.expected_trading_days(date(2026, 9, 28), date(2026, 10, 5))
    finally:
        gapfill.is_nse_holiday = orig
    # Mon 28, Tue 29, Wed 30, Thu 1, (Fri 2 holiday), (Sat/Sun), Mon 5
    assert days == [date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 1), date(2026, 10, 5)]


def test_script_path_resolves_from_source_checkout():
    p = gapfill._script_path()
    assert p is None or p.name == "truedata_backfill.py"


def test_package_root_contains_app_package():
    """The subprocess PYTHONPATH must point at the directory holding ``app``
    (repo/backend or /app) — the import failure that killed every boot pull."""
    root = gapfill._package_root()
    assert (root / "app" / "__init__.py").exists(), root


def test_expected_session_minutes_grid_and_no_trading_days():
    orig = gapfill.is_nse_holiday
    gapfill.is_nse_holiday = lambda d: d == date(2026, 10, 2)
    try:
        full = gapfill.expected_session_minutes(date(2026, 9, 9))          # Wed, post 15:40 close
        assert full[0] == 9 * 60 + 15 and full[-1] == 15 * 60 + 39 and len(full) == 385
        old = gapfill.expected_session_minutes(date(2026, 7, 31))          # pre-change 15:30 close
        assert old[-1] == 15 * 60 + 29 and len(old) == 375
        capped = gapfill.expected_session_minutes(date(2026, 9, 9), upper_min=10 * 60)
        assert capped[-1] == 9 * 60 + 59
        assert gapfill.expected_session_minutes(date(2026, 9, 6)) == []     # Sunday
        assert gapfill.expected_session_minutes(date(2026, 10, 2)) == []    # holiday
    finally:
        gapfill.is_nse_holiday = orig


def test_missing_minutes_and_runs():
    exp = list(range(555, 565))
    covered = {555, 556, 560, 561, 562}
    miss = gapfill.missing_minutes(exp, covered)
    assert miss == [557, 558, 559, 563, 564]
    assert gapfill.minute_runs(miss) == [("09:17", "09:19", 3), ("09:23", "09:24", 2)]
    assert gapfill.minute_runs([]) == []


def test_historical_allowed_rule():
    orig = gapfill.is_nse_holiday
    gapfill.is_nse_holiday = lambda d: False
    try:
        in_session = datetime(2026, 9, 9, 11, 0)          # Wed 11:00 IST
        ok, until = gapfill.historical_allowed(in_session, usable_days_in_lookback=20)
        assert ok is False and until is not None and (until.hour, until.minute) == (16, 0)
        ok, until = gapfill.historical_allowed(in_session, usable_days_in_lookback=0)
        assert ok is True and until is None                 # fresh install: nothing to protect
        ok, _ = gapfill.historical_allowed(datetime(2026, 9, 9, 16, 5), 20)
        assert ok is True                                    # after close + delay
        ok, _ = gapfill.historical_allowed(datetime(2026, 9, 6, 11, 0), 20)
        assert ok is True                                    # Sunday
        ok, _ = gapfill.historical_allowed(datetime(2026, 9, 9, 8, 0), 20)
        assert ok is True                                    # pre-open
    finally:
        gapfill.is_nse_holiday = orig


def test_reset_ledger_sql_reopens_eod_and_error_units():
    sql = str(gapfill._RESET_LEDGER_SQL)
    assert "eod:td:" in sql
    assert "status = 'error'" in sql
    assert "IDX', 'FUT'" in sql


def test_request_catchup_sets_event():
    ev = asyncio.Event()
    gapfill._catchup_event = ev
    try:
        gapfill.request_catchup("feed_recovered")
        assert ev.is_set() and gapfill._catchup_reason == "feed_recovered"
    finally:
        gapfill._catchup_event = None
        gapfill._catchup_reason = ""


async def test_run_gapfill_once_marks_error_on_nonzero_rc():
    """A failing puller must NOT report status=ok (the silent-failure defect)."""
    calls: list[list[str]] = []

    async def fake_puller(symbol, script, *extra):
        calls.append(list(extra) or ["pull"])
        if not extra:
            return (1, ["Traceback", "ModuleNotFoundError: No module named 'app'"])
        return (0, [])

    async def fake_missing(symbol, lookback, min_minutes):
        return [date(2026, 9, 8)]

    async def fake_usable(symbol, lookback, min_minutes):
        return 20

    async def fake_reset(symbol, lookback):
        return 0

    async def fake_refresh():
        return {}

    saved = {
        "_run_puller": gapfill._run_puller, "missing_days": gapfill.missing_days,
        "usable_days_count": gapfill.usable_days_count, "_reset_recent_ledger": gapfill._reset_recent_ledger,
        "_script_path": gapfill._script_path, "notify": gapfill.notify, "now_ist": gapfill.now_ist,
        "is_nse_holiday": gapfill.is_nse_holiday,
        "_settlement_script_path": gapfill._settlement_script_path,
    }
    notes: list[str] = []
    gapfill._run_puller = fake_puller
    gapfill.missing_days = fake_missing
    gapfill.usable_days_count = fake_usable
    gapfill._reset_recent_ledger = fake_reset
    gapfill._script_path = lambda: Path("x/truedata_backfill.py")
    gapfill._settlement_script_path = lambda: Path("x/nse_settlement_backfill.py")
    gapfill.notify = lambda key, msg: notes.append(key)
    gapfill.now_ist = lambda: datetime(2026, 9, 9, 17, 0)   # after close -> historical allowed
    gapfill.is_nse_holiday = lambda d: False
    import app.services.data_health as dh
    orig_refresh = dh.refresh_data_health
    dh.refresh_data_health = fake_refresh
    creds = (gapfill.settings.truedata_user, gapfill.settings.truedata_password)
    object.__setattr__(gapfill.settings, "truedata_user", "u")
    object.__setattr__(gapfill.settings, "truedata_password", "p")
    try:
        rep = await gapfill.run_gapfill_once(reason="test")
    finally:
        for k, v in saved.items():
            setattr(gapfill, k, v)
        dh.refresh_data_health = orig_refresh
        object.__setattr__(gapfill.settings, "truedata_user", creds[0])
        object.__setattr__(gapfill.settings, "truedata_password", creds[1])
    assert rep["status"] == "error", rep
    assert any("rc=1" in e for e in rep["errors"]), rep["errors"]
    assert "gapfill_failed" in notes
    assert rep["symbols"]["NIFTY"]["tail"][-1].startswith("ModuleNotFoundError")
    # The public NSE settlement pass runs FIRST (no vendor credentials, and it
    # fills days a minute-gap scan cannot see), then the vendor 1-minute pull.
    assert calls[0][:2] == ["--symbol", "NIFTY"]
    assert ["pull"] in calls


async def test_session_catchup_reports_complete_when_nothing_missing():
    async def fake_missing(symbol, day, now):
        return []

    orig = gapfill.missing_session_minutes
    orig_hol = gapfill.is_nse_holiday
    gapfill.missing_session_minutes = fake_missing
    gapfill.is_nse_holiday = lambda d: False
    try:
        rep = await gapfill.session_catchup("NIFTY", reason="test")
    finally:
        gapfill.missing_session_minutes = orig
        gapfill.is_nse_holiday = orig_hol
    assert rep["status"] in ("complete", "not_trading_day", "before_open"), rep


if __name__ == "__main__":
    async def _main() -> int:
        passed = total = 0
        for k, v in sorted(globals().items()):
            if not k.startswith("test_"):
                continue
            total += 1
            try:
                r = v()
                if inspect.iscoroutine(r):
                    await r
                print("PASS", k)
                passed += 1
            except Exception as e:  # noqa: BLE001
                print("FAIL", k, type(e).__name__, e)
        print(f"{passed}/{total} passed")
        return 0 if passed == total else 1
    raise SystemExit(asyncio.run(_main()))
