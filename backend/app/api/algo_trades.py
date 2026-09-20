"""P&L, trade-log, paper-session and live-status endpoints
(`/api/algo/trades*`, `/api/algo/status`, admin-gated).

Every figure is produced per ledger (live vs paper) with identical logic —
the two are never mixed in one aggregate (§12). The calendar's per-day
percentage uses that WEEKDAY's allocated capital from the current config
(§12.2's basis), which matches the risk limits' own units.
"""
from __future__ import annotations

from datetime import date as _date
from datetime import timedelta
from typing import Any, Literal, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..algo import trade_store as store
from ..algo.audit import audit
from ..algo.auth import (
    AdminIdentity,
    require_admin,
    require_admin_role,
    require_editor,
)
from ..algo.config_store import get_config_store
from ..algo.drawdown import equity_points_from_pnl, scan_drawdown
from ..algo.orchestrator import get_orchestrator
from ..core.time_utils import now_ist

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/algo", tags=["algo-trades"])

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")


def _ledger(v: str) -> str:
    if v not in ("live", "paper", ""):
        raise HTTPException(400, "ledger must be live, paper or empty")
    return v


async def _allocated_for(day_key: str, ledger: str) -> float:
    """Allocated capital for a weekday, on the balance basis of the LEDGER
    being queried — not the global paper_mode flag. (Bug fixed 2026-08-17:
    querying ledger=live while Paper Mode was ON divided live rupee P&L by
    the paper virtual balance, and vice versa.)"""
    cfg = (await get_config_store().get_live()).config
    d = cfg.days.get(day_key)  # type: ignore[arg-type]
    if d is None:
        return 0.0
    balance = (
        cfg.global_.paper.virtual_balance
        if ledger == "paper"
        else cfg.global_.demat_balance
    )
    pct = 100.0 if d.all_in else d.allocation_pct
    return balance * pct / 100


@router.get("/trades")
async def trade_log(
    date: Optional[str] = Query(default=None),
    day: str = Query(default=""),
    zone: str = Query(default=""),
    ledger: str = Query(default=""),
    from_date: Optional[str] = Query(default=None),
    to_date: Optional[str] = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
    _: AdminIdentity = Depends(require_admin),
) -> list[dict[str, Any]]:
    return await store.trades_filtered(
        trade_date=date, day=day.lower(), zone=zone.upper() if zone else "",
        ledger=_ledger(ledger), from_date=from_date, to_date=to_date, limit=limit,
    )


@router.get("/pnl/summary")
async def pnl_summary(
    ledger: str = Query(default="paper"),
    from_date: Optional[str] = Query(default=None),
    to_date: Optional[str] = Query(default=None),
    _: AdminIdentity = Depends(require_admin),
) -> dict[str, Any]:
    led = _ledger(ledger) or "paper"
    today = now_ist().date()
    start = from_date or (today - timedelta(days=today.weekday())).isoformat()
    end = to_date or today.isoformat()
    return await _compute_pnl_summary(led, start, end)


