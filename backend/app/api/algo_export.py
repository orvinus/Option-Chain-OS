"""§6 Export — full-day / custom-range exports of the ledgers and of a
backtest run, streamed server-side with no row cap.

    GET /api/algo/export/{trades|decisions|signals|calendar|summary}
        ?ledger=paper|live (trades/calendar/summary) &from_date&to_date
        [&zone] (decisions/signals) [&indicator] (signals) [&format=csv|ndjson]
    GET /api/algo/export/backtest/{run_id}/{trades|decisions|signals|days|equity|summary|settings}
        [?from_date&to_date] (default = the run's range, always clamped to it)

CSV is RFC-4180 (``text/csv``, ``Content-Disposition: attachment``); NDJSON
(``application/x-ndjson``) starts with ``{"__headers__": [...]}`` and feeds
the browser-side XLSX writer. Formatting lives in ``core/export_stream.py``
(pure, unit-tested); the row sources are the store iterators
(``session.stream`` + ``yield_per``) so a 300k-row decisions export never
sits in memory. Every route requires the signed-in algo identity.

Range rules: ``from_date <= to_date`` and at most 400 days, else 400.
Calendar/summary attribute P&L to the EXIT day (same as the P&L tab);
trades/decisions/signals filter on the stored ``trade_date``.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import date as _date
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from ..algo import decisions as dec
from ..algo import trade_store as store
from ..algo.auth import AdminIdentity, require_admin
from ..algo.backtest import store as bt_store
from ..core.export_stream import (
    DECISION_HEADERS,
    SIGNAL_HEADERS,
    TRADE_HEADERS,
    aiter_list,
    flatten_decision,
    flatten_settings,
    flatten_trade,
    stream_csv,
    stream_ndjson,
)
from .algo_trades import _WEEKDAYS, _allocated_for, _compute_pnl_summary

router = APIRouter(prefix="/algo/export", tags=["algo-export"])

MAX_RANGE_DAYS = 400

Format = Literal["csv", "ndjson"]
LedgerDataset = Literal["trades", "decisions", "signals", "calendar", "summary"]
BacktestDataset = Literal[
    "trades", "decisions", "signals", "days", "equity", "summary", "settings"
]

CALENDAR_HEADERS: tuple[str, ...] = (
    "date", "weekday", "trades", "wins", "losses", "pnl", "allocated", "pnl_pct",
)
# One table for the whole summary: scalar metrics (section=summary, key='')
# followed by the by_day / by_zone / by_date tables, one metric per row.
SUMMARY_HEADERS: tuple[str, ...] = ("section", "key", "metric", "value")
DAY_HEADERS: tuple[str, ...] = (
    "trade_date", "status", "skip_reason", "symbol", "expiry", "trades", "gross",
    "fees", "net", "equity_after", "forced_eod_close", "carried_open", "spotless",
    "minutes", "paused_reason", "detail",
)
EQUITY_HEADERS: tuple[str, ...] = ("date", "equity", "day_pnl")
SETTINGS_HEADERS: tuple[str, ...] = ("key", "value")


# ── validation ───────────────────────────────────────────────────────────────

def _parse_date(v: str, name: str) -> _date:
    try:
        return _date.fromisoformat(v)
    except (TypeError, ValueError) as e:
        raise HTTPException(400, f"{name} must be YYYY-MM-DD (got {v!r})") from e


def _validate_range(from_date: str, to_date: str) -> tuple[_date, _date]:
    frm, to = _parse_date(from_date, "from_date"), _parse_date(to_date, "to_date")
    if frm > to:
        raise HTTPException(400, f"from_date {frm} is after to_date {to}")
    if (to - frm).days + 1 > MAX_RANGE_DAYS:
        raise HTTPException(
            400, f"range is {(to - frm).days + 1} days — the export cap is {MAX_RANGE_DAYS} days"
        )
    return frm, to


def _ledger(v: str) -> str:
    if v not in ("live", "paper"):
        raise HTTPException(400, "ledger must be live or paper")
    return v


def _zone(v: str) -> str:
    z = (v or "").strip().upper()
    if z and z not in ("Z1", "Z2", "Z3"):
        raise HTTPException(400, "zone must be Z1, Z2, Z3 or empty")
    return z


# ── response builder ─────────────────────────────────────────────────────────

def _respond(
    filename_base: str, headers: Sequence[str], rows: AsyncIterator[Any], fmt: str
) -> StreamingResponse:
    if fmt == "ndjson":
        body = stream_ndjson(headers, rows)
        media, ext = "application/x-ndjson", "ndjson"
    else:
        body = stream_csv(headers, rows)
        media, ext = "text/csv; charset=utf-8", "csv"
    name = f"{filename_base}.{ext}"
    return StreamingResponse(
        body,
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _ledger_name(ledger: str, dataset: str, frm: _date, to: _date) -> str:
    return f"algo-{ledger}-{dataset}-{frm.isoformat()}_{to.isoformat()}"


def _run_name(run_id: int, dataset: str, frm: _date, to: _date) -> str:
    return f"backtest-{run_id}-{dataset}-{frm.isoformat()}_{to.isoformat()}"


async def _map(rows: AsyncIterator[dict[str, Any]], fn: Callable[[dict[str, Any]], dict[str, Any]]):
    async for r in rows:
        yield fn(r)


# ── shared row builders (ledger + backtest) ──────────────────────────────────

def _summary_rows(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Scalar metrics first, then the grouped tables one metric per row."""
    tables = {k: v for k, v in summary.items() if isinstance(v, list)}
    out: list[dict[str, Any]] = []
    for k, v in summary.items():
        if k in tables:
            continue
        out.append({
            "section": "summary", "key": "", "metric": k,
            "value": json.dumps(v, sort_keys=True, default=str) if isinstance(v, dict) else v,
        })
    for section, rows in tables.items():
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = row.get("group", row.get("date", ""))
            for metric, value in row.items():
                if metric in ("group",):
                    continue
                out.append({"section": section, "key": key, "metric": metric, "value": value})
    return out


