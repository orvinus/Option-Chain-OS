"""OI interpretation engine.

Classifies each side (CE / PE) of a strike into one of:
    * Long Buildup    (OI ↑, LTP ↑)
    * Short Buildup   (OI ↑, LTP ↓)
    * Short Covering  (OI ↓, LTP ↑)
    * Long Unwinding  (OI ↓, LTP ↓)
    * Neutral         (no meaningful change)
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class Interpretation(str, Enum):
    LONG_BUILDUP = "long_buildup"
    SHORT_BUILDUP = "short_buildup"
    SHORT_COVERING = "short_covering"
    LONG_UNWINDING = "long_unwinding"
    NEUTRAL = "neutral"


# Thresholds: a change has to clear these absolute deltas to be classified.
OI_EPSILON = 1
LTP_EPSILON = 0.05


def classify(oi_change: int, ltp_change: float | None) -> Interpretation:
    if ltp_change is None:
        return Interpretation.NEUTRAL
    if abs(oi_change) < OI_EPSILON or abs(ltp_change) < LTP_EPSILON:
        return Interpretation.NEUTRAL
    if oi_change > 0 and ltp_change > 0:
        return Interpretation.LONG_BUILDUP
    if oi_change > 0 and ltp_change < 0:
        return Interpretation.SHORT_BUILDUP
    if oi_change < 0 and ltp_change > 0:
        return Interpretation.SHORT_COVERING
    return Interpretation.LONG_UNWINDING


@dataclass
class StrikeInterpretation:
    strike: int
    call: Interpretation
    put: Interpretation


def classify_rows(rows: Iterable) -> list[StrikeInterpretation]:
    """Classify each ``OIChangeRow`` (duck-typed)."""
    out: list[StrikeInterpretation] = []
    for r in rows:
        out.append(
            StrikeInterpretation(
                strike=r.strike,
                call=classify(r.call_oi_change, r.call_ltp_change),
                put=classify(r.put_oi_change, r.put_ltp_change),
            )
        )
    return out