async def _compute_pnl_summary(led: str, start: str, end: str) -> dict[str, Any]:
    """The Cumulative Performance numbers for one ledger over [start, end]
    (exit-day attributed). Shared by ``GET /pnl/summary`` and the §6
    summary export so both always agree."""
    by_day = await store.summary_by("day", ledger=led, from_date=start, to_date=end)
    by_zone = await store.summary_by("zone_id", ledger=led, from_date=start, to_date=end)
    by_date = await store.summary_by("trade_date", ledger=led, from_date=start, to_date=end)

    trades = sum(r["trades"] for r in by_date)
    wins = sum(r["wins"] for r in by_date)
    pnl = round(sum(r["pnl"] for r in by_date) * 100) / 100
    gross_win = sum(r["gross_win"] for r in by_date)
    gross_loss = sum(-r["gross_loss"] for r in by_date)

    # §12.2 basis: allocation counts PER TRADED DATE — a weekday that traded
    # on N dates in the range contributes N × its allocated capital. (Bug
    # fixed 2026-08-18: summing one allocation per weekday NAME under-counted
    # the denominator ~N× on multi-week ranges, inflating every percentage.)
    alloc_cache: dict[str, float] = {}

    async def _alloc(day_key: str) -> float:
        if day_key not in alloc_cache:
            alloc_cache[day_key] = await _allocated_for(day_key, led)
        return alloc_cache[day_key]

    dates_per_day: dict[str, int] = {}
    total_allocated = 0.0
    blended_pnl = 0.0
    for r in by_date:
        d = _date.fromisoformat(r["group"])
        if d.weekday() >= 5:
            # A weekend-dated row (special Saturday session / manual backfill)
            # has no weekday allocation — keep it out of BOTH sides of the
            # blended ratio rather than inflating the numerator only.
            continue
        day_key = _WEEKDAYS[d.weekday()]
        dates_per_day[day_key] = dates_per_day.get(day_key, 0) + 1
        total_allocated += await _alloc(day_key)
        blended_pnl += float(r["pnl"])
    for r in by_day:
        alloc = (await _alloc(r["group"])) * dates_per_day.get(r["group"], 0)
        r["allocated"] = round(alloc * 100) / 100
        r["pnl_pct"] = round(r["pnl"] / alloc * 10000) / 100 if alloc > 0 else None

    return {
        "ledger": led,
        "from_date": start,
        "to_date": end,
        "trades": trades,
        "wins": wins,
        "losses": trades - wins,
        "win_rate": round(wins / trades * 1000) / 10 if trades else None,
        "pnl": pnl,
        "avg_win": round(gross_win / wins * 100) / 100 if wins else None,
        "avg_loss": round(-gross_loss / (trades - wins) * 100) / 100 if trades - wins else None,
        "profit_factor": round(gross_win / gross_loss * 100) / 100 if gross_loss > 0 else None,
        # Blended %: total pnl ÷ total allocated across the traded days —
        # never an average of daily percentages (§12.2).
        "pnl_pct_blended": (
            round(blended_pnl / total_allocated * 10000) / 100
            if total_allocated > 0 else None
        ),
        "by_day": by_day,
        "by_zone": by_zone,
        "by_date": by_date,
        # §7 max drawdown — equity = the ledger's balance + cumulative closed
        # P&L, one point per exit (same exit-day range as the aggregates).
        **(await _drawdown_fields(led, start, end)),
    }


DRAWDOWN_BASIS = "ledger balance + cumulative closed P&L (peak-to-trough, % of peak equity)"


async def _drawdown_fields(led: str, start: str, end: str) -> dict[str, Any]:
    cfg = (await get_config_store().get_live()).config
    base = (
        cfg.global_.paper.virtual_balance if led == "paper" else cfg.global_.demat_balance
    )
    closes = await store.closed_points(led, start, end)
    dd = scan_drawdown(equity_points_from_pnl(closes, starting=base), starting=base)
    return {
        "max_drawdown": round(dd.max_dd * 100) / 100,
        "max_drawdown_pct": dd.max_dd_pct,
        "max_drawdown_from": dd.from_key,
        "max_drawdown_to": dd.to_key,
        "drawdown_basis": DRAWDOWN_BASIS,
    }


@router.get("/pnl/calendar")
async def pnl_calendar(
    month: str = Query(..., description="YYYY-MM"),
    ledger: str = Query(default="paper"),
    _: AdminIdentity = Depends(require_admin),
) -> dict[str, Any]:
    led = _ledger(ledger) or "paper"
    try:
        year, mon = map(int, month.split("-"))
        first = _date(year, mon, 1)
    except ValueError as e:
        raise HTTPException(400, f"invalid month {month!r}") from e
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)

    rows = await store.summary_by(
        "trade_date", ledger=led, from_date=first.isoformat(), to_date=last.isoformat()
    )
    days: dict[str, dict[str, Any]] = {}
    for r in rows:
        d = _date.fromisoformat(r["group"])
        day_key = _WEEKDAYS[d.weekday()] if d.weekday() < 5 else ""
        alloc = await _allocated_for(day_key, led) if day_key else 0.0
        days[r["group"]] = {
            "pnl": r["pnl"],
            "pnl_pct": round(r["pnl"] / alloc * 10000) / 100 if alloc > 0 else None,
            "trades": r["trades"],
        }
    return {"month": month, "ledger": led, "days": days}


