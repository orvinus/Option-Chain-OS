"""Backtest endpoints (`/api/algo/backtest/*`, admin-gated).

Create/monitor/cancel/resume/delete runs, plus the read models the
Backtesting tab renders: TradeRow-shaped trade logs, PnlSummary/PnlCalendar-
shaped aggregates (so the P&L components render backtest results unchanged),
the equity curve, and the one-shot per-day replay bundle the scrubber
consumes with zero further calls.

Single-flight: one run at a time process-wide — the runner shares the event
loop and DB pool with the live orchestrator.
"""
from __future__ import annotations

from datetime import date as _date
from datetime import timedelta
from typing import Any, Literal, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..algo.audit import audit, config_diff
from ..algo.auth import AdminIdentity, require_admin, require_editor
from ..algo.backtest import runner as bt_runner
from ..algo.backtest import store as bt_store
from ..algo.backtest.preflight import run_preflight
from ..algo.config_models import AlgoConfig, validate_document
from ..algo.config_store import get_config_store
from ..core.holidays import known_holidays
from ..market.symbols import get_registry

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/algo/backtest", tags=["algo-backtest"])

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")


# ── request models ──────────────────────────────────────────────────────────

class ConfigSource(BaseModel):
    source: Literal["live", "version", "inline"] = "live"
    version: Optional[int] = None
    document: Optional[dict[str, Any]] = None


class SimSettings(BaseModel):
    """Per-run simulation inputs (New Run form → "Simulation settings"). Only
    the two knobs the simulator actually consumes are editable per run; they
    are applied to the FROZEN config so the runner needs no change and the
    run stays reproducible."""
    slippage_pct: Optional[float] = Field(default=None, ge=0, le=10)
    brokerage_per_order: Optional[float] = Field(default=None, ge=0, le=1000)


class RunSettings(BaseModel):
    balance_mode: Literal["compounding", "fixed_per_day"] = "compounding"
    starting_balance: Optional[float] = Field(default=None, gt=0)
    symbols: list[str] = Field(default_factory=list)
    excluded_days: list[str] = Field(default_factory=list)
    use_default_exclusions: bool = True
    min_minutes_per_day: int = Field(default=300, ge=1, le=385)
    carry_pause: bool = False
    allow_suspect_tz: bool = False
    sim: Optional[SimSettings] = None
    record_decisions: bool = True


class RunRequest(BaseModel):
    label: str = ""
    from_date: str
    to_date: str
    config: ConfigSource = Field(default_factory=ConfigSource)
    settings: RunSettings = Field(default_factory=RunSettings)
    # Scenario-suite support: when another run is active, queue this one
    # instead of 409ing — the runner starts it automatically when the slot
    # frees (runs execute strictly one at a time, oldest queued first).
    queue: bool = False
    # Provenance of the New-Run form's per-run overrides (display only).
    overrides: Optional[dict[str, Any]] = None


# ── helpers ─────────────────────────────────────────────────────────────────

def _parse_range(req: RunRequest) -> tuple[_date, _date]:
    try:
        frm = _date.fromisoformat(req.from_date)
        to = _date.fromisoformat(req.to_date)
    except ValueError as e:
        raise HTTPException(400, f"invalid date range: {e}") from e
    if frm > to:
        raise HTTPException(400, "from_date must be on or before to_date")
    if (to - frm).days > 400:
        raise HTTPException(400, "range too large (max ~13 months)")
    return frm, to


async def _resolve_config(src: ConfigSource) -> tuple[AlgoConfig, Optional[int]]:
    store = get_config_store()
    if src.source == "live":
        cv = await store.get_live()
        return cv.config.model_copy(deep=True), cv.version
    if src.source == "version":
        if src.version is None:
            raise HTTPException(400, "config.version required for source=version")
        cv = await store.get_version(src.version)
        if cv is None:
            raise HTTPException(404, f"config version {src.version} not found")
        return cv.config.model_copy(deep=True), cv.version
    if src.document is None:
        raise HTTPException(400, "config.document required for source=inline")
    try:
        return AlgoConfig.model_validate(src.document), None
    except Exception as e:
        raise HTTPException(400, f"invalid config document: {e}") from e


