"""Peak-to-trough maximum drawdown over an ordered equity series (§7).

One pure helper shared by the backtest run summary (day-close equity curve),
the live/paper P&L summary (ledger balance + cumulative closed P&L, one point
per exit) and the daily Telegram summary (intraday, starting at 0).

The scan reproduces the ORIGINAL ``backtest/store.summarize`` loop exactly —
peak starts at ``starting``, the peak key stays ``None`` until the equity
first exceeds it, drawdown = peak − equity, only a STRICTLY deeper drawdown
updates the record, and the percentage is of the peak at the time of the
trough (``0.0`` when that peak is not positive). Run #118's numbers are the
byte-identical regression fixture for this.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class Drawdown:
    max_dd: float                 # rupees, ≥ 0 (unrounded — callers round)
    max_dd_pct: float             # % of the peak equity, rounded to 2 dp
    from_key: Optional[str]       # key of the peak (None = never exceeded ``starting``)
    to_key: Optional[str]         # key of the trough (None = no drawdown)


def scan_drawdown(points: Iterable[tuple[str, float]], *, starting: float) -> Drawdown:
    """``points`` = ordered ``(key, equity)`` pairs — a date, an exit
    timestamp, anything sortable-by-construction; the keys are only echoed
    back as ``from_key`` / ``to_key``."""
    peak = starting
    max_dd = 0.0
    max_dd_pct = 0.0
    dd_from: Optional[str] = None
    dd_to: Optional[str] = None
    peak_key: Optional[str] = None
    for key, equity in points:
        if equity > peak:
            peak = equity
            peak_key = key
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = round(dd / peak * 100, 2) if peak > 0 else 0.0
            dd_from = peak_key
            dd_to = key
    return Drawdown(max_dd=max_dd, max_dd_pct=max_dd_pct, from_key=dd_from, to_key=dd_to)


def equity_points_from_pnl(
    closes: Iterable[tuple[str, float]], *, starting: float
) -> list[tuple[str, float]]:
    """Cumulative equity after each closed trade: ``starting`` + running sum
    of the (key, pnl) pairs, in the given order."""
    out: list[tuple[str, float]] = []
    equity = float(starting)
    for key, pnl in closes:
        equity += float(pnl or 0.0)
        out.append((key, equity))
    return out