@router.get("/notes")
async def day_notes(
    month: str = Query(..., description="YYYY-MM"),
    _: AdminIdentity = Depends(require_admin),
) -> dict[str, str]:
    """Per-day journal notes for one calendar month → {date: note}."""
    try:
        year, mon = map(int, month.split("-"))
        first = _date(year, mon, 1)
    except ValueError as e:
        raise HTTPException(400, f"invalid month {month!r}") from e
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return await store.notes_for_range(first.isoformat(), last.isoformat())


class _NoteBody(BaseModel):
    note: str = ""


@router.put("/notes/{note_date}")
async def put_day_note(
    note_date: str,
    body: _NoteBody,
    ident: AdminIdentity = Depends(require_editor),
) -> dict[str, str]:
    """Save the day's journal note; an empty note removes it. Audited."""
    try:
        _date.fromisoformat(note_date)
    except ValueError as e:
        raise HTTPException(400, f"invalid date {note_date!r}") from e
    action = await store.set_day_note(note_date, body.note, ident.username)
    await audit(
        "day_note", user_id=ident.user_id, username=ident.username,
        scope="global", field=note_date,
        detail={"action": action, "chars": len(body.note.strip())},
    )
    return {"status": action}


@router.get("/paper/session")
async def paper_session(_: AdminIdentity = Depends(require_admin)) -> dict[str, Any]:
    cfg = (await get_config_store().get_live()).config
    today = now_ist().date()
    since = cfg.global_.paper.session_started or today.isoformat()
    realized = await store.paper_realized_since(since)
    current = round((cfg.global_.paper.virtual_balance + realized) * 100) / 100
    open_position = _open_paper_position()
    unrealized = float((open_position or {}).get("unrealized_gross") or 0.0)
    closed_today = await store.today_closed(today, "paper")
    return {
        "session_started": since,
        "starting_balance": cfg.global_.paper.virtual_balance,
        "realized_pnl": round(realized * 100) / 100,
        "current_balance": current,
        # §8 completeness — the open paper position, marked to the engine's
        # last close, and the equity that includes it.
        "open_position": open_position,
        "unrealized_pnl": round(unrealized * 100) / 100,
        "equity": round((current + unrealized) * 100) / 100,
        "trades_today": len(closed_today),
        "realized_today": round(sum(p for _, p in closed_today) * 100) / 100,
    }


def _open_paper_position() -> Optional[dict[str, Any]]:
    """The orchestrator's status position when it is a PAPER position (its
    ledger, not the global paper_mode flag), in the PaperOpenPosition shape;
    None when nothing is open or the orchestrator is not in this process."""
    orch = get_orchestrator()
    pos = orch.status.position if orch is not None else None
    if not pos or pos.get("ledger") != "paper":
        return None
    return {
        "trade_id": pos.get("trade_id"),
        "strike": pos.get("strike"),
        "option_type": pos.get("option_type"),
        "side": pos.get("side"),
        "lots": pos.get("lots"),
        "entry_fill": pos.get("entry"),
        "entry_ts": pos.get("entry_ts"),
        "last_close": pos.get("last_close"),
        "unrealized_gross": pos.get("unrealized_gross"),
        "unrealized_pct": pos.get("unrealized_pct"),
    }


@router.post("/paper/reset")
async def paper_reset(ident: AdminIdentity = Depends(require_admin_role)) -> dict[str, Any]:
    """§8.3 Reset controls — clears the PAPER ledger and restarts the session
    today. The live ledger is untouchable by design. Refused while a paper
    position is open: deleting its row would orphan the in-memory position
    (its exit would then close a trade that no longer exists)."""
    orch = get_orchestrator()
    live_pos = getattr(orch, "position", None) if orch is not None else None
    if live_pos is not None and getattr(live_pos, "ledger", "") == "paper":
        raise HTTPException(
            409,
            f"a paper position is open (trade #{live_pos.trade_id}) — "
            "wait for its exit before resetting",
        )
    deleted = await store.reset_paper_ledger()
    cs = get_config_store()
    cv = await cs.get_live()
    cfg = cv.config.model_copy(deep=True)
    cfg.global_.paper.session_started = now_ist().date().isoformat()
    await cs.save(
        cfg, saved_by=ident.username, note="paper session reset", user_id=ident.user_id
    )
    await audit(
        "paper_reset", user_id=ident.user_id, username=ident.username,
        scope="global", detail={"deleted_rows": deleted},
    )
    return {"status": "ok", "deleted_rows": deleted}