async def _calendar_rows(led: str, frm: _date, to: _date) -> list[dict[str, Any]]:
    """Per exit-date rows — the P&L calendar's numbers, one row per date."""
    rows = await store.summary_by(
        "trade_date", ledger=led, from_date=frm.isoformat(), to_date=to.isoformat()
    )
    alloc_cache: dict[str, float] = {}
    out: list[dict[str, Any]] = []
    for r in rows:
        d = _date.fromisoformat(r["group"])
        day_key = _WEEKDAYS[d.weekday()] if d.weekday() < 5 else ""
        if day_key and day_key not in alloc_cache:
            alloc_cache[day_key] = await _allocated_for(day_key, led)
        alloc = alloc_cache.get(day_key, 0.0) if day_key else 0.0
        out.append({
            "date": r["group"],
            "weekday": day_key or d.strftime("%A").lower(),
            "trades": r["trades"],
            "wins": r["wins"],
            "losses": r["losses"],
            "pnl": r["pnl"],
            "allocated": round(alloc * 100) / 100 if alloc else None,
            "pnl_pct": round(r["pnl"] / alloc * 10000) / 100 if alloc > 0 else None,
        })
    return out


# ── ledger exports ───────────────────────────────────────────────────────────

@router.get("/trades")
async def export_trades(
    ledger: str = Query(default="paper"),
    from_date: str = Query(...),
    to_date: str = Query(...),
    zone: str = Query(default=""),
    format: Format = Query(default="csv"),
    _: AdminIdentity = Depends(require_admin),
) -> StreamingResponse:
    led = _ledger(ledger)
    frm, to = _validate_range(from_date, to_date)
    rows = store.iter_trades(
        ledger=led, from_date=frm.isoformat(), to_date=to.isoformat(), zone=_zone(zone)
    )
    return _respond(_ledger_name(led, "trades", frm, to), TRADE_HEADERS,
                    _map(rows, flatten_trade), format)


@router.get("/decisions")
async def export_decisions(
    from_date: str = Query(...),
    to_date: str = Query(...),
    zone: str = Query(default=""),
    ledger: str = Query(default=""),
    format: Format = Query(default="csv"),
    _: AdminIdentity = Depends(require_admin),
) -> StreamingResponse:
    """The live decision trace holds both ledgers' rows (the row carries its
    ``ledger`` column); ``ledger`` here only names the file."""
    frm, to = _validate_range(from_date, to_date)
    rows = dec.iter_live(frm, to, _zone(zone))
    label = ledger if ledger in ("live", "paper") else "live"
    return _respond(_ledger_name(label, "decisions", frm, to), DECISION_HEADERS,
                    _map(rows, flatten_decision), format)


@router.get("/signals")
async def export_signals(
    from_date: str = Query(...),
    to_date: str = Query(...),
    zone: str = Query(default=""),
    indicator: str = Query(default=""),
    ledger: str = Query(default=""),
    format: Format = Query(default="csv"),
    _: AdminIdentity = Depends(require_admin),
) -> StreamingResponse:
    frm, to = _validate_range(from_date, to_date)
    rows = store.iter_signals(
        from_date=frm.isoformat(), to_date=to.isoformat(), zone=_zone(zone),
        indicator=(indicator or "").strip().lower(),
    )
    label = ledger if ledger in ("live", "paper") else "live"
    return _respond(_ledger_name(label, "signals", frm, to), SIGNAL_HEADERS, rows, format)


@router.get("/calendar")
async def export_calendar(
    ledger: str = Query(default="paper"),
    from_date: str = Query(...),
    to_date: str = Query(...),
    format: Format = Query(default="csv"),
    _: AdminIdentity = Depends(require_admin),
) -> StreamingResponse:
    led = _ledger(ledger)
    frm, to = _validate_range(from_date, to_date)
    rows = await _calendar_rows(led, frm, to)
    return _respond(_ledger_name(led, "calendar", frm, to), CALENDAR_HEADERS,
                    aiter_list(rows), format)


