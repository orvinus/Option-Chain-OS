"""Telegram alert TEXT builders for the trading engine (pure, DB-free).

Every operator-facing message the orchestrator sends is composed here so the
wording is testable and consistent: each carries the IST time it refers to,
the reason, and the money figures the operator asked for (2026-09-09):

- ENTRY: entry price, lots/qty, strike, scenario, time.
- EXIT: entry → exit price and %, exit reason, net P&L ₹ and % of allocated,
  fees, holding time.
- KILLS / PAUSE: the time, the reason, the realized figure vs the limit.

``now`` is always the orchestrator's EVALUATED minute (naive IST) — never the
wall clock — so backtest signal texts stay deterministic.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

EXIT_REASON_LABEL: dict[str, str] = {
    "MAX_SL": "Stop-Loss (Max cap)",
    "BASE_SL": "Stop-Loss (Base)",
    "TRAIL_EXIT": "Trailing Stop",
    "SB_EXIT": "Zone Trail (System B)",
    "TARGET": "Target hit",
    "END_EXIT": "End-of-day exit",
    "EXPIRY_FORCE_CLOSE": "Expiry force-close",
    "MASTER_KILL": "Master kill switch",
    "DAY_KILL": "Day kill switch",
    "ZONE_KILL": "Zone kill switch",
    "MANUAL_SQUARE_OFF": "Manual square-off",
}


def fmt_ist(dt: Optional[datetime]) -> str:
    """``09:31 IST`` — the human clock every alert carries."""
    if dt is None:
        return "--:-- IST"
    return f"{dt:%H:%M} IST"


def _inr(v: float, signed: bool = False) -> str:
    s = f"{abs(v):,.0f}"
    if signed:
        return f"{'+' if v >= 0 else '−'}₹{s}"
    return f"{'−' if v < 0 else ''}₹{s}"


def exit_reason_label(code: str) -> str:
    return EXIT_REASON_LABEL.get(code, code or "exit")


def entry_message(
    *,
    now: Optional[datetime],
    side: str,
    symbol: str,
    strike: int,
    option_type: str,
    expiry: Optional[str],
    fill: float,
    lots: int,
    lot_size: int,
    sub_scenario: str,
    day: str,
    zone: str,
    ledger: str,
    trade_id: int,
) -> str:
    qty = lots * lot_size
    notional = fill * qty
    return (
        f"▲ ENTRY {side} {symbol} {strike}{option_type} @ ₹{fill:.2f} × {lots} lot(s) "
        f"({qty} qty ≈ {_inr(notional)}) — {fmt_ist(now)} · {day} {zone} · {sub_scenario}"
        + (f" · exp {expiry}" if expiry else "")
        + f" · [{ledger}] · trade #{trade_id}"
    )


def exit_message(
    *,
    now: Optional[datetime],
    side: str,
    symbol: str,
    strike: int,
    option_type: str,
    entry_fill: float,
    exit_fill: float,
    exit_reason: str,
    pnl: float,
    pnl_pct: Optional[float],
    fees_total: Optional[float],
    held_min: Optional[int],
    day: str,
    zone: str,
    ledger: str,
    trade_id: int,
) -> str:
    move_pct = ((exit_fill / entry_fill) - 1) * 100 if entry_fill else 0.0
    parts = [
        f"▼ EXIT {side} {symbol} {strike}{option_type} @ ₹{exit_fill:.2f} — {fmt_ist(now)} — "
        f"{exit_reason_label(exit_reason)}",
        f"entry ₹{entry_fill:.2f} → exit ₹{exit_fill:.2f} ({move_pct:+.2f}%)",
        f"P&L {_inr(pnl, signed=True)}"
        + (f" ({pnl_pct:+.2f}% of allocated)" if pnl_pct is not None else ""),
    ]
    if fees_total is not None:
        parts.append(f"fees {_inr(fees_total)}")
    if held_min is not None:
        parts.append(f"held {held_min} min")
    parts.append(f"[{day} {zone}, {ledger}] · trade #{trade_id}")
    return " · ".join(parts)


def risk_kill_message(
    kind: Literal["max_loss", "profit_lock"],
    *,
    now: Optional[datetime],
    realized: float,
    limit: float,
    pct: float,
    allocated: float,
) -> str:
    if kind == "max_loss":
        return (
            f"🛑 KILL {fmt_ist(now)} — Max daily loss breached: realized "
            f"{_inr(realized, signed=True)} vs limit {_inr(-abs(limit), signed=True)} "
            f"({pct:g}% of {_inr(allocated)} allocated). No further entries today."
        )
    return (
        f"✅ KILL {fmt_ist(now)} — Profit lock reached: realized "
        f"{_inr(realized, signed=True)} vs lock {_inr(abs(limit), signed=True)} "
        f"({pct:g}% of {_inr(allocated)}). No further entries today."
    )


def pause_message(
    *,
    now: Optional[datetime],
    streak: int,
    streak_loss: float,
    cap_trades: int,
    cap_pct: float,
) -> str:
    return (
        f"⏸ PAUSE {fmt_ist(now)} — Engine auto-paused: {streak} consecutive losses "
        f"(−₹{streak_loss:,.0f}; caps {cap_trades} trades / {cap_pct:g}% of allocated). "
        "Manual Resume required."
    )


def kill_message(
    *,
    now: Optional[datetime],
    scope: Literal["day", "zone"],
    reason: str,
    day: str,
    zone: str = "",
) -> str:
    if scope == "day":
        return (
            f"🛑 {fmt_ist(now)} — Trading is OFF today: {reason}. "
            "No zones will be evaluated."
        )
    return (
        f"🛑 {fmt_ist(now)} — {day} {zone} is killed ({reason}) — "
        "this window will not trade today."
    )