@router.get("/status")
async def orchestrator_status(_: AdminIdentity = Depends(require_admin)) -> dict[str, Any]:
    orch = get_orchestrator()
    if orch is None:
        return {"running": False}
    s = orch.status
    return {
        "running": True,
        "state": s.state,
        "active_zone": s.active_zone,
        "direction": s.direction,
        "readings": s.readings,
        "position": s.position,
        "gate_blocks": s.gate_blocks,
        "paused_reason": s.paused_reason,
        "realized_pnl_today": s.realized_pnl_today,
        "trades_today": s.trades_today,
        "last_evaluated": s.last_evaluated,
        "hunting_strikes": s.hunting_strikes,
    }


@router.get("/decisions")
async def decisions(
    date: Optional[str] = Query(default=None),
    zone: str = Query(default=""),
    limit: int = Query(default=400, ge=1, le=2000),
    compact: bool = Query(default=False),
    since: Optional[str] = Query(default=None),
    _: AdminIdentity = Depends(require_admin),
) -> dict[str, Any]:
    """The per-minute decision trace (algo_decisions): one row per evaluated
    minute with the stage reached, gates, indicator readings + traces, the
    candidate strikes and the outcome. Newest first."""
    from datetime import datetime as _dt

    from ..algo import decisions as dec

    try:
        d = _date.fromisoformat(date) if date else now_ist().date()
    except ValueError as e:
        raise HTTPException(400, f"invalid date {date!r}") from e
    since_dt = None
    if since:
        try:
            since_dt = _dt.fromisoformat(since)
        except ValueError as e:
            raise HTTPException(400, f"invalid since {since!r}") from e
    rows = await dec.fetch_live(d, zone.upper() if zone else "", limit=limit,
                                compact=compact, since=since_dt)
    return {"date": d.isoformat(), "zone": zone.upper() if zone else "", "rows": rows}


@router.get("/decisions/latest")
async def decisions_latest(_: AdminIdentity = Depends(require_admin)) -> dict[str, Any]:
    from ..algo import decisions as dec

    row = await dec.latest()
    return {"row": row}


@router.get("/signals")
async def signals(
    date: Optional[str] = Query(default=None),
    zone: str = Query(default=""),
    indicator: str = Query(default=""),
    limit: int = Query(default=400, ge=1, le=2000),
    _: AdminIdentity = Depends(require_admin),
) -> dict[str, Any]:
    """Indicator reading TRANSITIONS of one date (algo_signals), newest
    first — the JSON twin of the §6 signals export."""
    try:
        d = _date.fromisoformat(date) if date else now_ist().date()
    except ValueError as e:
        raise HTTPException(400, f"invalid date {date!r}") from e
    z = zone.upper() if zone else ""
    rows = await store.signals_filtered(
        trade_date=d.isoformat(), zone=z, indicator=indicator.lower() if indicator else "",
        limit=limit,
    )
    return {"date": d.isoformat(), "zone": z, "rows": rows}


@router.get("/broker/status")
async def broker_status(_: AdminIdentity = Depends(require_admin)) -> dict[str, Any]:
    """Lakshmishree Interactive API connection state for the Integrations tab."""
    from ..algo.broker import build_execution, get_interactive_client

    mode, detail = build_execution()
    client = get_interactive_client()
    return {
        "mode": mode,
        "detail": detail,
        "configured": client.configured,
        "logged_in": client.logged_in,
        "last_error": client.last_error,
    }


