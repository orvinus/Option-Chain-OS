"""Algo Config document endpoints (`/api/algo/config*`, `/api/algo/audit`).

All behind the admin session (``require_admin``). The save endpoint is the
commit half of §13's two-step confirmation: the frontend stages the edit,
shows the diff, asks "Are you sure…", and only then POSTs here — one atomic
INSERT of a new version.
"""
from __future__ import annotations

from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ValidationError

from ..algo.auth import AdminIdentity, require_admin, require_editor, role_rank
from ..algo.audit import fetch_audit
from ..algo.config_models import (
    WEEKDAYS,
    ZONE_IDS,
    AlgoConfig,
    validate_document,
    zone_completeness,
)
from ..algo.config_store import ConfigSaveError, get_config_store

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/algo", tags=["algo-config"])


class ConfigEnvelope(BaseModel):
    version: int
    saved_by: str
    saved_at: str
    note: str
    config: dict[str, Any]


class SaveRequest(BaseModel):
    config: dict[str, Any]
    note: str = ""
    # Optimistic concurrency (QA 2026-09-02 H1): the version the client's
    # draft was loaded from. When set and the live version has moved on (a
    # paper reset, a restore, another tab), the save is refused with 409 so a
    # stale draft can never silently overwrite an out-of-page change.
    base_version: Optional[int] = None


class SaveResponse(BaseModel):
    version: int
    warnings: list[str]


class RestoreRequest(BaseModel):
    version: int


class ValidationReport(BaseModel):
    errors: list[str]
    warnings: list[str]
    # §14 runtime safety gate, evaluated per zone: {"monday/Z1": [problems…]}.
    # An empty list means that zone is eligible to trade.
    zone_gate: dict[str, list[str]]


def _kill_state(cfg: AlgoConfig) -> dict[str, Any]:
    """Every kill-authority field (§11.2): master kill, paper/live routing,
    day kills, zone kills — PLUS the fields that are kill switches in effect:
    strategy_active (a per-zone execution lock since 2026-08-18), the
    capital-deployment levers (all_in / allocation %), and End-Exit (the only
    end-of-day close a live position has)."""
    state: dict[str, Any] = {
        "global.master_kill": cfg.global_.master_kill,
        "global.paper.paper_mode": cfg.global_.paper.paper_mode,
    }
    for day, d in cfg.days.items():
        state[f"days.{day}.day_kill"] = d.day_kill
        state[f"days.{day}.all_in"] = d.all_in
        state[f"days.{day}.allocation_pct"] = d.allocation_pct
        state[f"days.{day}.end_exit_enabled"] = d.end_exit_enabled
        for zid, z in d.zones.items():
            state[f"days.{day}.zones.{zid}.zone_kill"] = z.zone_kill
            state[f"days.{day}.zones.{zid}.strategy_active"] = z.strategy_active
    return state


async def _enforce_kill_authority(ident: AdminIdentity, new_cfg: AlgoConfig) -> None:
    """§11.2 — only the admin may flip a kill switch (or the paper/live
    routing, its real-money equivalent). An editor's save touching any of
    those fields is refused wholesale."""
    if role_rank(ident.role) >= 2:
        return
    live = (await get_config_store().get_live()).config
    old_state, new_state = _kill_state(live), _kill_state(new_cfg)
    changed = sorted(
        k for k in set(old_state) | set(new_state)
        if old_state.get(k) != new_state.get(k)
    )
    if changed:
        raise HTTPException(
            403,
            f"Only the admin may change kill switches / paper-live routing "
            f"(§11.2). Blocked fields: {', '.join(changed[:6])}"
            + (" …" if len(changed) > 6 else ""),
        )


def _parse_config(doc: dict[str, Any]) -> AlgoConfig:
    try:
        return AlgoConfig.model_validate(doc)
    except ValidationError as e:
        # Surface pydantic's per-field messages in the same shape as §15 errors
        # so the UI renders both identically.
        msgs = [
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()
        ]
        raise HTTPException(422, {"errors": msgs}) from e


@router.get("/config", response_model=ConfigEnvelope)
async def get_config(_: AdminIdentity = Depends(require_admin)) -> ConfigEnvelope:
    cv = await get_config_store().get_live()
    return ConfigEnvelope(
        version=cv.version,
        saved_by=cv.saved_by,
        saved_at=cv.saved_at,
        note=cv.note,
        config=cv.config.model_dump(by_alias=True),
    )


async def _live_version_now() -> Optional[int]:
    """Direct read (not the store's TTL cache) — the guard must see a save
    that landed a second ago."""
    from sqlalchemy import text

    from ..core.db import AsyncSessionLocal

    async with AsyncSessionLocal() as s:
        v = (await s.execute(text("SELECT MAX(version) FROM algo_config_versions"))).scalar()
    return int(v) if v is not None else None


