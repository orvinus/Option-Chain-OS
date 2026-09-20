"""Per-minute decision trace persistence for the LIVE orchestrator.

One row per evaluated minute (and the candidate strikes inside it) — the
filter-by-filter record that ``algo_signals`` (transitions only) and the
in-memory ``OrchestratorStatus`` never provided. Backtest rows go to
``algo_backtest_decisions`` through ``backtest/store.py`` instead, so the
live table never carries simulated rows.

Timestamps follow the ``algo_signals`` convention: the orchestrator's naive
IST boundary is stored unchanged (IST wall clock under a UTC label).
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from typing import Any, AsyncIterator, Optional

import structlog
from sqlalchemy import text

from ..core.db import AsyncSessionLocal

log = structlog.get_logger(__name__)

_INSERT = text(
    """
    INSERT INTO algo_decisions
        (ts, trade_date, day, zone_id, symbol, expiry, ledger, config_version,
         stage, state, direction, unanimous, decision, reason,
         gate_blocks, readings, candidates, sizing, position, zone_snapshot,
         data_age_s, trade_id)
    VALUES
        (:ts, :trade_date, :day, :zone_id, :symbol, :expiry, :ledger, :config_version,
         :stage, :state, :direction, :unanimous, :decision, :reason,
         CAST(:gate_blocks AS JSONB), CAST(:readings AS JSONB), CAST(:candidates AS JSONB),
         CAST(:sizing AS JSONB), CAST(:position AS JSONB), CAST(:zone_snapshot AS JSONB),
         :data_age_s, :trade_id)
    """
)

_COLS = (
    "id, ts, trade_date, day, zone_id, symbol, expiry, ledger, config_version, "
    "stage, state, direction, unanimous, decision, reason, gate_blocks, readings, "
    "candidates, sizing, position, zone_snapshot, data_age_s, trade_id"
)
_COMPACT_COLS = (
    "id, ts, trade_date, day, zone_id, symbol, stage, state, direction, unanimous, "
    "decision, reason, data_age_s, trade_id, "
    "COALESCE(jsonb_array_length(candidates), 0) AS n_candidates"
)

_FETCH = text(
    f"""
    SELECT {_COLS} FROM algo_decisions
    WHERE trade_date = :trade_date
      AND (:zone_id = '' OR zone_id = :zone_id)
      AND (CAST(:since AS TIMESTAMPTZ) IS NULL OR ts > :since)
    ORDER BY ts DESC, id DESC
    LIMIT :limit
    """
)
_FETCH_COMPACT = text(
    f"""
    SELECT {_COMPACT_COLS} FROM algo_decisions
    WHERE trade_date = :trade_date
      AND (:zone_id = '' OR zone_id = :zone_id)
      AND (CAST(:since AS TIMESTAMPTZ) IS NULL OR ts > :since)
    ORDER BY ts DESC, id DESC
    LIMIT :limit
    """
)
_LATEST = text(f"SELECT {_COLS} FROM algo_decisions ORDER BY ts DESC, id DESC LIMIT 1")
# §6 export stream: a date RANGE, oldest first, full (non-compact) columns.
_ITER = text(
    f"""
    SELECT {_COLS} FROM algo_decisions
    WHERE trade_date >= CAST(:from_date AS DATE)
      AND trade_date <= CAST(:to_date AS DATE)
      AND (:zone_id = '' OR zone_id = :zone_id)
    ORDER BY ts, id
    """
)
_PRUNE = text("DELETE FROM algo_decisions WHERE trade_date < :cutoff")


def to_params(row: dict[str, Any]) -> dict[str, Any]:
    """Normalise an orchestrator decision dict into INSERT parameters
    (JSON columns serialised with ``default=str`` so dates/decimals never
    raise inside a fire-and-forget task)."""
    def j(v: Any) -> Optional[str]:
        return None if v is None else json.dumps(v, default=str, sort_keys=True)

    return {
        "ts": row["ts"],
        "trade_date": row["trade_date"],
        "day": row.get("day", ""),
        "zone_id": row.get("zone_id", ""),
        "symbol": row.get("symbol", ""),
        "expiry": row.get("expiry"),
        "ledger": row.get("ledger", ""),
        "config_version": row.get("config_version"),
        "stage": row.get("stage", ""),
        "state": row.get("state", ""),
        "direction": row.get("direction", ""),
        "unanimous": row.get("unanimous"),
        "decision": row.get("decision", "reject"),
        "reason": (row.get("reason") or "")[:500],
        "gate_blocks": j(row.get("gate_blocks")),
        "readings": j(row.get("readings")),
        "candidates": j(row.get("candidates")),
        "sizing": j(row.get("sizing")),
        "position": j(row.get("position")),
        "zone_snapshot": j(row.get("zone_snapshot")),
        "data_age_s": row.get("data_age_s"),
        "trade_id": row.get("trade_id"),
    }


async def insert_live(row: dict[str, Any]) -> None:
    try:
        async with AsyncSessionLocal() as s:
            await s.execute(_INSERT, to_params(row))
            await s.commit()
    except Exception as e:  # noqa: BLE001 — tracing must never break a pass
        log.warning("algo.decisions.insert_failed", error=str(e))


def _out(r: Any) -> dict[str, Any]:
    d = dict(r)
    for k in ("ts",):
        if d.get(k) is not None and hasattr(d[k], "isoformat"):
            d[k] = d[k].isoformat()
    for k in ("trade_date", "expiry"):
        if d.get(k) is not None and hasattr(d[k], "isoformat"):
            d[k] = d[k].isoformat()
    return d


async def fetch_live(
    trade_date: date, zone_id: str = "", *, limit: int = 400,
    compact: bool = False, since: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    sql = _FETCH_COMPACT if compact else _FETCH
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(
                sql, {"trade_date": trade_date, "zone_id": zone_id or "",
                      "since": since, "limit": max(1, min(int(limit), 2000))},
            )
        ).mappings().all()
    return [_out(r) for r in rows]


async def iter_live(
    from_date: date, to_date: date, zone_id: str = ""
) -> AsyncIterator[dict[str, Any]]:
    """Server-side-cursor stream of every live decision row in the range —
    always the FULL row (the export ignores ``compact``)."""
    async with AsyncSessionLocal() as s:
        result = await s.stream(
            _ITER.execution_options(yield_per=500),
            {"from_date": from_date, "to_date": to_date, "zone_id": zone_id or ""},
        )
        async for r in result.mappings():
            yield _out(r)


async def latest() -> Optional[dict[str, Any]]:
    async with AsyncSessionLocal() as s:
        r = (await s.execute(_LATEST)).mappings().first()
    return _out(r) if r else None


async def prune_live(older_than_days: int = 120) -> int:
    from datetime import timedelta

    from ..core.time_utils import now_ist

    cutoff = now_ist().date() - timedelta(days=older_than_days)
    async with AsyncSessionLocal() as s:
        res = await s.execute(_PRUNE, {"cutoff": cutoff})
        await s.commit()
        return res.rowcount or 0


# Fire-and-forget writer for the orchestrator deps. The loop holds only weak
# refs to tasks, so keep strong refs until done (same pattern as audit_event).
_tasks: set[asyncio.Task] = set()


def record_decision_live(row: dict[str, Any]) -> None:
    try:
        t = asyncio.get_running_loop().create_task(insert_live(row))
        _tasks.add(t)
        t.add_done_callback(_tasks.discard)
    except RuntimeError:
        pass
