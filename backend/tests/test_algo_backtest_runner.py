"""Backtest runner lifecycle — day loop, progress, cancellation, resume.

Run:  cd backend && PYTHONPATH=. python tests/test_algo_backtest_runner.py

Store and data access are faked in-memory (house style: patch the runner's
module attributes); the ORCHESTRATOR and engines are real. The synthetic
frames produce NO_TRADE readings, so these tests pin the run lifecycle —
entries/exits themselves are pinned by test_algo_orchestrator.py and the
DB-backed golden run (validation/backtest_golden.py).
"""
from __future__ import annotations

import asyncio
import copy
import json
from datetime import date, datetime, timedelta, timezone

from app.algo.backtest import runner as runner_mod
from app.algo.backtest.data import build_day_frame
from app.algo.backtest.preflight import DayPlan, PreflightReport
from app.algo.config_models import default_config


# ── fakes ───────────────────────────────────────────────────────────────────

class FakeStore:
    def __init__(self, run_row):
        self.run_row = run_row
        self.days: dict[str, dict] = {}
        self.trades: list[dict] = []
        self.signals: list[dict] = []
        self.status_calls: list[tuple] = []
        self.progress: dict = {}
        self.summary_patches: list[dict] = []

    async def get_run(self, run_id, include_config=False):
        return copy.deepcopy(self.run_row)

    async def set_status(self, run_id, status, *, error="", summary=None,
                         stamp_started=False, stamp_finished=False):
        self.status_calls.append((status, error))
        self.run_row["status"] = status
        if summary is not None:
            self.run_row["summary"] = summary

    async def merge_summary(self, run_id, patch):
        self.summary_patches.append(patch)

    async def update_progress(self, run_id, *, days_total=None, days_done=None,
                              cursor_date=None):
        if days_total is not None:
            self.progress["days_total"] = days_total
        if days_done is not None:
            self.progress["days_done"] = days_done
        if cursor_date is not None:
            self.progress["cursor_date"] = cursor_date

    async def upsert_day(self, run_id, trade_date, status, skip_reason="", detail=None):
        self.days[trade_date.isoformat()] = {
            "trade_date": trade_date.isoformat(), "status": status,
            "skip_reason": skip_reason, "detail": detail,
        }

    async def run_days(self, run_id):
        return sorted(self.days.values(), key=lambda d: d["trade_date"])

    async def prior_closed_trades(self, run_id):
        return [copy.deepcopy(t) for t in self.trades]

    async def delete_day_outputs(self, run_id, trade_date):
        iso = trade_date.isoformat()
        self.trades = [t for t in self.trades
                       if t["trade_date"].isoformat() != iso]
        self.signals = [s for s in self.signals
                        if s["trade_date"].isoformat() != iso]
        # Mirror the real store: re-open prior-day rows whose exit was booked
        # on the re-run day (carried-trade resume determinism).
        for t in self.trades:
            ex = t.get("exit_ts")
            if ex is not None and ex.date().isoformat() == iso:
                t.update(exit_ts=None, exit_price=None, pnl_rupees=None,
                         pnl_pct=None, exit_reason="", fees=None)

    async def update_trade_exit(self, run_id, t):
        for row in self.trades:
            if row["seq"] == t["seq"]:
                row.update(
                    exit_ts=t.get("exit_ts"), exit_price=t.get("exit_price"),
                    pnl_rupees=t.get("pnl_rupees"), pnl_pct=t.get("pnl_pct"),
                    exit_reason=t.get("exit_reason", ""), fees=t.get("fees"),
                )
                return

    async def bulk_insert_trades(self, run_id, rows):
        self.trades.extend(copy.deepcopy(rows))

    async def bulk_insert_signals(self, run_id, rows):
        self.signals.extend(copy.deepcopy(rows))

    async def summarize(self, run_id):
        return {"trades": len(self.trades)}

    async def oldest_queued_run(self):
        return None


D1 = date(2026, 3, 3)   # Tue
D2 = date(2026, 3, 4)   # Wed


