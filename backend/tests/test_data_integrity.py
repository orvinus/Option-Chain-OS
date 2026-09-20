"""Pure classifiers behind the data-integrity report (services/data_integrity.py)."""
from __future__ import annotations

from datetime import date

from app.services import data_integrity as di


def test_categories_are_the_ten_required_ones():
    assert len(di.CATEGORIES) == 10
    for c in ("missing_expected_intervals", "duplicates", "out_of_order", "boundary_mismatch",
              "incomplete_candles", "invalid_ohlc", "missing_or_stale_oi", "tz_session_errors",
              "unsupported_instruments", "insufficient_warmup"):
        assert c in di.CATEGORIES


def test_ohlc_invalid_reasons():
    assert di.ohlc_invalid(100, 101, 99, 100.5) is None
    assert di.ohlc_invalid(100, 99.5, 99, 100.5) == "high<max(open,close)"
    assert di.ohlc_invalid(100, 101, 100.2, 100.5) == "low>min(open,close)"
    assert di.ohlc_invalid(100, 98, 99, 97) == "high<low"
    assert di.ohlc_invalid(100, 101, 99, 0) == "close<=0"
    assert di.ohlc_invalid(None, 101, 99, 100) is None      # incomplete, not invalid


def test_stale_runs_detects_flat_oi_and_breaks_on_gaps():
    series = [(555 + i, 1000) for i in range(10)]           # 10 flat minutes
    assert di.stale_runs(series, 10) == [(555, 564, 10)]
    assert di.stale_runs(series, 11) == []
    series2 = [(555, 1), (556, 1), (557, 1), (559, 1), (560, 1), (561, 1)]   # gap at 558
    assert di.stale_runs(series2, 3) == [(555, 557, 3), (559, 561, 3)]
    series3 = [(555, 0), (556, 0), (557, 0)]                # zero OI never a run
    assert di.stale_runs(series3, 2) == []
    series4 = [(555, 5), (556, 6), (557, 6), (558, 6), (559, 7)]
    assert di.stale_runs(series4, 3) == [(556, 558, 3)]


def test_boundary_mismatch_rules():
    assert di.boundary_mismatch((600, 100.0, 10000), (601, 101.0, 10100)) is None
    assert di.boundary_mismatch((600, 100.0, 10000), (605, 101.0, 10100)) == "seam gap 5 min"
    assert di.boundary_mismatch((600, 100.0, 10000), (601, 101.0, 12000)).startswith("oi jump")
    assert di.boundary_mismatch((600, 100.0, 10000), (601, 130.0, 10000)).startswith("ltp jump")


def test_warmup_verdict_thresholds():
    v = di.warmup_verdict(daily_days=3, weekly_days=5, h1_minutes=60)
    assert v["ok"] is True
    assert di.warmup_verdict(2, 5, 60)["ok"] is False
    assert di.warmup_verdict(3, 4, 60)["ok"] is False
    assert di.warmup_verdict(3, 5, 59)["ok"] is False


def test_expected_minutes_respects_session_close_change_and_weekends():
    assert len(di.expected_minutes(date(2026, 9, 9))) == 385
    assert len(di.expected_minutes(date(2026, 7, 31))) == 375
    assert di.expected_minutes(date(2026, 9, 6)) == []


def test_runs_of_formats_hhmm():
    assert di.runs_of([555, 556, 557, 600]) == [["09:15", "09:17", 3], ["10:00", "10:00", 1]]