@router.get("/broker/account")
async def broker_account(_: AdminIdentity = Depends(require_admin)) -> dict[str, Any]:
    """A-to-Z live account state from the Interactive API: balance, net
    positions and today's order book. Read-only; the singleton session is
    reused (one login per process). Gateway failures come back as
    ``connected: false`` + reason so the panel can render them — never a 5xx
    for a broker-side problem."""
    from ..algo.backtest import store as bt_store
    from ..algo.broker import XtsInteractiveError, get_interactive_client

    import asyncio as _asyncio
    import time as _t

    from ..algo.broker import XtsTransportError

    client = get_interactive_client()
    if not client.configured:
        raise HTTPException(409, "Interactive API credentials are not configured.")

    async def _read(fn):
        # READ-ONLY calls may be retried once on a transport timeout: the first
        # call after an idle spell (cold TLS + a slow gateway) timed out at 10 s
        # and failed the whole card (13.7 s, 2026-09-15). Order placement is
        # never retried this way — a retried BUY can execute twice.
        try:
            return await fn()
        except XtsTransportError:
            return await fn()

    t0 = _t.perf_counter()
    try:
        # The three reads are independent — in parallel the card waits for the
        # slowest one, not the sum of all three.
        if not client.logged_in:
            await client.login()
        balance, positions, orders = await _asyncio.gather(
            _read(client.balance), _read(client.positions_net), _read(client.order_book)
        )
    except XtsInteractiveError as e:
        # Clean the reason (gateways return whole HTML pages off-hours) and
        # serve the LAST-GOOD snapshot so funds/positions stay visible on
        # weekends instead of an empty panel.
        reason = " ".join(str(e).split())
        if "<html" in reason.lower():
            reason = "broker gateway offline (returned an HTML error page) — normal outside market hours"
        elif len(reason) > 180:
            reason = reason[:177] + "…"
        snapshot = None
        try:
            snapshot = await bt_store.get_broker_snapshot()
        except Exception:  # snapshot cache must never break the endpoint
            log.warning("broker.snapshot_read_failed")
        return {
            "connected": False,
            "error": reason,
            "balance": None,
            "positions": [],
            "orders": [],
            "last_snapshot": snapshot,
        }
    await _enrich_positions_with_our_price(positions)
    payload = {
        "connected": True,
        "error": None,
        "balance": balance,
        "positions": positions,
        "orders": orders,
        "fetched_ms": round((_t.perf_counter() - t0) * 1000),
    }
    try:
        await bt_store.put_broker_snapshot(
            {"balance": balance, "positions": positions, "orders": orders}
        )
    except Exception:
        log.warning("broker.snapshot_write_failed")
    return payload


_TRADING_SYMBOL_RE = None


async def _enrich_positions_with_our_price(positions: list[dict[str, Any]]) -> None:
    """Add ``OurLTP`` / ``OurMTM`` (and when it was priced) to each broker net
    position, from this platform's own newest tick for that contract.

    The broker's MTM / UnrealizedMTM fields read 0.00 throughout the 2026-09-15
    burn-in, so the card had no way to show what an open position is worth.
    TradingSymbol looks like ``NIFTY 15SEP2026 CE 23600``; anything that does
    not parse is left untouched. MTM = SellAmount − BuyAmount + open quantity × our last price."""
    import re
    from datetime import datetime as _dt

    from ..algo.series import latest_contract_quote
    from ..core.time_utils import IST

    global _TRADING_SYMBOL_RE
    if _TRADING_SYMBOL_RE is None:
        _TRADING_SYMBOL_RE = re.compile(r"^([A-Z0-9&-]+)\s+(\d{2}[A-Z]{3}\d{4})\s+(CE|PE)\s+(\d+)$")

    def num(v: Any) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    for row in positions:
        try:
            m = _TRADING_SYMBOL_RE.match(str(row.get("TradingSymbol", "")).strip().upper())
            if not m:
                continue
            sym, exp_s, ot, strike = m.group(1), m.group(2), m.group(3), int(m.group(4))
            expiry = _dt.strptime(exp_s, "%d%b%Y").date()
            q = await latest_contract_quote(sym, expiry, strike, ot)
            # Exact, from the broker's own traded amounts: cash out of the sells
            # minus cash into the buys, plus whatever quantity is still open
            # marked at our last price. (Gross — before charges.)
            buy_amt, sell_amt = num(row.get("BuyAmount")), num(row.get("SellAmount"))
            net = num(row.get("Quantity"))
            if q is not None:
                ltp, ts = q
                row["OurLTP"] = round(ltp, 2)
                row["OurLTPAt"] = ts.astimezone(IST).strftime("%H:%M:%S")
                row["OurMTM"] = round(sell_amt - buy_amt + net * ltp, 2)
            elif net == 0 and (buy_amt or sell_amt):
                row["OurMTM"] = round(sell_amt - buy_amt, 2)
        except Exception as e:  # enrichment must never break the account card
            log.warning("broker.position_enrich_failed", error=str(e))