def _apply_sim(cfg: AlgoConfig, s: RunSettings) -> AlgoConfig:
    """Apply the per-run simulation knobs onto the frozen document."""
    if s.sim is not None:
        if s.sim.slippage_pct is not None:
            cfg.global_.paper.slippage_pct = float(s.sim.slippage_pct)
        if s.sim.brokerage_per_order is not None:
            cfg.global_.fees.brokerage_per_order = float(s.sim.brokerage_per_order)
    return cfg


def _pin_paper(cfg: AlgoConfig) -> AlgoConfig:
    """Backtests always fill on the paper simulator: ledger derives from
    paper_mode, and shadow/reconcile must stay inert."""
    cfg.global_.paper.paper_mode = True
    cfg.global_.paper.shadow_mode = False
    return cfg


def _snapshot_market(cfg: AlgoConfig, settings: dict[str, Any]) -> dict[str, Any]:
    """Freeze lot sizes + strike steps into the run so a resume months later
    still sizes identically (historical lot-size changes are an accepted,
    documented approximation)."""
    reg = get_registry()
    symbols = {d.index_symbol for d in cfg.days.values()}
    lot_sizes: dict[str, int] = {}
    strike_steps: dict[str, int] = {}
    for sym in symbols:
        entry = reg.get(sym)
        if entry is None:
            # Fail loud at run creation — the old silent 75/50 fallback could
            # size every trade of a run with the wrong contract math.
            raise HTTPException(
                400,
                f"index symbol {sym!r} is not in the symbol registry "
                "(data/symbols.json) — cannot determine its lot size",
            )
        lot_sizes[sym] = entry.lot_size
        strike_steps[sym] = entry.strike_step
    settings["lot_sizes"] = lot_sizes
    settings["strike_steps"] = strike_steps
    return settings


async def _run_or_404(run_id: int, include_config: bool = False) -> dict[str, Any]:
    run = await bt_store.get_run(run_id, include_config)
    if run is None:
        raise HTTPException(404, f"backtest run {run_id} not found")
    return run


def _allocated_for_run(cfg: AlgoConfig, settings: dict[str, Any], day_key: str) -> float:
    d = cfg.days.get(day_key)  # type: ignore[arg-type]
    if d is None:
        return 0.0
    balance = float(
        settings.get("starting_balance") or cfg.global_.paper.virtual_balance
    )
    pct = 100.0 if d.all_in else d.allocation_pct
    return balance * pct / 100