def _frame_for(day: date):
    open_utc = datetime.combine(day, datetime.min.time()).replace(
        hour=3, minute=45, tzinfo=timezone.utc
    )
    chain = []
    for i in range(0, 240):
        for strike in (22400, 22500, 22600):
            for ot in ("CE", "PE"):
                chain.append({
                    "strike": strike, "option_type": ot,
                    "bucket": open_utc + timedelta(minutes=i),
                    "oi": 1000 + i, "o": 100.0, "h": 100.5, "l": 99.5, "c": 100.0,
                })
    return build_day_frame(
        trade_date=day, symbol="NIFTY", expiry=day, open_utc=open_utc,
        chain=chain, spot_rows=[{"bucket": open_utc, "spot": 22500.0}],
        preopen=None, bounds=(22400, 22600), strike_step=50,
    )


def _report(days):
    r = PreflightReport(from_date=days[0].isoformat(), to_date=days[-1].isoformat())
    for d in days:
        r.days.append(DayPlan(
            trade_date=d.isoformat(), symbol="NIFTY", expiry=d.isoformat(),
            minutes=240, planned=True,
        ))
    r.planned = len(days)
    return r


def _run_row(cfg):
    return {
        "id": 1, "label": "t", "from_date": D1.isoformat(), "to_date": D2.isoformat(),
        "config": json.loads(cfg.model_dump_json(by_alias=True)),
        "settings": {"balance_mode": "compounding", "starting_balance": 30000,
                     "lot_sizes": {"NIFTY": 65}, "strike_steps": {"NIFTY": 50}},
        "status": "queued", "summary": None,
    }


def _patched(store, days):
    """Patch the runner module's collaborators; return restore()."""
    orig = (runner_mod.store, runner_mod.load_day_frame, runner_mod.run_preflight)

    async def fake_load(symbol, expiry, trade_date, step):
        return _frame_for(trade_date)

    async def fake_preflight(cfg, settings, from_date, to_date):
        return _report(days)

    runner_mod.store = store
    runner_mod.load_day_frame = fake_load
    runner_mod.run_preflight = fake_preflight

    def restore():
        runner_mod.store, runner_mod.load_day_frame, runner_mod.run_preflight = orig

    return restore


def _canonical(store: FakeStore):
    return json.dumps(
        {"trades": store.trades, "signals": store.signals,
         "days": sorted(store.days.values(), key=lambda d: d["trade_date"])},
        default=str, sort_keys=True,
    )


# ── tests ───────────────────────────────────────────────────────────────────

def test_two_day_run_completes_with_day_rows_and_progress():
    cfg = default_config(today=D1)
    store = FakeStore(_run_row(cfg))
    restore = _patched(store, [D1, D2])
    try:
        asyncio.run(runner_mod._run(1))
    finally:
        restore()
    assert store.run_row["status"] == "done", store.status_calls
    assert store.days[D1.isoformat()]["status"] == "done"
    assert store.days[D2.isoformat()]["status"] == "done"
    assert store.progress == {"days_total": 2, "days_done": 2,
                              "cursor_date": D2}
    d1 = store.days[D1.isoformat()]["detail"]
    assert d1["equity_after"] == 30000.0, "no trades → equity unchanged"
    assert d1["symbol"] == "NIFTY" and d1["trades"] == 0
    states = {s["reading"] for s in store.signals if s["indicator"] == "status"}
    assert "idle" in states, f"status timeline must be captured, got {states}"
    assert any(s["indicator"] == "combined" for s in store.signals), \
        "combined signal transitions must be captured"
    assert store.summary_patches and "preflight" in store.summary_patches[0]


def test_cancellation_between_days():
    cfg = default_config(today=D1)
    store = FakeStore(_run_row(cfg))
    restore = _patched(store, [D1, D2])
    job = runner_mod.BacktestJob(run_id=1)
    runner_mod._jobs[1] = job

    orig_upsert = store.upsert_day

    async def cancel_after_d1(run_id, trade_date, status, skip_reason="", detail=None):
        await orig_upsert(run_id, trade_date, status, skip_reason, detail)
        if trade_date == D1 and status == "done":
            job.cancel_requested = True

    store.upsert_day = cancel_after_d1
    try:
        asyncio.run(runner_mod._run(1))
    finally:
        restore()
        runner_mod._jobs.pop(1, None)
    assert store.run_row["status"] == "cancelled"
    assert D2.isoformat() not in store.days or store.days[D2.isoformat()]["status"] != "done"


