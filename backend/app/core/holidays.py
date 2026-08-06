"""NSE trading-holiday calendar (file-backed, deliberately conservative).

``is_nse_regular_session_open()`` knows nothing about holidays, so the strict
health probe would report "stale data during session hours" for ~6.5 hours on
every NSE holiday — driving the VPS sentinel to its restart cap and paging the
operator for a closed market. This module lets strict answer "holiday" instead.

The failure asymmetry matters: a date WRONGLY listed here silences real-outage
alerting for a whole trading day, while a missing date merely causes a few
capped, clearly-worded false pages. So the shipped file contains only dates that
are certain, and the operator extends it from the NSE circular (the oi-doctor
script warns when the file looks under-maintained). Missing file = no holidays.
"""
from __future__ import annotations

import json
from datetime import date
from functools import lru_cache

from .config import PROJECT_ROOT
from .logging import get_logger

log = get_logger("holidays")

HOLIDAY_FILE = PROJECT_ROOT / "data" / "nse_holidays.json"


@lru_cache(maxsize=1)
def _load() -> frozenset[str]:
    try:
        raw = json.loads(HOLIDAY_FILE.read_text(encoding="utf-8"))
        days = raw.get("holidays", [])
        return frozenset(str(d) for d in days)
    except FileNotFoundError:
        return frozenset()
    except Exception as e:  # malformed file must never break health
        log.warning("holidays.load_failed", error=str(e))
        return frozenset()


def is_nse_holiday(d: date) -> bool:
    return d.isoformat() in _load()


def known_holidays() -> frozenset[str]:
    return _load()