def _summary_rows(trades: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    agg: dict[str, dict[str, Any]] = {}
    for t in trades:
        if t["exit_ts"] is None:
            continue
        g = str(t[key])
        a = agg.setdefault(
            g, {"group": g, "trades": 0, "wins": 0, "losses": 0,
                "pnl": 0.0, "gross_win": 0.0, "gross_loss": 0.0},
        )
        pnl = t["pnl_rupees"] or 0.0
        a["trades"] += 1
        a["wins"] += 1 if pnl > 0 else 0
        a["losses"] += 1 if pnl <= 0 else 0
        a["pnl"] = round(a["pnl"] + pnl, 2)
        a["gross_win"] = round(a["gross_win"] + max(pnl, 0.0), 2)
        a["gross_loss"] = round(a["gross_loss"] + max(-pnl, 0.0), 2)
    return sorted(agg.values(), key=lambda x: x["group"])


# ── sandbox config (the standalone workspace's editable document) ───────────
#
# Lives OUTSIDE algo_config_versions on purpose: nothing done in the
# Backtesting workspace may ever change what the live orchestrator trades.
# Runs launched from it freeze the document (source=inline), so old runs stay
# reproducible as the sandbox evolves.

class SandboxPut(BaseModel):
    config: dict[str, Any]


class SandboxCopy(BaseModel):
    source: Literal["live", "version"] = "live"
    version: Optional[int] = None


async def _seed_sandbox_if_absent(username: str) -> dict[str, Any]:
    row = await bt_store.get_sandbox()
    if row is not None:
        return row
    cv = await get_config_store().get_live()
    doc = _pin_paper(cv.config.model_copy(deep=True)).model_dump(by_alias=True, mode="json")
    await bt_store.put_sandbox(doc, updated_by=f"{username} (seeded from live v{cv.version})")
    return await bt_store.get_sandbox()  # type: ignore[return-value]


@router.get("/sandbox")
async def sandbox_get(ident: AdminIdentity = Depends(require_admin)) -> dict[str, Any]:
    return await _seed_sandbox_if_absent(ident.username)


@router.put("/sandbox")
async def sandbox_put(
    body: SandboxPut, ident: AdminIdentity = Depends(require_editor)
) -> dict[str, Any]:
    try:
        cfg = AlgoConfig.model_validate(body.config)
    except Exception as e:
        raise HTTPException(400, {"errors": [f"invalid document: {e}"]}) from e
    _pin_paper(cfg)
    errors, warnings = validate_document(cfg)
    if errors:
        raise HTTPException(400, {"errors": errors})
    old = await bt_store.get_sandbox()
    new_doc = cfg.model_dump(by_alias=True, mode="json")
    changes = config_diff(old["config"], new_doc) if old else []
    await bt_store.put_sandbox(new_doc, updated_by=ident.username)
    await audit(
        "backtest_sandbox_save",
        user_id=ident.user_id,
        username=ident.username,
        scope="backtest/sandbox",
        detail={"changed_fields": len(changes),
                "paths": [p for p, _, _ in changes[:40]]},
    )
    return {"status": "saved", "changed_fields": len(changes), "warnings": warnings}


@router.post("/sandbox/copy")
async def sandbox_copy(
    body: SandboxCopy, ident: AdminIdentity = Depends(require_editor)
) -> dict[str, Any]:
    cfg, version = await _resolve_config(
        ConfigSource(source=body.source, version=body.version)
    )
    _pin_paper(cfg)
    await bt_store.put_sandbox(
        cfg.model_dump(by_alias=True, mode="json"), updated_by=ident.username
    )
    await audit(
        "backtest_sandbox_copy",
        user_id=ident.user_id,
        username=ident.username,
        scope="backtest/sandbox",
        detail={"source": body.source, "version": version},
    )
    return {"status": "copied", "from_version": version}


# ── coverage ────────────────────────────────────────────────────────────────

@router.get("/coverage")
async def coverage(_: AdminIdentity = Depends(require_admin)) -> dict[str, Any]:
    """True stored-data bounds per symbol — what the date pickers clamp to.

    Exists because the Backtesting UI had NO way to learn this: the From date
    was the hardcoded string "2026-02-05" and the To date was
    ``new Date().toISOString()`` (UTC, so before 05:30 IST it rendered
    yesterday). Both now come from here, so the range extends itself the
    moment a new day lands.

    Reads ``oi_day_stats`` only — never a DISTINCT over ``oi_snapshots_unified``
    (the 2026-08-11 pool-exhaustion shape), because a browser tab polls this.
    """
    from ..services.data_health import coverage as _coverage

    try:
        return await _coverage()
    except Exception as e:
        # Matview missing (migration not yet applied) must degrade to a usable
        # answer, not a 500 that blanks the whole run form.
        log.warning("algo.backtest.coverage_failed", error=str(e))
        from ..core.time_utils import now_ist

        return {
            "server_today_ist": now_ist().date().isoformat(),
            "last_trading_day": None,
            "stats_refreshed_at": None,
            "stale": True,
            "bounds": {"min": None, "max": None},
            "default_from": None,
            "default_to": None,
            "symbols": {},
            "error": "coverage unavailable — run `alembic upgrade head`",
        }


# ── preflight ───────────────────────────────────────────────────────────────

@router.post("/preflight")
async def preflight(
    req: RunRequest, _: AdminIdentity = Depends(require_admin)
) -> dict[str, Any]:
    frm, to = _parse_range(req)
    cfg, _version = await _resolve_config(req.config)
    _pin_paper(cfg)
    _apply_sim(cfg, req.settings)
    pf_settings = req.settings.model_dump()
    pf_settings["platform_holidays"] = sorted(known_holidays())
    report = await run_preflight(cfg, pf_settings, frm, to)
    return report.as_dict()


# ── run lifecycle ───────────────────────────────────────────────────────────

@router.post("/runs")
async def create_run(
    req: RunRequest, ident: AdminIdentity = Depends(require_editor)
) -> dict[str, Any]:
    frm, to = _parse_range(req)
    cfg, version = await _resolve_config(req.config)
    _pin_paper(cfg)
    _apply_sim(cfg, req.settings)
    errors, warnings = validate_document(cfg)
    if errors:
        raise HTTPException(400, {"errors": errors})

    slot_busy = bt_runner.active_run_id() is not None
    if slot_busy and not req.queue:
        raise HTTPException(
            409,
            "another backtest run is already active — pass queue:true (or use "
            "the suite button) to line this one up behind it",
        )

    settings = _snapshot_market(cfg, req.settings.model_dump())
    # Concretize the default here so every downstream consumer (runner,
    # summary, equity endpoint, allocation basis) sees one number.
    if not settings.get("starting_balance"):
        settings["starting_balance"] = cfg.global_.paper.virtual_balance
    if req.overrides:
        settings["overrides"] = req.overrides
    # Frozen for resume determinism: the platform holiday file as of now.
    settings["platform_holidays"] = sorted(known_holidays())
    # What the simulation actually ran with (display: the Settings-used card).
    settings["sim_effective"] = {
        "slippage_pct": cfg.global_.paper.slippage_pct,
        "fill_source": cfg.global_.paper.fill_source,
        "latency_ms_ignored": cfg.global_.paper.latency_ms,
        "fees": cfg.global_.fees.model_dump(),
        "overnight_carry": cfg.global_.overnight_carry,
        "config_holidays": len(cfg.global_.holidays),
        "platform_holidays": len(settings["platform_holidays"]),
    }
    run_id = await bt_store.create_run(
        label=req.label,
        created_by=ident.username,
        from_date=frm,
        to_date=to,
        config=cfg.model_dump(by_alias=True, mode="json"),
        config_version=version,
        settings=settings,
    )
    await audit(
        "backtest_run_create",
        user_id=ident.user_id,
        username=ident.username,
        scope=f"backtest/{run_id}",
        detail={"label": req.label, "from": req.from_date, "to": req.to_date,
                "config_version": version, "source": req.config.source,
                "queued": slot_busy},
    )
    if slot_busy:
        # Row stays 'queued'; the runner auto-starts it when the slot frees.
        return {
            "id": run_id,
            "status": "queued",
            "config_version": version,
            "warnings": warnings,
        }
    try:
        await bt_runner.start_run(run_id)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    return {
        "id": run_id,
        "status": "running",
        "config_version": version,
        "warnings": warnings,
    }


@router.get("/runs")
async def list_runs(
    limit: int = Query(default=50, ge=1, le=200),
    _: AdminIdentity = Depends(require_admin),
) -> list[dict[str, Any]]:
    live_ids = set()
    active = bt_runner.active_run_id()
    if active is not None:
        live_ids.add(active)
    orphaned = await bt_store.fail_orphaned_runs(live_ids)
    if orphaned:
        log.warning("algo.backtest.orphans_failed", run_ids=orphaned)
    runs = await bt_store.list_runs(limit)
    for r in runs:
        s = r.get("summary") or {}
        r["net_pnl"] = s.get("net_pnl")
        r["trades"] = s.get("trades")
        r.pop("summary", None)
    return runs


@router.get("/runs/{run_id}")
async def run_detail(
    run_id: int,
    include_config: bool = Query(default=False),
    _: AdminIdentity = Depends(require_admin),
) -> dict[str, Any]:
    return await _run_or_404(run_id, include_config)


@router.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: int, ident: AdminIdentity = Depends(require_editor)
) -> dict[str, Any]:
    run = await _run_or_404(run_id)
    if run["status"] not in ("queued", "running"):
        raise HTTPException(409, f"run is {run['status']} — nothing to cancel")
    job = bt_runner.get_job(run_id)
    if job is None:
        # Orphan (process restarted): flip the row directly.
        await bt_store.set_status(
            run_id, "cancelled", error="cancelled (no live job)", stamp_finished=True
        )
    else:
        job.cancel_requested = True
    await audit(
        "backtest_run_cancel", user_id=ident.user_id, username=ident.username,
        scope=f"backtest/{run_id}",
    )
    return {"status": "cancelling"}