@router.get("/summary")
async def export_summary(
    ledger: str = Query(default="paper"),
    from_date: str = Query(...),
    to_date: str = Query(...),
    format: Format = Query(default="csv"),
    _: AdminIdentity = Depends(require_admin),
) -> StreamingResponse:
    led = _ledger(ledger)
    frm, to = _validate_range(from_date, to_date)
    summary = await _compute_pnl_summary(led, frm.isoformat(), to.isoformat())
    return _respond(_ledger_name(led, "summary", frm, to), SUMMARY_HEADERS,
                    aiter_list(_summary_rows(summary)), format)


# ── backtest exports ─────────────────────────────────────────────────────────

async def _run_or_404(run_id: int) -> dict[str, Any]:
    run = await bt_store.get_run(run_id)
    if run is None:
        raise HTTPException(404, f"backtest run {run_id} not found")
    return run


def _clamp_to_run(
    run: dict[str, Any], from_date: str | None, to_date: str | None
) -> tuple[_date, _date]:
    run_from, run_to = _date.fromisoformat(run["from_date"]), _date.fromisoformat(run["to_date"])
    frm = _parse_date(from_date, "from_date") if from_date else run_from
    to = _parse_date(to_date, "to_date") if to_date else run_to
    if frm > to:
        raise HTTPException(400, f"from_date {frm} is after to_date {to}")
    frm, to = max(frm, run_from), min(to, run_to)
    if frm > to:
        raise HTTPException(
            400, f"range does not overlap the run ({run_from} → {run_to})"
        )
    if (to - frm).days + 1 > MAX_RANGE_DAYS:
        raise HTTPException(
            400, f"range is {(to - frm).days + 1} days — the export cap is {MAX_RANGE_DAYS} days"
        )
    return frm, to


def _day_rows(days: list[dict[str, Any]], frm: _date, to: _date) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for d in days:
        td = d["trade_date"]
        if td < frm.isoformat() or td > to.isoformat():
            continue
        det = d.get("detail") or {}
        out.append({
            "trade_date": td,
            "status": d["status"],
            "skip_reason": d.get("skip_reason", ""),
            "symbol": det.get("symbol"),
            "expiry": det.get("expiry"),
            "trades": det.get("trades"),
            "gross": det.get("gross"),
            "fees": det.get("fees"),
            "net": det.get("net"),
            "equity_after": det.get("equity_after"),
            "forced_eod_close": det.get("forced_eod_close"),
            "carried_open": det.get("carried_open"),
            "spotless": det.get("spotless"),
            "minutes": det.get("minutes"),
            "paused_reason": det.get("paused_reason"),
            "detail": det or None,
        })
    return out


@router.get("/backtest/{run_id}/{dataset}")
async def export_backtest(
    run_id: int,
    dataset: BacktestDataset,
    from_date: str | None = Query(default=None),
    to_date: str | None = Query(default=None),
    zone: str = Query(default=""),
    indicator: str = Query(default=""),
    format: Format = Query(default="csv"),
    _: AdminIdentity = Depends(require_admin),
) -> StreamingResponse:
    run = await _run_or_404(run_id)
    frm, to = _clamp_to_run(run, from_date, to_date)
    name = _run_name(run_id, dataset, frm, to)
    f, t = frm.isoformat(), to.isoformat()

    if dataset == "trades":
        rows = bt_store.iter_run_trades(run_id, from_date=f, to_date=t)
        return _respond(name, TRADE_HEADERS, _map(rows, flatten_trade), format)
    if dataset == "decisions":
        rows = bt_store.iter_run_decisions(run_id, from_date=f, to_date=t, zone=_zone(zone))
        return _respond(name, DECISION_HEADERS, _map(rows, flatten_decision), format)
    if dataset == "signals":
        rows = bt_store.iter_run_signals(
            run_id, from_date=f, to_date=t, zone=_zone(zone),
            indicator=(indicator or "").strip().lower(),
        )
        return _respond(name, SIGNAL_HEADERS, rows, format)
    if dataset == "days":
        days = await bt_store.run_days(run_id)
        return _respond(name, DAY_HEADERS, aiter_list(_day_rows(days, frm, to)), format)
    if dataset == "equity":
        summary = await bt_store.summarize(run_id)
        pts = [p for p in summary.get("equity", []) if f <= p["date"] <= t]
        return _respond(name, EQUITY_HEADERS, aiter_list(pts), format)
    if dataset == "summary":
        summary = await bt_store.summarize(run_id)
        summary = {
            "run_id": run_id, "label": run.get("label"), "status": run.get("status"),
            "from_date": run["from_date"], "to_date": run["to_date"],
            "config_version": run.get("config_version"),
            **{k: v for k, v in summary.items() if k != "equity"},
        }
        return _respond(name, SUMMARY_HEADERS, aiter_list(_summary_rows(summary)), format)
    # settings — the frozen run inputs (settings JSON incl. sim_effective).
    doc = {
        "run_id": run_id,
        "label": run.get("label"),
        "created_by": run.get("created_by"),
        "created_at": run.get("created_at"),
        "from_date": run["from_date"],
        "to_date": run["to_date"],
        "config_version": run.get("config_version"),
        "status": run.get("status"),
        "settings": run.get("settings") or {},
    }
    return _respond(name, SETTINGS_HEADERS, aiter_list(flatten_settings(doc)), format)


__all__ = ["router"]