@router.get("/broker/reconcile")
async def broker_reconcile(_: AdminIdentity = Depends(require_admin)) -> dict[str, Any]:
    """Broker-vs-engine position audit on demand. With no live position open
    there is nothing of the algo's to reconcile (manual holdings are not our
    business) — the response says so rather than guessing."""
    from ..algo.broker.execution import reconcile_live_position

    orch = get_orchestrator()
    pos = orch.position if orch is not None else None
    if pos is None or pos.ledger != "live" or not pos.broker_order_id:
        return {
            "position": None,
            "match": None,
            "detail": "no live broker position is open — nothing to reconcile",
        }
    from ..market.symbols import get_registry

    entry = get_registry().get(pos.contract.symbol)
    lot = entry.lot_size if entry is not None else 75
    match, detail = await reconcile_live_position(
        symbol=pos.contract.symbol,
        expiry=pos.contract.expiry,
        strike=pos.contract.strike,
        option_type=pos.contract.option_type,
        expected_qty=pos.lots * lot,
    )
    return {
        "position": {
            "trade_id": pos.trade_id,
            "contract": pos.contract.token,
            "lots": pos.lots,
        },
        "match": match,
        "detail": detail,
    }


@router.get("/telegram/status")
async def telegram_status(
    force: bool = Query(default=False), _: AdminIdentity = Depends(require_admin)
) -> dict[str, Any]:
    """Honest connection state for the Integrations card: configured?
    bot reachable (getMe, cached 5 min)? Never exposes the token."""
    from ..core.config import settings as _settings
    from ..core.notify import probe

    out = await probe(force=force)
    out["min_interval_s"] = _settings.alert_min_interval_s
    return out


@router.post("/telegram/test")
async def telegram_test(ident: AdminIdentity = Depends(require_admin_role)) -> dict[str, Any]:
    """Send one real test message (bypasses the per-key throttle)."""
    from ..core.notify import is_configured, send_now

    if not is_configured():
        raise HTTPException(
            409, "Telegram is not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID unset)"
        )
    ok, detail = await send_now(
        f"🔔 Test message from OI Algo — sent by {ident.username or 'admin'} at "
        f"{now_ist():%H:%M} IST"
    )
    await audit(
        "telegram_test", user_id=ident.user_id, username=ident.username, scope="global",
        detail={"ok": ok, "detail": detail},
    )
    if not ok:
        raise HTTPException(502, f"Telegram send failed: {detail}")
    return {"status": "sent", "detail": detail}


@router.post("/broker/test-login")
async def broker_test_login(ident: AdminIdentity = Depends(require_admin_role)) -> dict[str, Any]:
    """Admin-triggered probe of the Interactive session — part of the broker
    burn-in checklist. Never called automatically."""
    from ..algo.broker import XtsInteractiveError, get_interactive_client

    client = get_interactive_client()
    if not client.configured:
        raise HTTPException(409, "Interactive API credentials are not configured.")
    try:
        # login() early-returns when a token is already held — which can be a
        # DEAD token (daily expiry / second session). An authed probe both
        # validates the session for real and self-heals it via the client's
        # single-re-login path (observed lying "ok" on a dead token 2026-08-17).
        await client.login()
        await client.balance()
    except XtsInteractiveError as e:
        raise HTTPException(502, str(e)) from e
    await audit(
        "broker_test_login", user_id=ident.user_id, username=ident.username, scope="global"
    )
    return {"status": "ok", "logged_in": True}


# ── manual burn-in orders (admin, from Integrations & Broker) ────────────────

# Hard server-side ceiling on a manual test entry's notional. The UI cannot
# raise it: a test order exists to prove placement, fill, ledger and latency,
# never to take real exposure.
MANUAL_TEST_MAX_NOTIONAL = 1000.0