@router.post("/runs/{run_id}/resume")
async def resume_run(
    run_id: int, ident: AdminIdentity = Depends(require_editor)
) -> dict[str, Any]:
    run = await _run_or_404(run_id)
    if run["status"] not in ("error", "cancelled"):
        raise HTTPException(409, f"run is {run['status']} — only error/cancelled resume")
    if bt_runner.active_run_id() is not None:
        raise HTTPException(409, "another backtest run is already active")
    await bt_store.set_status(run_id, "queued")
    try:
        await bt_runner.start_run(run_id)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    await audit(
        "backtest_run_resume", user_id=ident.user_id, username=ident.username,
        scope=f"backtest/{run_id}",
    )
    return {"id": run_id, "status": "running"}


@router.post("/runs/{run_id}/delete")
async def delete_run(
    run_id: int, ident: AdminIdentity = Depends(require_editor)
) -> dict[str, Any]:
    run = await _run_or_404(run_id)
    if run["status"] in ("queued", "running") and bt_runner.get_job(run_id) is not None:
        raise HTTPException(409, "cancel the run before deleting it")
    await bt_store.delete_run(run_id)
    await audit(
        "backtest_run_delete", user_id=ident.user_id, username=ident.username,
        scope=f"backtest/{run_id}", detail={"label": run.get("label", "")},
    )
    return {"status": "deleted"}