def test_resume_determinism_byte_identical():
    cfg = default_config(today=D1)

    # Uninterrupted reference run.
    ref_store = FakeStore(_run_row(cfg))
    restore = _patched(ref_store, [D1, D2])
    try:
        asyncio.run(runner_mod._run(1))
    finally:
        restore()
    reference = _canonical(ref_store)

    # Interrupted: run day 1, "crash", then resume.
    store = FakeStore(_run_row(cfg))
    job = runner_mod.BacktestJob(run_id=1)
    runner_mod._jobs[1] = job
    orig_upsert = store.upsert_day

    async def cancel_after_d1(run_id, trade_date, status, skip_reason="", detail=None):
        await orig_upsert(run_id, trade_date, status, skip_reason, detail)
        if trade_date == D1 and status == "done":
            job.cancel_requested = True

    store.upsert_day = cancel_after_d1
    restore = _patched(store, [D1, D2])
    try:
        asyncio.run(runner_mod._run(1))          # stops after D1
        store.upsert_day = orig_upsert
        job.cancel_requested = False
        asyncio.run(runner_mod._run(1))          # resumes at D2
    finally:
        restore()
        runner_mod._jobs.pop(1, None)
    assert store.run_row["status"] == "done"
    assert _canonical(store) == reference, "resumed run must be byte-identical"


def test_unhandled_exception_marks_error():
    cfg = default_config(today=D1)
    store = FakeStore(_run_row(cfg))
    restore = _patched(store, [D1])

    async def boom(symbol, expiry, trade_date, step):
        raise RuntimeError("frame exploded")

    runner_mod.load_day_frame = boom
    try:
        asyncio.run(runner_mod._run(1))
    finally:
        restore()
    assert store.run_row["status"] == "error"
    assert "frame exploded" in store.status_calls[-1][1]



def test_day_detail_carries_entry_funnel():
    cfg = default_config(today=D1)
    store = FakeStore(_run_row(cfg))
    restore = _patched(store, [D1])
    try:
        asyncio.run(runner_mod._run(1))
    finally:
        restore()
    f = store.days[D1.isoformat()]["detail"]["funnel"]
    for k in ("direction_minutes", "hunts_started", "hunts_discarded",
              "hold_minutes", "hunt_minutes", "trigger_armed_minutes",
              "band_blocked_minutes", "entries"):
        assert k in f, f"funnel missing {k}"
    assert f["entries"] == 0, "synthetic NO_TRADE frames must produce no entries"
    assert f["hunts_started"] >= f["hunts_discarded"] - 1


def test_queued_run_starts_after_active_finishes():
    """The scenario-suite queue: run 1 finishes → the runner auto-starts the
    oldest queued run — even though run 1's own task is still alive while its
    finally-block advances the queue."""
    cfg = default_config(today=D1)

    class QueueStore(FakeStore):
        def __init__(self, rows):
            super().__init__(rows[1])
            self.rows = rows

        async def get_run(self, run_id, include_config=False):
            return copy.deepcopy(self.rows[run_id])

        async def set_status(self, run_id, status, *, error="", summary=None,
                             stamp_started=False, stamp_finished=False):
            self.status_calls.append((run_id, status, error))
            self.rows[run_id]["status"] = status

        async def oldest_queued_run(self):
            for rid, row in sorted(self.rows.items()):
                if row["status"] == "queued":
                    return rid
            return None

    r1 = _run_row(cfg)
    r2 = {**_run_row(cfg), "id": 2, "status": "queued"}
    store = QueueStore({1: r1, 2: r2})
    restore = _patched(store, [D1])

    async def drive():
        await runner_mod._run(1)
        job2 = runner_mod._jobs.get(2)
        assert job2 is not None and job2.task is not None, (
            "run 2 must have been auto-started by the queue advance"
        )
        await job2.task

    try:
        asyncio.run(drive())
    finally:
        restore()
        runner_mod._jobs.pop(1, None)
        runner_mod._jobs.pop(2, None)
    assert store.rows[1]["status"] == "done"
    assert store.rows[2]["status"] == "done", "queued run must complete after run 1"


def _run_all() -> None:
    import traceback

    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except Exception:
                failed += 1
                print(f"FAIL  {name}")
                traceback.print_exc()
    if failed:
        raise SystemExit(f"{failed} test(s) failed")
    print("all backtest runner tests passed")


if __name__ == "__main__":
    _run_all()
