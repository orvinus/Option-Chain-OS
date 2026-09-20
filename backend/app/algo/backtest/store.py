"""Persistence for backtest runs (algo_backtest_* tables, migration 0010).

House style mirrors trade_store.py: text SQL + AsyncSessionLocal, JSON via
json.dumps, ISO serialization at the edge. Deleting a run is ONE statement —
the child tables cascade.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, AsyncIterator, Optional

from sqlalchemy import text

from ...core.db import AsyncSessionLocal
from ..drawdown import scan_drawdown

# ── runs ─────────────────────────────────────────────────────────────────────

_INSERT_RUN = text(
    """
    INSERT INTO algo_backtest_runs
        (label, created_by, from_date, to_date, config, config_version, settings)
    VALUES
        (:label, :created_by, :from_date, :to_date,
         CAST(:config AS JSONB), :config_version, CAST(:settings AS JSONB))
    RETURNING id
    """
)

_RUN_COLS = (
    "id, label, created_by, created_at, from_date, to_date, config_version, "
    "settings, status, days_total, days_done, cursor_date, started_at, "
    "finished_at, summary, error"
)

_GET_RUN = text(f"SELECT {_RUN_COLS}, config FROM algo_backtest_runs WHERE id = :id")
_LIST_RUNS = text(
    f"SELECT {_RUN_COLS} FROM algo_backtest_runs ORDER BY id DESC LIMIT :limit"
)
_ACTIVE_RUNS = text(
    "SELECT id FROM algo_backtest_runs WHERE status = 'running'"
)
_OLDEST_QUEUED = text(
    "SELECT id FROM algo_backtest_runs WHERE status = 'queued' ORDER BY id LIMIT 1"
)


def _iso(v: Any) -> Any:
    if isinstance(v, datetime) or isinstance(v, date):
        return v.isoformat()
    return v


def _run_row(r: dict[str, Any], include_config: bool = False) -> dict[str, Any]:
    out = {
        "id": int(r["id"]),
        "label": r["label"],
        "created_by": r["created_by"],
        "created_at": _iso(r["created_at"]),
        "from_date": _iso(r["from_date"]),
        "to_date": _iso(r["to_date"]),
        "config_version": r["config_version"],
        "settings": r["settings"] or {},
        "status": r["status"],
        "days_total": int(r["days_total"]),
        "days_done": int(r["days_done"]),
        "cursor_date": _iso(r["cursor_date"]),
        "started_at": _iso(r["started_at"]),
        "finished_at": _iso(r["finished_at"]),
        "summary": r["summary"],
        "error": r["error"],
    }
    if include_config:
        out["config"] = r["config"]
    return out


async def create_run(
    *,
    label: str,
    created_by: str,
    from_date: date,
    to_date: date,
    config: dict[str, Any],
    config_version: Optional[int],
    settings: dict[str, Any],
) -> int:
    async with AsyncSessionLocal() as s:
        row = (
            await s.execute(
                _INSERT_RUN,
                {
                    "label": label,
                    "created_by": created_by,
                    "from_date": from_date,
                    "to_date": to_date,
                    "config": json.dumps(config, default=str),
                    "config_version": config_version,
                    "settings": json.dumps(settings, default=str),
                },
            )
        ).scalar_one()
        await s.commit()
        return int(row)


async def get_run(run_id: int, include_config: bool = False) -> Optional[dict[str, Any]]:
    async with AsyncSessionLocal() as s:
        r = (await s.execute(_GET_RUN, {"id": run_id})).mappings().first()
    return _run_row(dict(r), include_config) if r else None


async def list_runs(limit: int = 50) -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(_LIST_RUNS, {"limit": limit})).mappings().all()
    return [_run_row(dict(r)) for r in rows]


async def set_status(
    run_id: int,
    status: str,
    *,
    error: str = "",
    summary: Optional[dict[str, Any]] = None,
    stamp_started: bool = False,
    stamp_finished: bool = False,
) -> None:
    sets = ["status = :status", "error = :error"]
    params: dict[str, Any] = {"id": run_id, "status": status, "error": error}
    if summary is not None:
        sets.append("summary = CAST(:summary AS JSONB)")
        params["summary"] = json.dumps(summary, default=str)
    if stamp_started:
        sets.append("started_at = now()")
    if stamp_finished:
        sets.append("finished_at = now()")
    async with AsyncSessionLocal() as s:
        await s.execute(
            text(f"UPDATE algo_backtest_runs SET {', '.join(sets)} WHERE id = :id"),
            params,
        )
        await s.commit()


async def merge_summary(run_id: int, patch: dict[str, Any]) -> None:
    """Merge keys into runs.summary (preflight lands here before day 1)."""
    async with AsyncSessionLocal() as s:
        await s.execute(
            text(
                "UPDATE algo_backtest_runs SET summary = "
                "COALESCE(summary, '{}'::jsonb) || CAST(:patch AS JSONB) "
                "WHERE id = :id"
            ),
            {"id": run_id, "patch": json.dumps(patch, default=str)},
        )
        await s.commit()


async def update_progress(
    run_id: int, *, days_total: Optional[int] = None,
    days_done: Optional[int] = None, cursor_date: Optional[date] = None,
) -> None:
    sets = []
    params: dict[str, Any] = {"id": run_id}
    if days_total is not None:
        sets.append("days_total = :days_total")
        params["days_total"] = days_total
    if days_done is not None:
        sets.append("days_done = :days_done")
        params["days_done"] = days_done
    if cursor_date is not None:
        sets.append("cursor_date = :cursor_date")
        params["cursor_date"] = cursor_date
    if not sets:
        return
    async with AsyncSessionLocal() as s:
        await s.execute(
            text(f"UPDATE algo_backtest_runs SET {', '.join(sets)} WHERE id = :id"),
            params,
        )
        await s.commit()


async def active_run_ids() -> list[int]:
    async with AsyncSessionLocal() as s:
        return [int(r[0]) for r in (await s.execute(_ACTIVE_RUNS)).all()]


async def oldest_queued_run() -> Optional[int]:
    """Next run in the scenario-suite queue (queued rows legitimately have no
    in-process job — they wait for the running one to finish)."""
    async with AsyncSessionLocal() as s:
        rid = (await s.execute(_OLDEST_QUEUED)).scalar()
    return int(rid) if rid is not None else None


async def fail_orphaned_runs(live_job_ids: set[int]) -> list[int]:
    """A 'running' row with no in-process job means the process died mid-run.
    Flip it to a resumable error instead of showing an eternal spinner.
    (Queued rows are NOT orphans — they wait their turn.)"""
    orphaned = [rid for rid in await active_run_ids() if rid not in live_job_ids]
    for rid in orphaned:
        await set_status(
            rid, "error",
            error="interrupted by a backend restart — resume to continue",
            stamp_finished=True,
        )
    return orphaned


async def delete_run(run_id: int) -> None:
    async with AsyncSessionLocal() as s:
        await s.execute(
            text("DELETE FROM algo_backtest_runs WHERE id = :id"), {"id": run_id}
        )
        await s.commit()


# ── per-day rows ─────────────────────────────────────────────────────────────

_UPSERT_DAY = text(
    """
    INSERT INTO algo_backtest_days (run_id, trade_date, status, skip_reason, detail)
    VALUES (:run_id, :trade_date, :status, :skip_reason, CAST(:detail AS JSONB))
    ON CONFLICT (run_id, trade_date) DO UPDATE
        SET status = EXCLUDED.status,
            skip_reason = EXCLUDED.skip_reason,
            detail = EXCLUDED.detail
    """
)
_RUN_DAYS = text(
    """
    SELECT trade_date, status, skip_reason, detail
    FROM algo_backtest_days WHERE run_id = :run_id ORDER BY trade_date
    """
)


async def upsert_day(
    run_id: int, trade_date: date, status: str,
    skip_reason: str = "", detail: Optional[dict[str, Any]] = None,
) -> None:
    async with AsyncSessionLocal() as s:
        await s.execute(
            _UPSERT_DAY,
            {
                "run_id": run_id,
                "trade_date": trade_date,
                "status": status,
                "skip_reason": skip_reason,
                "detail": json.dumps(detail, default=str) if detail is not None else None,
            },
        )
        await s.commit()


async def run_days(run_id: int) -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(_RUN_DAYS, {"run_id": run_id})).mappings().all()
    return [
        {
            "trade_date": r["trade_date"].isoformat(),
            "status": r["status"],
            "skip_reason": r["skip_reason"],
            "detail": r["detail"],
        }
        for r in rows
    ]


# ── trades ───────────────────────────────────────────────────────────────────

_INSERT_TRADE = text(
    """
    INSERT INTO algo_backtest_trades
        (run_id, seq, trade_date, day, zone_id, index_symbol, side, token,
         strike, expiry, entry_ts, entry_price, exit_ts, exit_price, lots,
         pnl_rupees, pnl_pct, exit_reason, ledger, sub_scenario, fees)
    VALUES
        (:run_id, :seq, :trade_date, :day, :zone_id, :index_symbol, :side,
         :token, :strike, :expiry, :entry_ts, :entry_price, :exit_ts,
         :exit_price, :lots, :pnl_rupees, :pnl_pct, :exit_reason, :ledger,
         :sub_scenario, CAST(:fees AS JSONB))
    """
)
_DELETE_DAY_TRADES = text(
    "DELETE FROM algo_backtest_trades WHERE run_id = :run_id AND trade_date = :trade_date"
)
_DELETE_DAY_SIGNALS = text(
    "DELETE FROM algo_backtest_signals WHERE run_id = :run_id AND trade_date = :trade_date"
)
_RUN_TRADES = text(
    """
    SELECT seq, trade_date, day, zone_id, index_symbol, side, token, strike,
           expiry, entry_ts, entry_price, exit_ts, exit_price, lots,
           pnl_rupees, pnl_pct, exit_reason, ledger, sub_scenario, fees
    FROM algo_backtest_trades
    WHERE run_id = :run_id
      AND (CAST(:trade_date AS DATE) IS NULL OR trade_date = CAST(:trade_date AS DATE))
      AND (:zone = '' OR zone_id = :zone)
    ORDER BY trade_date, entry_ts, seq
    LIMIT :limit
    """
)


async def bulk_insert_trades(run_id: int, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    async with AsyncSessionLocal() as s:
        for t in rows:
            await s.execute(
                _INSERT_TRADE,
                {
                    "run_id": run_id,
                    "seq": t["seq"],
                    "trade_date": t["trade_date"],
                    "day": t["day"],
                    "zone_id": t["zone_id"],
                    "index_symbol": t["index_symbol"],
                    "side": t["side"],
                    "token": t.get("token", ""),
                    "strike": t.get("strike"),
                    "expiry": t.get("expiry"),
                    "entry_ts": t["entry_ts"],
                    "entry_price": t["entry_price"],
                    "exit_ts": t.get("exit_ts"),
                    "exit_price": t.get("exit_price"),
                    "lots": t["lots"],
                    "pnl_rupees": t.get("pnl_rupees"),
                    "pnl_pct": t.get("pnl_pct"),
                    "exit_reason": t.get("exit_reason", ""),
                    "ledger": t.get("ledger", "paper"),
                    "sub_scenario": t.get("sub_scenario", ""),
                    "fees": json.dumps(t["fees"], default=str)
                    if t.get("fees") is not None
                    else None,
                },
            )
        await s.commit()


_REOPEN_CARRIED_EXITS = text(
    """
    UPDATE algo_backtest_trades
    SET exit_ts = NULL, exit_price = NULL, pnl_rupees = NULL,
        pnl_pct = NULL, exit_reason = '', fees = NULL
    WHERE run_id = :run_id AND trade_date < :trade_date
      AND exit_ts IS NOT NULL
      AND (exit_ts AT TIME ZONE 'UTC')::date = :trade_date
    """
)


_DELETE_DAY_DECISIONS = text(
    "DELETE FROM algo_backtest_decisions WHERE run_id = :run_id AND trade_date = :trade_date"
)
_INSERT_DECISION = text(
    """
    INSERT INTO algo_backtest_decisions
        (run_id, ts, trade_date, day, zone_id, symbol, expiry, ledger, config_version,
         stage, state, direction, unanimous, decision, reason,
         gate_blocks, readings, candidates, sizing, position, zone_snapshot,
         data_age_s, trade_id)
    VALUES
        (:run_id, :ts, :trade_date, :day, :zone_id, :symbol, :expiry, :ledger, :config_version,
         :stage, :state, :direction, :unanimous, :decision, :reason,
         CAST(:gate_blocks AS JSONB), CAST(:readings AS JSONB), CAST(:candidates AS JSONB),
         CAST(:sizing AS JSONB), CAST(:position AS JSONB), CAST(:zone_snapshot AS JSONB),
         :data_age_s, :trade_id)
    """
)
_RUN_DECISIONS = text(
    """
    SELECT id, ts, trade_date, day, zone_id, symbol, expiry, ledger, config_version,
           stage, state, direction, unanimous, decision, reason, gate_blocks, readings,
           candidates, sizing, position, zone_snapshot, data_age_s, trade_id
    FROM algo_backtest_decisions
    WHERE run_id = :run_id
      AND (CAST(:trade_date AS DATE) IS NULL OR trade_date = CAST(:trade_date AS DATE))
    ORDER BY ts, id
    """
)
_RUN_DECISIONS_COMPACT = text(
    """
    SELECT id, ts, trade_date, day, zone_id, symbol, stage, state, direction, unanimous,
           decision, reason, data_age_s, trade_id,
           COALESCE(jsonb_array_length(candidates), 0) AS n_candidates
    FROM algo_backtest_decisions
    WHERE run_id = :run_id
      AND (CAST(:trade_date AS DATE) IS NULL OR trade_date = CAST(:trade_date AS DATE))
    ORDER BY ts, id
    """
)
_RUN_DECISION_AT = text(
    """
    SELECT id, ts, trade_date, day, zone_id, symbol, expiry, ledger, config_version,
           stage, state, direction, unanimous, decision, reason, gate_blocks, readings,
           candidates, sizing, position, zone_snapshot, data_age_s, trade_id
    FROM algo_backtest_decisions
    WHERE run_id = :run_id AND trade_date = :trade_date AND ts <= :ts
    ORDER BY ts DESC, id DESC
    LIMIT 1
    """
)


async def bulk_insert_decisions(run_id: int, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    from ..decisions import to_params

    async with AsyncSessionLocal() as s:
        for r in rows:
            await s.execute(_INSERT_DECISION, {"run_id": run_id, **to_params(r)})
        await s.commit()


def _decision_out(r: Any) -> dict[str, Any]:
    d = dict(r)
    for k in ("ts", "trade_date", "expiry"):
        if d.get(k) is not None and hasattr(d[k], "isoformat"):
            d[k] = d[k].isoformat()
    return d


async def run_decisions(
    run_id: int, trade_date: Optional[str] = None, *, compact: bool = False
) -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(
                _RUN_DECISIONS_COMPACT if compact else _RUN_DECISIONS,
                {"run_id": run_id, "trade_date": trade_date},
            )
        ).mappings().all()
    return [_decision_out(r) for r in rows]


async def run_decision_at(run_id: int, trade_date: date, ts: datetime) -> Optional[dict[str, Any]]:
    async with AsyncSessionLocal() as s:
        r = (
            await s.execute(_RUN_DECISION_AT, {"run_id": run_id, "trade_date": trade_date, "ts": ts})
        ).mappings().first()
    return _decision_out(r) if r else None


async def delete_day_outputs(run_id: int, trade_date: date) -> None:
    """Idempotent re-run of a partial day (resume path). Besides dropping the
    day's own rows, RE-OPEN any prior-day (carried) trade whose exit was
    booked on this day — the re-run re-adopts it and re-books the identical
    exit, so interrupt+resume stays byte-deterministic."""
    async with AsyncSessionLocal() as s:
        await s.execute(_DELETE_DAY_TRADES, {"run_id": run_id, "trade_date": trade_date})
        await s.execute(_DELETE_DAY_SIGNALS, {"run_id": run_id, "trade_date": trade_date})
        await s.execute(_DELETE_DAY_DECISIONS, {"run_id": run_id, "trade_date": trade_date})
        await s.execute(_REOPEN_CARRIED_EXITS, {"run_id": run_id, "trade_date": trade_date})
        await s.commit()


_UPDATE_TRADE_EXIT = text(
    """
    UPDATE algo_backtest_trades
    SET exit_ts = :exit_ts, exit_price = :exit_price,
        pnl_rupees = :pnl_rupees, pnl_pct = :pnl_pct,
        exit_reason = :exit_reason, fees = CAST(:fees AS JSONB)
    WHERE run_id = :run_id AND seq = :seq
    """
)


async def update_trade_exit(run_id: int, t: dict[str, Any]) -> None:
    """Book the exit half of a CARRIED trade (row inserted on its entry day,
    exit fires on a later simulated day)."""
    async with AsyncSessionLocal() as s:
        await s.execute(
            _UPDATE_TRADE_EXIT,
            {
                "run_id": run_id,
                "seq": t["seq"],
                "exit_ts": t.get("exit_ts"),
                "exit_price": t.get("exit_price"),
                "pnl_rupees": t.get("pnl_rupees"),
                "pnl_pct": t.get("pnl_pct"),
                "exit_reason": t.get("exit_reason", ""),
                "fees": json.dumps(t["fees"], default=str)
                if t.get("fees") is not None
                else None,
            },
        )
        await s.commit()


def _trade_row(r: dict[str, Any]) -> dict[str, Any]:
    """Identical serialization to trade_store.trades_filtered → the frontend
    TradeRow type renders backtest trades with zero changes. ``id`` carries
    the deterministic in-run seq."""
    d = dict(r)
    d["id"] = int(d.pop("seq"))
    d["trade_date"] = d["trade_date"].isoformat()
    d["expiry"] = d["expiry"].isoformat() if d["expiry"] else None
    d["entry_ts"] = d["entry_ts"].isoformat()
    d["exit_ts"] = d["exit_ts"].isoformat() if d["exit_ts"] else None
    for k in ("entry_price", "exit_price", "pnl_rupees", "pnl_pct"):
        d[k] = float(d[k]) if d[k] is not None else None
    return d


async def run_trades(
    run_id: int,
    *,
    trade_date: Optional[str] = None,
    zone: str = "",
    limit: int = 2000,
) -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(
                _RUN_TRADES,
                {
                    "run_id": run_id,
                    "trade_date": trade_date,
                    "zone": zone,
                    "limit": max(1, min(limit, 10000)),
                },
            )
        ).mappings().all()
    return [_trade_row(dict(r)) for r in rows]


async def prior_closed_trades(run_id: int) -> list[dict[str, Any]]:
    """Raw rows for ledger re-seeding on resume (dates as date objects)."""
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT seq, trade_date, day, zone_id, index_symbol, side, "
                    "token, strike, expiry, entry_ts, entry_price, exit_ts, "
                    "exit_price, lots, pnl_rupees, pnl_pct, exit_reason, ledger, "
                    "sub_scenario, fees "
                    "FROM algo_backtest_trades WHERE run_id = :run_id "
                    "ORDER BY seq"
                ),
                {"run_id": run_id},
            )
        ).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("entry_price", "exit_price", "pnl_rupees", "pnl_pct"):
            d[k] = float(d[k]) if d[k] is not None else None
        out.append(d)
    return out


# ── signals / events ─────────────────────────────────────────────────────────

_INSERT_SIGNAL = text(
    """
    INSERT INTO algo_backtest_signals
        (run_id, ts, trade_date, day, zone_id, indicator, reading, payload)
    VALUES
        (:run_id, :ts, :trade_date, :day, :zone_id, :indicator, :reading,
         CAST(:payload AS JSONB))
    """
)
_RUN_SIGNALS = text(
    """
    SELECT ts, trade_date, day, zone_id, indicator, reading, payload
    FROM algo_backtest_signals
    WHERE run_id = :run_id
      AND (CAST(:trade_date AS DATE) IS NULL OR trade_date = CAST(:trade_date AS DATE))
    ORDER BY ts, id
    """
)


async def bulk_insert_signals(run_id: int, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    async with AsyncSessionLocal() as s:
        for r in rows:
            await s.execute(
                _INSERT_SIGNAL,
                {
                    "run_id": run_id,
                    "ts": r["ts"],
                    "trade_date": r["trade_date"],
                    "day": r["day"],
                    "zone_id": r.get("zone_id", ""),
                    "indicator": r["indicator"],
                    "reading": r["reading"],
                    "payload": json.dumps(r["payload"], default=str)
                    if r.get("payload") is not None
                    else None,
                },
            )
        await s.commit()


async def run_signals(
    run_id: int, trade_date: Optional[str] = None
) -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(
                _RUN_SIGNALS, {"run_id": run_id, "trade_date": trade_date}
            )
        ).mappings().all()
    return [
        {
            "ts": r["ts"].isoformat(),
            "trade_date": r["trade_date"].isoformat(),
            "day": r["day"],
            "zone_id": r["zone_id"],
            "indicator": r["indicator"],
            "reading": r["reading"],
            "payload": r["payload"],
        }
        for r in rows
    ]


# ── §6 export streams (server-side cursors, oldest first, no cap) ───────────

_ITER_RUN_TRADES = text(
    """
    SELECT seq, trade_date, day, zone_id, index_symbol, side, token, strike,
           expiry, entry_ts, entry_price, exit_ts, exit_price, lots,
           pnl_rupees, pnl_pct, exit_reason, ledger, sub_scenario, fees
    FROM algo_backtest_trades
    WHERE run_id = :run_id
      AND trade_date >= CAST(:from_date AS DATE)
      AND trade_date <= CAST(:to_date AS DATE)
    ORDER BY trade_date, entry_ts, seq
    """
)
_ITER_RUN_DECISIONS = text(
    """
    SELECT id, ts, trade_date, day, zone_id, symbol, expiry, ledger, config_version,
           stage, state, direction, unanimous, decision, reason, gate_blocks, readings,
           candidates, sizing, position, zone_snapshot, data_age_s, trade_id
    FROM algo_backtest_decisions
    WHERE run_id = :run_id
      AND trade_date >= CAST(:from_date AS DATE)
      AND trade_date <= CAST(:to_date AS DATE)
      AND (:zone = '' OR zone_id = :zone)
    ORDER BY ts, id
    """
)
_ITER_RUN_SIGNALS = text(
    """
    SELECT id, ts, trade_date, day, zone_id, indicator, reading, payload
    FROM algo_backtest_signals
    WHERE run_id = :run_id
      AND trade_date >= CAST(:from_date AS DATE)
      AND trade_date <= CAST(:to_date AS DATE)
      AND (:zone = '' OR zone_id = :zone)
      AND (:indicator = '' OR indicator = :indicator)
    ORDER BY ts, id
    """
)


async def iter_run_trades(
    run_id: int, *, from_date: str, to_date: str
) -> AsyncIterator[dict[str, Any]]:
    async with AsyncSessionLocal() as s:
        result = await s.stream(
            _ITER_RUN_TRADES.execution_options(yield_per=500),
            {"run_id": run_id, "from_date": from_date, "to_date": to_date},
        )
        async for r in result.mappings():
            yield _trade_row(dict(r))


async def iter_run_decisions(
    run_id: int, *, from_date: str, to_date: str, zone: str = ""
) -> AsyncIterator[dict[str, Any]]:
    async with AsyncSessionLocal() as s:
        result = await s.stream(
            _ITER_RUN_DECISIONS.execution_options(yield_per=500),
            {"run_id": run_id, "from_date": from_date, "to_date": to_date, "zone": zone},
        )
        async for r in result.mappings():
            yield _decision_out(r)


async def iter_run_signals(
    run_id: int, *, from_date: str, to_date: str, zone: str = "", indicator: str = ""
) -> AsyncIterator[dict[str, Any]]:
    async with AsyncSessionLocal() as s:
        result = await s.stream(
            _ITER_RUN_SIGNALS.execution_options(yield_per=500),
            {
                "run_id": run_id, "from_date": from_date, "to_date": to_date,
                "zone": zone, "indicator": indicator,
            },
        )
        async for r in result.mappings():
            yield {
                "id": int(r["id"]),
                "ts": r["ts"].isoformat(),
                "trade_date": r["trade_date"].isoformat(),
                "day": r["day"],
                "zone_id": r["zone_id"],
                "indicator": r["indicator"],
                "reading": r["reading"],
                "payload": r["payload"],
            }


# ── sandbox config (the workspace's editable document) ──────────────────────

_GET_SANDBOX = text(
    "SELECT config, updated_by, updated_at FROM algo_backtest_sandbox WHERE name = :name"
)
_UPSERT_SANDBOX = text(
    """
    INSERT INTO algo_backtest_sandbox (name, config, updated_by, updated_at)
    VALUES (:name, CAST(:config AS JSONB), :updated_by, now())
    ON CONFLICT (name) DO UPDATE
        SET config = EXCLUDED.config,
            updated_by = EXCLUDED.updated_by,
            updated_at = now()
    """
)


async def get_sandbox(name: str = "sandbox") -> Optional[dict[str, Any]]:
    async with AsyncSessionLocal() as s:
        r = (await s.execute(_GET_SANDBOX, {"name": name})).mappings().first()
    if r is None:
        return None
    return {
        "config": r["config"],
        "updated_by": r["updated_by"],
        "updated_at": _iso(r["updated_at"]),
    }


async def put_sandbox(
    config: dict[str, Any], *, updated_by: str, name: str = "sandbox"
) -> None:
    async with AsyncSessionLocal() as s:
        await s.execute(
            _UPSERT_SANDBOX,
            {
                "name": name,
                "config": json.dumps(config, default=str),
                "updated_by": updated_by,
            },
        )
        await s.commit()


# ── broker account snapshot (last-good payload for offline gateways) ────────

_GET_BROKER_SNAPSHOT = text(
    "SELECT payload, fetched_at FROM algo_broker_snapshot WHERE id = 1"
)
_UPSERT_BROKER_SNAPSHOT = text(
    """
    INSERT INTO algo_broker_snapshot (id, payload, fetched_at)
    VALUES (1, CAST(:payload AS JSONB), now())
    ON CONFLICT (id) DO UPDATE
        SET payload = EXCLUDED.payload, fetched_at = now()
    """
)


async def get_broker_snapshot() -> Optional[dict[str, Any]]:
    async with AsyncSessionLocal() as s:
        r = (await s.execute(_GET_BROKER_SNAPSHOT)).mappings().first()
    if r is None:
        return None
    return {"payload": r["payload"], "fetched_at": _iso(r["fetched_at"])}


async def put_broker_snapshot(payload: dict[str, Any]) -> None:
    async with AsyncSessionLocal() as s:
        await s.execute(
            _UPSERT_BROKER_SNAPSHOT, {"payload": json.dumps(payload, default=str)}
        )
        await s.commit()


# ── summary ──────────────────────────────────────────────────────────────────

async def summarize(run_id: int) -> dict[str, Any]:
    """Whole-run aggregates: totals, win rate, profit factor, by-day/by-zone/
    by-date tables, day-level equity curve and max drawdown."""
    trades = await run_trades(run_id, limit=10000)
    days = await run_days(run_id)
    run = await get_run(run_id)
    settings = (run or {}).get("settings", {})
    starting = float(settings.get("starting_balance") or 0)

    closed = [t for t in trades if t["exit_ts"] is not None]
    wins = [t for t in closed if (t["pnl_rupees"] or 0) > 0]
    losses = [t for t in closed if (t["pnl_rupees"] or 0) <= 0]
    gross_win = sum(t["pnl_rupees"] or 0 for t in wins)
    gross_loss = sum(-(t["pnl_rupees"] or 0) for t in losses)
    net = sum(t["pnl_rupees"] or 0 for t in closed)
    fees_total = sum(
        float((t.get("fees") or {}).get("total", 0) or 0) for t in closed
    )

    # Exit-day attribution for the calendar/date table (a carried trade's
    # P&L lands on the day it CLOSED); weekday/zone stay entry-attributed.
    for t in closed:
        t["exit_date"] = (t["exit_ts"] or t["trade_date"])[:10]

    def _group(key: str) -> list[dict[str, Any]]:
        agg: dict[str, dict[str, Any]] = {}
        for t in closed:
            g = str(t[key])
            a = agg.setdefault(
                g,
                {"group": g, "trades": 0, "wins": 0, "losses": 0,
                 "pnl": 0.0, "gross_win": 0.0, "gross_loss": 0.0},
            )
            pnl = t["pnl_rupees"] or 0
            a["trades"] += 1
            a["wins"] += 1 if pnl > 0 else 0
            a["losses"] += 1 if pnl <= 0 else 0
            a["pnl"] = round(a["pnl"] + pnl, 2)
            a["gross_win"] = round(a["gross_win"] + max(pnl, 0), 2)
            a["gross_loss"] = round(a["gross_loss"] + max(-pnl, 0), 2)
        return sorted(agg.values(), key=lambda x: x["group"])

    # Equity curve + max drawdown over completed days. The RUNNER's stored
    # equity_after is authoritative (it resolved the actual starting balance);
    # recomputing from settings here once produced a curve anchored at 0.
    equity_points: list[dict[str, Any]] = []
    for d in days:
        if d["status"] != "done":
            continue
        det = d.get("detail") or {}
        day_pnl = float(det.get("net", 0) or 0)
        eq_after = det.get("equity_after")
        if eq_after is None:
            continue
        equity_points.append(
            {"date": d["trade_date"], "equity": float(eq_after),
             "day_pnl": round(day_pnl, 2)}
        )
    if not starting and equity_points:
        starting = round(equity_points[0]["equity"] - equity_points[0]["day_pnl"], 2)

    # Shared §7 helper (app.algo.drawdown) — the scan it runs is this
    # function's original loop, moved verbatim; tests pin the two equal.
    dd_scan = scan_drawdown(
        ((p["date"], p["equity"]) for p in equity_points), starting=starting
    )
    max_dd = dd_scan.max_dd
    max_dd_pct = dd_scan.max_dd_pct
    dd_from = dd_scan.from_key
    dd_to = dd_scan.to_key

    # Entry funnel aggregate — where direction → hunt → trigger → entry died.
    funnel_keys = (
        "direction_minutes", "hunts_started", "hunts_discarded", "hold_minutes",
        "hunt_minutes", "trigger_armed_minutes", "band_blocked_minutes", "entries",
    )
    funnel_total: dict[str, int] = {k: 0 for k in funnel_keys}
    funnel_days = 0
    days_with_direction = 0
    for d in days:
        f = (d.get("detail") or {}).get("funnel")
        if not isinstance(f, dict):
            continue
        funnel_days += 1
        for k in funnel_keys:
            funnel_total[k] += int(f.get(k, 0) or 0)
        if int(f.get("direction_minutes", 0) or 0) > 0:
            days_with_direction += 1
    funnel_summary = None
    if funnel_days > 0:
        hunts = funnel_total["hunts_started"]
        funnel_summary = {
            **funnel_total,
            "days_counted": funnel_days,
            "days_with_direction": days_with_direction,
            "avg_hunt_life_min": round(funnel_total["hunt_minutes"] / hunts, 1)
            if hunts > 0 else None,
        }

    return {
        "trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "funnel": funnel_summary,
        "win_rate": round(len(wins) / len(closed) * 100, 2) if closed else None,
        "gross_win": round(gross_win, 2),
        "gross_loss": round(gross_loss, 2),
        "net_pnl": round(net, 2),
        "fees_total": round(fees_total, 2),
        "avg_win": round(gross_win / len(wins), 2) if wins else None,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "by_date": _group("exit_date"),
        "by_day": _group("day"),
        "by_zone": _group("zone_id"),
        "starting_balance": starting,
        "final_equity": equity_points[-1]["equity"] if equity_points else starting,
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": max_dd_pct,
        "max_drawdown_from": dd_from,
        "max_drawdown_to": dd_to,
        "equity": equity_points,
        "days_done": sum(1 for d in days if d["status"] == "done"),
        "days_skipped": sum(1 for d in days if d["status"] == "skipped"),
        "forced_eod_closes": sum(
            1 for d in days
            if d["status"] == "done" and (d.get("detail") or {}).get("forced_eod_close")
        ),
        "carried_open_days": sum(
            1 for d in days
            if d["status"] == "done" and (d.get("detail") or {}).get("carried_open")
        ),
        "expiry_force_closes": sum(
            1 for t in closed if t.get("exit_reason") == "EXPIRY_FORCE_CLOSE"
        ),
        "open_at_end": sum(1 for t in trades if t["exit_ts"] is None),
        "spotless_days": sum(
            1 for d in days
            if d["status"] == "done" and (d.get("detail") or {}).get("spotless")
        ),
    }