# ── read models ─────────────────────────────────────────────────────────────

@router.get("/runs/{run_id}/days")
async def run_days(
    run_id: int, _: AdminIdentity = Depends(require_admin)
) -> list[dict[str, Any]]:
    await _run_or_404(run_id)
    return await bt_store.run_days(run_id)


@router.get("/runs/{run_id}/trades")
async def run_trades(
    run_id: int,
    date: Optional[str] = Query(default=None),
    zone: str = Query(default=""),
    limit: int = Query(default=2000, ge=1, le=10000),
    _: AdminIdentity = Depends(require_admin),
) -> list[dict[str, Any]]:
    await _run_or_404(run_id)
    return await bt_store.run_trades(
        run_id, trade_date=date, zone=zone.upper() if zone else "", limit=limit
    )


@router.get("/runs/{run_id}/decisions")
async def run_decisions(
    run_id: int,
    day: Optional[str] = Query(default=None),
    compact: bool = Query(default=False),
    _: AdminIdentity = Depends(require_admin),
) -> list[dict[str, Any]]:
    """Per-minute decision trace of a run (algo_backtest_decisions)."""
    await _run_or_404(run_id)
    return await bt_store.run_decisions(run_id, trade_date=day, compact=compact)


@router.get("/runs/{run_id}/decisions/at")
async def run_decision_at(
    run_id: int,
    day: str = Query(...),
    ts: str = Query(..., description="HH:MM IST (the simulated minute boundary)"),
    _: AdminIdentity = Depends(require_admin),
) -> dict[str, Any]:
    """The decision row at/just before ``day ts`` — the replay playhead."""
    from datetime import datetime as _dt

    await _run_or_404(run_id)
    try:
        d = _date.fromisoformat(day)
        hh, mm = (int(p) for p in ts.split(":"))
    except ValueError as e:
        raise HTTPException(400, f"invalid day/ts {day!r} {ts!r}") from e
    row = await bt_store.run_decision_at(run_id, d, _dt(d.year, d.month, d.day, hh, mm))
    if row is None:
        raise HTTPException(404, "no decision at or before that minute")
    return row


