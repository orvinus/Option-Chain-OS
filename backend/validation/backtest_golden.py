"""Backtest golden determinism — run twice, byte-identical; interrupt +
resume, still byte-identical.

Run (inside the backend container, DB required):
    cd /app && PYTHONPATH=. python validation/backtest_golden.py [FROM TO]

Defaults to the three most recent archived/live NIFTY days. Uses the CURRENT
live config (paper pinned) — trades may legitimately be zero; the assertion
is determinism of the complete output (trades + signals + day rows +
summary), which holds regardless. All runs it creates are deleted at the end.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import date, timedelta

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from sqlalchemy import text  # noqa: E402

from app.algo.backtest import runner as bt_runner  # noqa: E402
from app.algo.backtest import store as bt_store  # noqa: E402
from app.algo.config_store import get_config_store  # noqa: E402
from app.core.db import AsyncSessionLocal  # noqa: E402
from app.market.symbols import get_registry  # noqa: E402

_RECENT_DAYS_SQL = text(
    """
    SELECT DISTINCT (ts AT TIME ZONE 'Asia/Kolkata')::date AS d
    FROM oi_snapshots_unified
    WHERE symbol = 'NIFTY' AND option_type IN ('CE','PE')
    ORDER BY d DESC LIMIT 6
    """
)


async def _canonical(run_id: int) -> str:
    trades = await bt_store.run_trades(run_id, limit=10000)
    signals = await bt_store.run_signals(run_id)
    days = await bt_store.run_days(run_id)
    summary = await bt_store.summarize(run_id)
    # The per-minute decision trace is part of the determinism contract too
    # (row ids excluded — they are sequence values, not outputs).
    decisions = [
        {k: v for k, v in d.items() if k != "id"}
        for d in await bt_store.run_decisions(run_id)
    ]
    return json.dumps(
        {"trades": trades, "signals": signals, "days": days, "summary": summary,
         "decisions": decisions},
        default=str, sort_keys=True,
    )


async def _mk_run(cfg_doc: dict, settings: dict, frm: date, to: date, label: str) -> int:
    return await bt_store.create_run(
        label=label, created_by="golden", from_date=frm, to_date=to,
        config=cfg_doc, config_version=None, settings=settings,
    )


async def main() -> None:
    if len(sys.argv) >= 3:
        frm, to = date.fromisoformat(sys.argv[1]), date.fromisoformat(sys.argv[2])
    else:
        async with AsyncSessionLocal() as s:
            days = sorted(r[0] for r in (await s.execute(_RECENT_DAYS_SQL)).all())
        frm, to = days[0], days[-1]
    print(f"golden window: {frm} → {to}")

    cv = await get_config_store().get_live()
    cfg = cv.config.model_copy(deep=True)
    cfg.global_.paper.paper_mode = True
    cfg.global_.paper.shadow_mode = False
    cfg_doc = cfg.model_dump(by_alias=True, mode="json")

    reg = get_registry()
    symbols = {d.index_symbol for d in cfg.days.values()}
    settings = {
        "balance_mode": "compounding",
        "starting_balance": cfg.global_.paper.virtual_balance,
        "use_default_exclusions": True,
        "min_minutes_per_day": 200,
        "lot_sizes": {s: (reg.get(s).lot_size if reg.get(s) else 75) for s in symbols},
        "strike_steps": {s: (reg.get(s).strike_step if reg.get(s) else 50) for s in symbols},
    }

    created: list[int] = []
    try:
        # ── run A and run B: identical inputs → identical outputs ──
        # Created JUST-IN-TIME: a pre-created row sits at status 'queued' and
        # the runner's scenario-suite queue-advance would auto-start it behind
        # our back the moment the previous run finishes (double execution).
        a = await _mk_run(cfg_doc, settings, frm, to, "golden-A")
        created.append(a)
        await bt_runner._run(a)
        b = await _mk_run(cfg_doc, settings, frm, to, "golden-B")
        created.append(b)
        await bt_runner._run(b)
        ca, cb = await _canonical(a), await _canonical(b)
        ra, rb = await bt_store.get_run(a), await bt_store.get_run(b)
        assert ra["status"] == "done", f"run A ended {ra['status']}: {ra['error']}"
        assert rb["status"] == "done", f"run B ended {rb['status']}: {rb['error']}"
        assert ca == cb, "two identical runs produced different outputs"
        n_tr = (json.loads(ca)["summary"] or {}).get("trades")
        print(f"PASS  same-input determinism ({ra['days_done']} days, {n_tr} trades)")

        # ── run C: cancel after the first completed day, then resume ──
        c = await _mk_run(cfg_doc, settings, frm, to, "golden-C")
        created.append(c)
        job = bt_runner.BacktestJob(run_id=c)
        bt_runner._jobs[c] = job

        orig_upsert = bt_store.upsert_day

        async def cancel_after_first(run_id, trade_date, status, skip_reason="", detail=None):
            await orig_upsert(run_id, trade_date, status, skip_reason, detail)
            if run_id == c and status == "done":
                job.cancel_requested = True

        bt_store.upsert_day = cancel_after_first
        try:
            await bt_runner._run(c)
        finally:
            bt_store.upsert_day = orig_upsert
        rc = await bt_store.get_run(c)
        done_after_cancel = sum(
            1 for d in await bt_store.run_days(c) if d["status"] == "done"
        )
        assert rc["status"] == "cancelled", (
            f"expected cancelled, got {rc['status']} — the window must contain "
            f"≥2 planned days (only {done_after_cancel} ran); widen FROM/TO"
        )
        print(f"…    interrupted after {done_after_cancel} day(s)")

        job.cancel_requested = False
        await bt_runner._run(c)          # resume
        rc = await bt_store.get_run(c)
        assert rc["status"] == "done", f"resume ended {rc['status']}: {rc['error']}"
        cc = await _canonical(c)
        assert cc == ca, "resumed run differs from the uninterrupted reference"
        print("PASS  interrupt + resume determinism")
    finally:
        for rid in created:
            bt_runner._jobs.pop(rid, None)
            await bt_store.delete_run(rid)
        print(f"cleaned up {len(created)} golden run(s)")

    print("\nall golden checks passed")


if __name__ == "__main__":
    asyncio.run(main())