@router.post("/config", response_model=SaveResponse)
async def save_config(
    body: SaveRequest, ident: AdminIdentity = Depends(require_editor)
) -> SaveResponse:
    cfg = _parse_config(body.config)
    await _enforce_kill_authority(ident, cfg)
    if body.base_version is not None:
        live_version = await _live_version_now()
        if live_version is not None and live_version != body.base_version:
            raise HTTPException(
                409,
                {
                    "errors": [
                        f"config changed elsewhere (now v{live_version}, your draft is "
                        f"from v{body.base_version}) — reload and re-apply your edits"
                    ],
                    "live_version": live_version,
                },
            )
    try:
        cv, warnings = await get_config_store().save(
            cfg, saved_by=ident.username, note=body.note, user_id=ident.user_id
        )
    except ConfigSaveError as e:
        raise HTTPException(422, {"errors": e.errors}) from e
    return SaveResponse(version=cv.version, warnings=warnings)


@router.get("/config/versions")
async def config_versions(
    limit: int = Query(default=25, ge=1, le=200),
    _: AdminIdentity = Depends(require_admin),
) -> list[dict[str, Any]]:
    return await get_config_store().history(limit)


@router.get("/config/versions/{version}", response_model=ConfigEnvelope)
async def config_version(
    version: int, _: AdminIdentity = Depends(require_admin)
) -> ConfigEnvelope:
    cv = await get_config_store().get_version(version)
    if cv is None:
        raise HTTPException(404, f"version {version} does not exist")
    return ConfigEnvelope(
        version=cv.version,
        saved_by=cv.saved_by,
        saved_at=cv.saved_at,
        note=cv.note,
        config=cv.config.model_dump(by_alias=True),
    )


@router.post("/config/restore", response_model=SaveResponse)
async def restore_config(
    body: RestoreRequest, ident: AdminIdentity = Depends(require_editor)
) -> SaveResponse:
    if role_rank(ident.role) < 2:
        target = await get_config_store().get_version(body.version)
        if target is not None:
            await _enforce_kill_authority(ident, target.config)
    try:
        cv, warnings = await get_config_store().restore(
            body.version, saved_by=ident.username, user_id=ident.user_id
        )
    except ConfigSaveError as e:
        raise HTTPException(422, {"errors": e.errors}) from e
    return SaveResponse(version=cv.version, warnings=warnings)


@router.get("/config/defaults")
async def config_defaults(_: AdminIdentity = Depends(require_admin)) -> dict[str, Any]:
    """The Appendix-A reference document — the single source the UI's
    "Reset to Default" actions copy from (day resets, zone resets, per-engine
    resets). Served from code so the frontend never re-implements the seed."""
    from ..algo.config_models import default_config

    return default_config().model_dump(by_alias=True)


@router.get("/config/validation", response_model=ValidationReport)
async def config_validation(
    _: AdminIdentity = Depends(require_admin),
) -> ValidationReport:
    """§15 checklist + the §14 per-zone runtime gate, evaluated against the LIVE
    document — the Validation Summary sub-tab and the orchestrator read the
    same functions, so UI and engine can never disagree about eligibility."""
    from ..algo.broker import get_interactive_client

    cv = await get_config_store().get_live()
    errors, warnings = validate_document(
        cv.config, broker_configured=get_interactive_client().configured
    )
    gate: dict[str, list[str]] = {}
    for day in WEEKDAYS:
        for zid in ZONE_IDS:
            gate[f"{day}/{zid}"] = zone_completeness(cv.config, day, zid)
    return ValidationReport(errors=errors, warnings=warnings, zone_gate=gate)


class ValidationRequest(BaseModel):
    document: dict[str, Any]


@router.post("/config/validation", response_model=ValidationReport)
async def config_validation_document(
    body: ValidationRequest,
    _: AdminIdentity = Depends(require_admin),
) -> ValidationReport:
    """The same §15 + §14 report, evaluated against a POSTED document — how
    the Backtesting workspace validates its SANDBOX draft without saving
    anything anywhere."""
    from ..algo.broker import get_interactive_client
    from ..algo.config_models import AlgoConfig

    try:
        cfg = AlgoConfig.model_validate(body.document)
    except Exception as e:
        return ValidationReport(
            errors=[f"invalid document: {e}"], warnings=[], zone_gate={}
        )
    errors, warnings = validate_document(
        cfg, broker_configured=get_interactive_client().configured
    )
    gate: dict[str, list[str]] = {}
    for day in WEEKDAYS:
        for zid in ZONE_IDS:
            gate[f"{day}/{zid}"] = zone_completeness(cfg, day, zid)
    return ValidationReport(errors=errors, warnings=warnings, zone_gate=gate)


@router.get("/audit")
async def audit_log(
    username: str = Query(default=""),
    since: Optional[str] = Query(default=None),
    until: Optional[str] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    _: AdminIdentity = Depends(require_admin),
) -> list[dict[str, Any]]:
    return await fetch_audit(username=username, since=since, until=until, limit=limit)