@router.get("/runs/{run_id}/pnl/summary")
async def run_pnl_summary(
    run_id: int, _: AdminIdentity = Depends(require_admin)
) -> dict[str, Any]:
    """PnlSummary-shaped, computed over the run's trades with the RUN's
    config/starting balance as the §12.2 allocation basis."""
    run = await _run_or_404(run_id, include_config=True)
    cfg = AlgoConfig.model_validate(run["config"])
    settings = run.get("settings") or {}
    trades = await bt_store.run_trades(run_id, limit=10000)

    by_day = _summary_rows(trades, "day")
    by_zone = _summary_rows(trades, "zone_id")
    by_date = _summary_rows(trades, "trade_date")

    n = sum(r["trades"] for r in by_date)
    wins = sum(r["wins"] for r in by_date)
    pnl = round(sum(r["pnl"] for r in by_date) * 100) / 100
    gross_win = sum(r["gross_win"] for r in by_date)
    gross_loss = sum(r["gross_loss"] for r in by_date)

    total_allocated = 0.0
    for r in by_day:
        alloc = _allocated_for_run(cfg, settings, r["group"])
        r["allocated"] = round(alloc * 100) / 100
        r["pnl_pct"] = round(r["pnl"] / alloc * 10000) / 100 if alloc > 0 else None
        total_allocated += alloc

    # §7: the run summary's day-close equity drawdown, passed through as-is.
    run_summary = await bt_store.summarize(run_id)

    return {
        "max_drawdown": run_summary.get("max_drawdown"),
        "max_drawdown_pct": run_summary.get("max_drawdown_pct"),
        "max_drawdown_from": run_summary.get("max_drawdown_from"),
        "max_drawdown_to": run_summary.get("max_drawdown_to"),
        "drawdown_basis": "run equity curve (day-close equity, peak-to-trough, % of peak equity)",
        "ledger": "backtest",
        "from_date": run["from_date"],
        "to_date": run["to_date"],
        "trades": n,
        "wins": wins,
        "losses": n - wins,
        "win_rate": round(wins / n * 1000) / 10 if n else None,
        "pnl": pnl,
        "avg_win": round(gross_win / wins * 100) / 100 if wins else None,
        "avg_loss": round(-gross_loss / (n - wins) * 100) / 100 if n - wins else None,
        "profit_factor": round(gross_win / gross_loss * 100) / 100 if gross_loss > 0 else None,
        "pnl_pct_blended": (
            round(pnl / total_allocated * 10000) / 100 if total_allocated > 0 else None
        ),
        "by_day": by_day,
        "by_zone": by_zone,
        "by_date": by_date,
    }


@router.get("/runs/{run_id}/pnl/calendar")
async def run_pnl_calendar(
    run_id: int,
    month: str = Query(..., description="YYYY-MM"),
    _: AdminIdentity = Depends(require_admin),
) -> dict[str, Any]:
    run = await _run_or_404(run_id, include_config=True)
    cfg = AlgoConfig.model_validate(run["config"])
    settings = run.get("settings") or {}
    try:
        year, mon = map(int, month.split("-"))
        first = _date(year, mon, 1)
    except ValueError as e:
        raise HTTPException(400, f"invalid month {month!r}") from e
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)

    trades = await bt_store.run_trades(run_id, limit=10000)
    rows = [
        r for r in _summary_rows(trades, "trade_date")
        if first.isoformat() <= r["group"] <= last.isoformat()
    ]
    days: dict[str, dict[str, Any]] = {}
    for r in rows:
        d = _date.fromisoformat(r["group"])
        day_key = _WEEKDAYS[d.weekday()] if d.weekday() < 5 else ""
        alloc = _allocated_for_run(cfg, settings, day_key) if day_key else 0.0
        days[r["group"]] = {
            "pnl": r["pnl"],
            "pnl_pct": round(r["pnl"] / alloc * 10000) / 100 if alloc > 0 else None,
            "trades": r["trades"],
        }
    return {"month": month, "ledger": "backtest", "days": days}


@router.get("/runs/{run_id}/equity")
async def run_equity(
    run_id: int, _: AdminIdentity = Depends(require_admin)
) -> dict[str, Any]:
    run = await _run_or_404(run_id)
    settings = run.get("settings") or {}
    days = await bt_store.run_days(run_id)
    points = []
    for d in days:
        if d["status"] != "done":
            continue
        det = d.get("detail") or {}
        points.append(
            {
                "date": d["trade_date"],
                "equity": det.get("equity_after"),
                "day_pnl": det.get("net", 0),
            }
        )
    return {
        "mode": settings.get("balance_mode", "compounding"),
        "starting_balance": settings.get("starting_balance"),
        "points": points,
    }


