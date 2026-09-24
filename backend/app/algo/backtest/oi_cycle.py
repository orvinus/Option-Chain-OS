"""One-day OI-integrated simulation for the Ultra Master Pro chart (2026-09-25).

The UMP chart's own replay (``/api/algo/engines/ump``) runs an INDEPENDENT
engine over the contract's whole life — it never waits for an OI signal, so it
draws entries the platform would never take (e.g. 13:18 when the OI signal
came at 13:20). This module answers "what does the OI-integrated platform do
on this day?" with the SAME code live trading and the backtest run: a fresh
``ZoneOrchestrator`` over ``BacktestDeps`` (the backtest's day frame, fills and
warm-ups), stepped minute by minute. Every OI signal / direction change starts
a fresh UMP entry calculation (``UmpEngine.start_entry_cycle``), the premium
band picks the contract, and one position is held at a time.

Nothing is written to the database. ``up_to`` stops the simulation at a
closed minute (today's chart must never see the future).
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta
from typing import Any, Optional

import structlog

from ...core.holidays import known_holidays
from ...core.time_utils import session_close_min
from ..config_models import AlgoConfig
from ..orchestrator import ZoneOrchestrator
from .data import load_day_frame, load_expiry_map
from .deps import (
    BacktestDeps,
    EventCollector,
    InMemoryLedger,
    prefetch_official_closes,
    prefetch_premium_minutes,
)

log = structlog.get_logger(__name__)

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")
_FIRST_MINUTE = time(9, 15)


def _segments(minutes: list[tuple[str, str, str]]) -> list[dict[str, Any]]:
    """[(HH:MM, direction, zone)] per minute → contiguous CALL/PUT windows."""
    out: list[dict[str, Any]] = []
    for hm, direction, zone in minutes:
        if direction not in ("CALL", "PUT"):
            continue
        last = out[-1] if out else None
        if last and last["direction"] == direction and last["zone"] == zone and last["_next"] == hm:
            last["end"] = hm
        else:
            out.append({"direction": direction, "zone": zone, "start": hm, "end": hm})
        out[-1]["_next"] = (
            datetime.strptime(hm, "%H:%M") + timedelta(minutes=1)
        ).strftime("%H:%M")
    for s in out:
        s.pop("_next", None)
    return out


async def simulate_day(
    cfg: AlgoConfig, day: date, *, up_to: Optional[datetime] = None
) -> Optional[dict[str, Any]]:
    """Run the OI-integrated platform over ``day`` (IST, naive wall clock).

    Returns None when the day cannot be simulated (weekend, no configuration,
    no stored data). Otherwise: the contract context, the per-zone OI signal
    windows, and every trade with its engine events (entries, trail raises,
    exits) — exactly what the backtest's day replay would show."""
    if day.weekday() > 4:
        return None
    from ...market.symbols import get_registry

    cfg = cfg.model_copy(deep=True)
    cfg.global_.paper.paper_mode = True       # a simulation never routes orders
    cfg.global_.paper.shadow_mode = False
    day_cfg = cfg.days.get(_WEEKDAYS[day.weekday()])
    if day_cfg is None:
        return None
    symbol = day_cfg.index_symbol
    expiry = (await load_expiry_map([symbol])).for_day(symbol, day)
    if expiry is None:
        return None
    reg = get_registry().get(symbol)
    step = (reg.strike_step if reg else None) or 50
    frame = await load_day_frame(symbol, expiry, day, step)
    if frame is None:
        return None
    try:
        warm = await prefetch_premium_minutes(frame, cfg)
    except Exception:  # noqa: BLE001 — deps falls back to per-contract fetches
        warm = None
    try:
        eod = await prefetch_official_closes(frame, cfg)
    except Exception:  # noqa: BLE001
        eod = None

    ledger = InMemoryLedger(cfg.global_.paper.virtual_balance, "compounding")
    collector = EventCollector()
    deps = BacktestDeps(
        cfg=cfg, frame=frame, ledger=ledger, collector=collector,
        lot_sizes={symbol: (reg.lot_size if reg else 0) or 1},
        premium_minutes=warm if warm is not None else {},
        _premium_warmed=warm is not None,
        official_closes=eod if eod is not None else {},
        _official_warmed=eod is not None,
        config_version=None,
        platform_holidays=known_holidays(),
    )
    orch = ZoneOrchestrator(deps.as_orchestrator_deps())

    close_min = session_close_min(day)
    last = datetime.combine(day, time(close_min // 60, close_min % 60))
    if up_to is not None:
        last = min(last, up_to.replace(second=0, microsecond=0))
    boundary = datetime.combine(day, _FIRST_MINUTE)
    per_minute: list[tuple[str, str, str]] = []
    engines: dict[int, Any] = {}
    prev_id: Optional[int] = None
    while boundary <= last:
        deps.set_now(boundary)
        await orch.evaluate_minute(boundary)
        st = orch.status
        # The minute the reading describes is the one that just CLOSED.
        per_minute.append((
            (boundary - timedelta(minutes=1)).strftime("%H:%M"),
            st.direction or "", st.active_zone or "",
        ))
        pos = orch.position
        cur = pos.trade_id if pos is not None else None
        if pos is not None:
            engines[pos.trade_id] = pos.engine
        if prev_id is not None and prev_id != cur and prev_id in engines:
            collector.capture_engine(prev_id, engines[prev_id])
        prev_id = cur
        boundary += timedelta(minutes=1)
        await asyncio.sleep(0)
    if prev_id is not None and prev_id in engines:
        collector.capture_engine(prev_id, engines[prev_id])   # still open at the cut

    trades = []
    for t in ledger.trades:
        cap = collector.ump_captures.get(t["seq"], {})
        eng_trades = cap.get("engine_trades") or []
        trades.append({
            "id": t["seq"],
            "zone_id": t.get("zone_id"),
            "side": t.get("side"),
            "strike": t.get("strike"),
            "option_type": "CE" if t.get("side") == "CALL" else "PE",
            "expiry": str(t.get("expiry")),
            "entry_ts": _iso(t.get("entry_ts")),
            "fill": t.get("entry_price"),
            "signal_price": eng_trades[-1]["entry_price"] if eng_trades else None,
            "sub_scenario": t.get("sub_scenario"),
            "exit_ts": _iso(t.get("exit_ts")),
            "exit_price": t.get("exit_price"),
            "exit_reason": t.get("exit_reason"),
            "pnl_rupees": t.get("pnl_rupees"),
            "events": cap.get("events") or [],
        })
    return {
        "date": day.isoformat(),
        "symbol": symbol,
        "expiry": expiry.isoformat(),
        "up_to": last.strftime("%H:%M"),
        "signals": _segments(per_minute),
        "trades": trades,
    }


def _iso(v: Any) -> Optional[str]:
    if v is None:
        return None
    return v.isoformat() if hasattr(v, "isoformat") else str(v)
