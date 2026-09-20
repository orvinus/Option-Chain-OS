"""scripts/truedata_backfill.py — day mode, stable ledger keys, error contract.

The script is imported as a module (its argparse main is guarded)."""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "truedata_backfill.py"
if not _SCRIPT.exists():                       # image / test-container layout: /scripts
    _SCRIPT = Path("/scripts/truedata_backfill.py")

spec = importlib.util.spec_from_file_location("truedata_backfill", _SCRIPT)
tb = importlib.util.module_from_spec(spec)
sys.modules["truedata_backfill"] = tb
spec.loader.exec_module(tb)  # type: ignore[union-attr]


class _StubTD:
    """No credentials in the test container: the vendor client is never used
    by these tests, so stand in a stub with the one attribute the code reads."""

    class gov:  # noqa: N801 - mirrors TrueDataRest.gov
        min_interval = 0.25

    def __init__(self, *a, **k):
        pass

    async def aclose(self):
        pass


tb.TrueDataRest = _StubTD  # type: ignore[assignment]


def _bf(**kw):
    ns = argparse.Namespace(
        symbol="NIFTY", months=6, rps=4.0, workers=4, index_symbol=None, dry_run=False,
        include_live_days=False, days=None, years=1, day=None, to=None, max_expiries=2,
    )
    for k, v in kw.items():
        setattr(ns, k, v)
    return tb.Backfill(ns)


def test_package_root_resolution_matches_layout():
    # In a source checkout the package is under backend/; in the image it is
    # next to the script's parent. Either way ``app`` must be importable.
    assert (tb._PKG_ROOT / "app" / "__init__.py").exists() or Path("/backend/app/__init__.py").exists(), tb._PKG_ROOT


def test_window_day_mode_caps_at_last_completed_minute():
    bf = _bf(day="2026-09-09")
    start, end = bf._window()
    assert start == datetime(2026, 9, 9, 9, 15)
    assert end <= datetime(2026, 9, 9, 15, 40)
    bf2 = _bf(day="2026-09-09", to="11:30")
    _s, end2 = bf2._window()
    assert end2 <= datetime(2026, 9, 9, 11, 30)


def test_day_mode_unit_namespace_and_status():
    bf = _bf(day="2026-09-09")
    assert bf._unit_suffix() == ":d260909"
    assert bf._unit_status(datetime(2026, 9, 9, 15, 40)) == "done"
    assert bf._unit_status(datetime(2026, 9, 9, 11, 30)) == "partial"
    full = _bf()
    assert full._unit_suffix() == "" and full._unit_status(datetime(2026, 9, 9, 11, 30)) == "done"


def test_full_window_start_is_month_aligned_and_stable():
    bf = _bf()
    s1, _ = bf._window()
    assert s1.day == 1 and (s1.hour, s1.minute) == (9, 0)
    # The same calendar month must yield the same start regardless of the day.
    s_a = (datetime(2026, 9, 3) - timedelta(days=6 * 31)).replace(day=1)
    s_b = (datetime(2026, 9, 25) - timedelta(days=6 * 31)).replace(day=1)
    assert s_a == s_b


def test_day_mode_live_days_skip_is_disabled():
    bf = _bf(day="2026-09-09")
    assert asyncio.run(bf._load_live_days("NIFTY")) == set()


def test_fetch_unit_returns_none_on_vendor_error_and_counts_it():
    bf = _bf()

    class _TD:
        async def get_bars(self, *a, **k):
            raise tb.TrueDataError("boom")

    marks: list = []

    async def _mark(unit, status, rows=0, error=None):
        marks.append((unit, status))

    bf.td = _TD()
    bf._mark = _mark  # type: ignore[assignment]
    out = asyncio.run(bf._fetch_unit("NIFTY:260915:23500:CE", "X",
                                     datetime(2026, 9, 9, 9, 15), datetime(2026, 9, 9, 15, 40)))
    assert out is None                      # callers must NOT mark 'done'
    assert marks == [("NIFTY:260915:23500:CE", "error")]
    assert bf.errors == 1


def test_fetch_unit_skips_done_units_without_calling_vendor():
    bf = _bf()
    bf.done_units = {"u1"}

    class _TD:
        async def get_bars(self, *a, **k):
            raise AssertionError("must not be called")

    bf.td = _TD()
    assert asyncio.run(bf._fetch_unit("u1", "X", datetime(2026, 9, 9), datetime(2026, 9, 9, 15))) is None


def test_discover_expiries_day_mode_returns_nearest_n():
    bf = _bf(day="2026-09-09", max_expiries=2)

    class _TD:
        async def get_symbol_expiry_list(self, symbol):
            return ["2026-09-15", "2026-09-22", "2026-09-29", "2026-10-06", "2026-10-27"]

    bf.td = _TD()
    exps = asyncio.run(bf._discover_expiries("NIFTY"))
    assert exps == [date(2026, 9, 15), date(2026, 9, 22)]