@router.get("/runs/{run_id}/day/{day_date}")
async def run_day_bundle(
    run_id: int, day_date: str, _: AdminIdentity = Depends(require_admin)
) -> dict[str, Any]:
    """The one-shot replay bundle: everything the day scrubber needs, so a
    playhead move is pure client-side subtraction — never a network call."""
    run = await _run_or_404(run_id, include_config=True)
    try:
        d = _date.fromisoformat(day_date)
    except ValueError as e:
        raise HTTPException(400, f"invalid date {day_date!r}") from e
    day_rows = {x["trade_date"]: x for x in await bt_store.run_days(run_id)}
    row = day_rows.get(day_date)
    if row is None:
        raise HTTPException(404, f"day {day_date} is not part of run {run_id}")

    weekday = _WEEKDAYS[d.weekday()] if d.weekday() < 5 else ""
    cfg = AlgoConfig.model_validate(run["config"])
    day_cfg = cfg.days.get(weekday)  # type: ignore[arg-type]
    zones = []
    if day_cfg is not None:
        for zid, z in day_cfg.zones.items():
            zones.append(
                {
                    "zone_id": zid,
                    "start": z.start,
                    "end": z.end,
                    "premium_min": z.premium_min,
                    "premium_max": z.premium_max,
                    "max_trades": z.max_trades,
                    "reeval_cadence": z.reeval_cadence,
                    "enabled_indicators": list(z.enabled_indicators),
                    "zone_kill": z.zone_kill,
                }
            )

    signals = await bt_store.run_signals(run_id, day_date)
    ump_by_seq: dict[str, Any] = {}
    status_timeline: list[dict[str, Any]] = []
    alerts: list[dict[str, Any]] = []
    indicator_signals: list[dict[str, Any]] = []
    for s in signals:
        kind = s["indicator"]
        if kind == "ump_capture":
            ump_by_seq[s["reading"]] = s["payload"]
        elif kind == "status":
            status_timeline.append(
                {"ts": s["ts"], "state": s["reading"], "zone": s["zone_id"],
                 "direction": (s["payload"] or {}).get("direction", "")}
            )
        elif kind == "notify":
            alerts.append(
                {"ts": s["ts"], "key": s["reading"],
                 "text": (s["payload"] or {}).get("text", "")}
            )
        else:
            indicator_signals.append(
                {"ts": s["ts"], "zone_id": s["zone_id"], "indicator": kind,
                 "reading": s["reading"], "payload": s["payload"]}
            )

    trades = await bt_store.run_trades(run_id, trade_date=day_date)
    for t in trades:
        t["ump"] = ump_by_seq.get(str(t["id"]))

    decisions_index = await bt_store.run_decisions(run_id, trade_date=day_date, compact=True)
    from ..core.time_utils import SESSION_OPEN_MIN, session_close_min

    minutes_per_day = session_close_min(d) - SESSION_OPEN_MIN

    detail = row.get("detail") or {}
    day_net = float(detail.get("net", 0) or 0)
    equity_after = detail.get("equity_after")
    day_start = (
        round(float(equity_after) - day_net, 2) if equity_after is not None else None
    )
    eq_points = []
    running = day_start or 0.0
    for t in sorted([t for t in trades if t["exit_ts"]], key=lambda x: x["exit_ts"]):
        running = round(running + (t["pnl_rupees"] or 0), 2)
        eq_points.append({"ts": t["exit_ts"], "trade_seq": t["id"], "equity": running})

    return {
        "run_id": run_id,
        "trade_date": day_date,
        "day": weekday,
        "symbol": detail.get("symbol"),
        "expiry": detail.get("expiry"),
        "status": row["status"],
        "skip_reason": row["skip_reason"],
        "spotless": bool(detail.get("spotless")),
        "forced_eod_close": bool(detail.get("forced_eod_close")),
        "minutes": detail.get("minutes"),
        "config_version": run.get("config_version"),
        "zones": zones,
        "minutes_per_day": minutes_per_day,
        "gaps": detail.get("gaps") or [],
        "status_timeline": status_timeline,
        "signals": indicator_signals,
        "alerts": alerts,
        "decisions_index": decisions_index,
        "trades": trades,
        "equity": {
            "day_start": day_start,
            "day_end": equity_after,
            "points": eq_points,
        },
    }
