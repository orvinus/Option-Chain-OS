"""TrueData hold-last refresher window = the configured session on trading days.

The old literal 15:35 end dropped the final five minutes of every session for
every strike that did not trade; the missing holiday check manufactured rows
all day on a weekday NSE holiday."""
from __future__ import annotations

from datetime import datetime

import pytz

from app.ingest.truedata_feed import TrueDataFeedClient

IST = pytz.timezone("Asia/Kolkata")


def _client() -> TrueDataFeedClient:
    c = TrueDataFeedClient.__new__(TrueDataFeedClient)
    c._active_symbol = "NIFTY"
    return c


def _at(y, mo, d, hh, mm) -> datetime:
    return IST.localize(datetime(y, mo, d, hh, mm))


def test_window_reaches_1539_and_stops_at_close():
    c = _client()
    assert c._in_session_window(_at(2026, 9, 9, 15, 36)) is True     # was False under the 15:35 literal
    assert c._in_session_window(_at(2026, 9, 9, 15, 39)) is True
    assert c._in_session_window(_at(2026, 9, 9, 15, 40)) is False    # close is exclusive
    assert c._in_session_window(_at(2026, 9, 9, 9, 14)) is False
    assert c._in_session_window(_at(2026, 9, 9, 9, 15)) is True
    # pre-change date keeps the 15:30 close
    assert c._in_session_window(_at(2026, 7, 31, 15, 31)) is False


def test_window_closed_on_weekends_and_nse_holidays(monkeypatch):
    c = _client()
    assert c._in_session_window(_at(2026, 9, 6, 11, 0)) is False       # Sunday
    import app.core.holidays as hol
    monkeypatch.setattr(hol, "is_nse_holiday", lambda d: True)
    assert c._in_session_window(_at(2026, 9, 9, 11, 0)) is False       # weekday holiday