class ManualTestEntryRequest(BaseModel):
    side: Literal["CALL", "PUT"]
    max_notional: float = Field(default=MANUAL_TEST_MAX_NOTIONAL, gt=0, le=MANUAL_TEST_MAX_NOTIONAL)
    premium_min: float = Field(default=0.5, gt=0, le=5)
    premium_max: float = Field(default=2.0, gt=0, le=5)
    # Must be true when Paper Mode is OFF — the UI sets it only after the
    # admin confirms a REAL order in a dialog.
    confirm_live: bool = False


class ManualSquareOffRequest(BaseModel):
    confirm_live: bool = False


async def _live_mode() -> bool:
    cv = await get_config_store().get_live()
    return not cv.config.global_.paper.paper_mode


@router.post("/manual/test-entry")
async def manual_test_entry(
    body: ManualTestEntryRequest,
    ident: AdminIdentity = Depends(require_admin_role),
) -> dict[str, Any]:
    """Place ONE small test position now through the engine's own execution
    path (paper or live per Paper Mode). Capped at ₹1,000 notional."""
    import time as _t

    from ..algo.orchestrator import ManualOrderError

    t0 = _t.perf_counter()
    orch = get_orchestrator()
    if orch is None:
        raise HTTPException(409, "The orchestrator is not running in this process.")
    if body.premium_min >= body.premium_max:
        raise HTTPException(422, "premium_min must be below premium_max")
    live = await _live_mode()
    if live and not body.confirm_live:
        raise HTTPException(409, "Paper Mode is OFF — this would be a REAL order. Confirm to proceed.")
    try:
        result = await orch.manual_test_entry(
            now_ist().replace(tzinfo=None),
            side=body.side,
            max_notional=body.max_notional,
            premium_min=body.premium_min,
            premium_max=body.premium_max,
        )
    except ManualOrderError as e:
        await audit(
            "manual_test_entry_refused", user_id=ident.user_id, username=ident.username,
            scope="global", detail={"reason": str(e), "live": live, **body.model_dump()},
        )
        raise HTTPException(409, str(e)) from e
    result["server_ms"] = round((_t.perf_counter() - t0) * 1000, 1)
    await audit(
        "manual_test_entry", user_id=ident.user_id, username=ident.username, scope="global",
        detail={k: result[k] for k in ("trade_id", "ledger", "strike", "option_type", "lots",
                                       "fill_price", "broker_order_id", "server_ms")},
    )
    return result


@router.post("/manual/square-off")
async def manual_square_off(
    body: ManualSquareOffRequest,
    ident: AdminIdentity = Depends(require_admin_role),
) -> dict[str, Any]:
    """Close the open position now through the engine's own exit path."""
    import time as _t

    from ..algo.orchestrator import ManualOrderError

    t0 = _t.perf_counter()
    orch = get_orchestrator()
    if orch is None:
        raise HTTPException(409, "The orchestrator is not running in this process.")
    pos = orch.position
    if pos is not None and pos.ledger == "live" and not body.confirm_live:
        raise HTTPException(409, "This position is LIVE — squaring off sends a REAL sell. Confirm to proceed.")
    try:
        result = await orch.manual_square_off(now_ist().replace(tzinfo=None))
    except ManualOrderError as e:
        raise HTTPException(409, str(e)) from e
    trade = None
    if result.get("trade_id", -1) >= 0:
        rows = await store.trades_filtered(
            trade_date=now_ist().date().isoformat(), ledger=result["ledger"], limit=50,
        )
        trade = next((r for r in rows if r.get("id") == result["trade_id"]), None)
    result["trade"] = trade
    result["server_ms"] = round((_t.perf_counter() - t0) * 1000, 1)
    await audit(
        "manual_square_off", user_id=ident.user_id, username=ident.username, scope="global",
        detail={k: result.get(k) for k in ("trade_id", "ledger", "closed", "server_ms")},
    )
    return result


@router.post("/resume")
async def resume_engine(ident: AdminIdentity = Depends(require_admin_role)) -> dict[str, Any]:
    """Manual resume after the consecutive-loss auto-pause (§7.2)."""
    orch = get_orchestrator()
    if orch is None:
        raise HTTPException(409, "The orchestrator is not running in this process.")
    orch.resume()
    await audit("engine_resume", user_id=ident.user_id, username=ident.username, scope="global")
    return {"status": "ok"}
