"""The 15:45 IST daily Telegram summary (pure builder + pure scheduler).

Sent once per trading day at/after 15:45 IST — restart-safe (the sent
marker is an audit row the loop checks at boot) — and gated by the day's
``telegram_trade_entry_exit`` toggle like every other trade alert.
"""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Optional

try:  # shared helper (also used by the backtest summary and /pnl/summary)
    from .drawdown import scan_drawdown
except ImportError:  # pragma: no cover — helper lands with §7
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class _DD:
        max_dd: float
        max_dd_pct: float
        from_key: Optional[str]
        to_key: Optional[str]

    def scan_drawdown(points, *, starting):  # type: ignore[misc]
        peak = starting
        max_dd = 0.0
        max_pct = 0.0
        pk = None
        f = t = None
        for k, eq in points:
            if eq > peak:
                peak, pk = eq, k
            dd = peak - eq
            if dd > max_dd:
                max_dd = dd
                max_pct = round(dd / peak * 100, 2) if peak > 0 else 0.0
                f, t = pk, k
        return _DD(max_dd, max_pct, f, t)

SUMMARY_TIME = time(15, 45)
SUMMARY_EVENT = "runtime_daily_summary"


def summary_due(now: datetime, sent_for: Optional[date], is_holiday: bool) -> bool:
    """True when the summary for ``now.date()`` should go out now."""
    if now.weekday() >= 5 or is_holiday:
        return False
    if now.time() < SUMMARY_TIME:
        return False
    return sent_for != now.date()


def _inr(v: float, signed: bool = True) -> str:
    s = f"{abs(v):,.0f}"
    if signed:
        return f"{'+' if v >= 0 else '−'}₹{s}"
    return f"{'−' if v < 0 else ''}₹{s}"


def build_daily_summary(
    *,
    day: date,
    ledger: str,
    closed: list[dict[str, Any]],
    allocated: float,
    kill_events: list[tuple[datetime, str]],
    open_position: Optional[dict[str, Any]],
    now: datetime,
) -> str:
    """``closed`` rows: trade_store.closed_on() dicts — need ``pnl_rupees``,
    ``zone_id``, ``side``, ``strike``, ``option_type``, ``exit_ts``,
    ``fees`` (dict with ``total``) and ``exit_reason``."""
    n = len(closed)
    pnls = [float(r.get("pnl_rupees") or 0.0) for r in closed]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p <= 0) if n else 0
    net = sum(pnls)
    fees = sum(float((r.get("fees") or {}).get("total") or 0.0) for r in closed)
    gross = net + fees
    win_rate = round(wins / n * 100) if n else 0
    lines = [
        f"📋 Daily summary {day.isoformat()} ({day:%A}) [{ledger}] — sent {now:%H:%M} IST",
        (
            f"Trades {n} · {wins}W/{losses}L · win {win_rate}%"
            if n
            else "Trades 0 — no closed trades today"
        ),
    ]
    if n:
        pct = f" ({net / allocated * 100:+.1f}% of allocated ₹{allocated:,.0f})" if allocated > 0 else ""
        lines.append(f"Gross {_inr(gross)} · fees ₹{fees:,.0f} · Net {_inr(net)}{pct}")
        by_zone: dict[str, list[float]] = {}
        for r, p in zip(closed, pnls):
            by_zone.setdefault(str(r.get("zone_id") or "?"), []).append(p)
        lines.append(
            "Zones: "
            + " · ".join(f"{z} {_inr(sum(v))} ({len(v)})" for z, v in sorted(by_zone.items()))
        )
        best_i = max(range(n), key=lambda i: pnls[i])
        worst_i = min(range(n), key=lambda i: pnls[i])

        def _c(r: dict[str, Any]) -> str:
            return f"{r.get('side', '')} {r.get('strike', '')}{r.get('option_type', '')}".strip()

        lines.append(f"Best {_inr(pnls[best_i])} {_c(closed[best_i])} · Worst {_inr(pnls[worst_i])} {_c(closed[worst_i])}")
        points = [(str(r.get("exit_ts") or i), 0.0) for i, r in enumerate(closed)]
        eq = 0.0
        pts: list[tuple[str, float]] = []
        for (k, _), p in zip(points, pnls):
            eq += p
            pts.append((k, eq))
        dd = scan_drawdown(pts, starting=0.0)
        if dd.max_dd > 0:
            pct_dd = f" ({dd.max_dd / allocated * 100:.1f}%)" if allocated > 0 else ""
            span = ""
            if dd.from_key and dd.to_key:
                span = f" {str(dd.from_key)[11:16]}→{str(dd.to_key)[11:16]}"
            lines.append(f"Max intraday drawdown −₹{dd.max_dd:,.0f}{pct_dd}{span}")
        else:
            lines.append("Max intraday drawdown: none")
    if kill_events:
        lines.append("Kills: " + "; ".join(f"{t:%H:%M} {r}" for t, r in kill_events))
    else:
        lines.append("Kills: none")
    if open_position:
        op = open_position
        unreal = op.get("unrealized_gross")
        lines.append(
            f"Open/carried: {op.get('side', '')} {op.get('strike', '')}{op.get('option_type', '')} × "
            f"{op.get('lots', '')} @ ₹{float(op.get('entry_fill') or 0):.2f}"
            + (f", unrealized {_inr(float(unreal))}" if unreal is not None else "")
        )
    else:
        lines.append("Open/carried: none")
    return "\n".join(lines)
