"""The backtest run lifecycle — the live orchestrator stepped through history.

Per planned day: prefetch a DayFrame, build a FRESH ZoneOrchestrator (resets
the per-day alert/reading caches exactly as a restart would), drive
``evaluate_minute`` across every session minute, then flush trades/signals/
status to the run's tables. Cross-day state is ONLY the equity ledger (and,
optionally, the consecutive-loss pause when ``carry_pause`` is set).

Single-flight: one run process-wide — the runner shares the event loop, the
GIL and the DB pool with the LIVE orchestrator and dashboards. The per-minute
``asyncio.sleep(0)`` is mandatory: the in-memory deps never await anything
real, so a day would otherwise block the loop for seconds at a time.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Optional

import structlog

from ...core.tasks import spawn_supervised
from ...core.time_utils import SESSION_OPEN_MIN, session_close_min
from ..config_models import AlgoConfig
from ..orchestrator import ZoneOrchestrator
from ..paper import round_trip_pnl, sell_fill
from . import store
from .data import DayFrame, load_day_frame
from .deps import (
    BacktestDeps,
    EventCollector,
    InMemoryLedger,
    prefetch_official_closes,
    prefetch_premium_minutes,
)
from .preflight import run_preflight

log = structlog.get_logger(__name__)

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")
_FIRST_MINUTE = time(9, 15)
_LAST_MINUTE = time(15, 40)


async def _load_bundle(
    symbol: str, expiry: date, day: date, step: int, cfg: AlgoConfig
):
    """Prefetch payload for one day: the frame plus the batched premium warm
    (both SQL round-trips ride under the PREVIOUS day's minute loop)."""
    frame = await load_day_frame(symbol, expiry, day, step)
    if frame is None:
        return None
    try:
        warm = await prefetch_premium_minutes(frame, cfg)
    except Exception:
        # Non-fatal: deps falls back to per-contract fetches (exact, slower).
        log.warning("algo.backtest.premium_prefetch_failed", day=str(day))
        warm = None
    try:
        eod = await prefetch_official_closes(frame, cfg)
    except Exception:
        log.warning("algo.backtest.eod_prefetch_failed", day=str(day))
        eod = None
    return frame, warm, eod


@dataclass
class BacktestJob:
    run_id: int
    cancel_requested: bool = False
    task: Optional[asyncio.Task] = None
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


_jobs: dict[int, BacktestJob] = {}


def get_job(run_id: int) -> Optional[BacktestJob]:
    return _jobs.get(run_id)


def active_run_id(exclude: Optional[int] = None) -> Optional[int]:
    """The run currently holding the slot. ``exclude`` lets the queue-advance
    hook ignore the run that is FINISHING (its task is still technically
    alive while its own ``finally`` block runs)."""
    for rid, job in _jobs.items():
        if rid == exclude:
            continue
        if job.task is not None and not job.task.done():
            return rid
    return None


async def start_run(run_id: int, *, _finishing: Optional[int] = None) -> None:
    """Spawn the supervised runner task. Raises RuntimeError when another run
    is already active (single-flight). ``_finishing`` is the queue-advance
    hook's own just-ended run (ignored in the check)."""
    active = active_run_id(exclude=_finishing)
    if active is not None:
        raise RuntimeError(f"backtest run #{active} is already running")
    job = BacktestJob(run_id=run_id)
    _jobs[run_id] = job
    # respawn=False: a crashed backtest must never restart itself and
    # double-write rows — the error status + resume endpoint own recovery.
    job.task = spawn_supervised(
        lambda: _run(run_id), f"algo-backtest-{run_id}", respawn=False
    )


async def _run(run_id: int) -> None:
    job = _jobs.get(run_id) or BacktestJob(run_id=run_id)
    _jobs[run_id] = job
    try:
        await _run_inner(run_id, job)
    except asyncio.CancelledError:
        await store.set_status(run_id, "cancelled", stamp_finished=True)
        raise
    except Exception as e:
        log.exception("algo.backtest.run_failed", run_id=run_id)
        await store.set_status(
            run_id, "error", error=f"{type(e).__name__}: {e}", stamp_finished=True
        )
    finally:
        # Scenario-suite queue: whatever way this run ended, hand the slot to
        # the oldest queued run (its own supervised task — we return quickly).
        try:
            await _start_next_queued(after=run_id)
        except Exception:
            log.exception("algo.backtest.queue_advance_failed")


async def _start_next_queued(after: Optional[int] = None) -> None:
    if active_run_id(exclude=after) is not None:
        return
    nxt = await store.oldest_queued_run()
    if nxt is not None:
        log.info("algo.backtest.queue_next", run_id=nxt)
        await start_run(nxt, _finishing=after)


def _frame_gaps(frame: DayFrame, min_len: int = 3) -> list[tuple[str, str, int]]:
    """Runs of ≥``min_len`` consecutive session minutes with no basket tick,
    as (HH:MM, HH:MM, length) — inside the date's own session length."""
    out: list[tuple[str, str, int]] = []
    n = min(frame.minutes_per_day, len(frame.minute_ticks))
    start = None
    for i in range(n + 1):
        empty = i < n and not frame.minute_ticks[i]
        if empty and start is None:
            start = i
        elif not empty and start is not None:
            if i - start >= min_len:
                a, b = SESSION_OPEN_MIN + start, SESSION_OPEN_MIN + i - 1
                out.append((f"{a // 60:02d}:{a % 60:02d}", f"{b // 60:02d}:{b % 60:02d}", i - start))
            start = None
    return out


async def _run_inner(run_id: int, job: BacktestJob) -> None:
    run = await store.get_run(run_id, include_config=True)
    if run is None:
        raise RuntimeError(f"run #{run_id} does not exist")
    cfg = AlgoConfig.model_validate(run["config"])
    # Frozen-config guard (overnight carry): the model field defaults to True
    # on re-parse, but a run frozen BEFORE the feature exists must replay its
    # recorded behavior byte-identically — the RAW stored JSON is the truth.
    # Post-deploy documents always serialize the key explicitly.
    cfg.global_.overnight_carry = bool(
        (run["config"].get("global") or {}).get("overnight_carry", False)
    )
    carry_on = cfg.global_.overnight_carry
    settings: dict[str, Any] = run.get("settings") or {}
    from_date = date.fromisoformat(run["from_date"])
    to_date = date.fromisoformat(run["to_date"])
    carry_pause = bool(settings.get("carry_pause", False))
    balance_mode = settings.get("balance_mode", "compounding")
    starting_balance = float(
        settings.get("starting_balance") or cfg.global_.paper.virtual_balance
    )
    lot_sizes: dict[str, int] = {
        k: int(v) for k, v in (settings.get("lot_sizes") or {}).items()
    }
    strike_steps: dict[str, int] = {
        k: int(v) for k, v in (settings.get("strike_steps") or {}).items()
    }

    await store.set_status(run_id, "running", stamp_started=True)

    # ── preflight → day plan (authoritative re-run at start) ──
    report = await run_preflight(cfg, settings, from_date, to_date)
    await store.merge_summary(run_id, {"preflight": report.as_dict()})

    already_done = {
        d["trade_date"]
        for d in await store.run_days(run_id)
        if d["status"] == "done"
    }
    for plan in report.days:
        if plan.trade_date in already_done:
            continue  # resume: completed days keep their results
        await store.upsert_day(
            run_id,
            date.fromisoformat(plan.trade_date),
            "planned" if plan.planned else "skipped",
            skip_reason=plan.skip_reason,
            detail={
                "symbol": plan.symbol,
                "expiry": plan.expiry,
                "minutes": plan.minutes,
                "spotless": plan.spotless,
            },
        )
    planned_days = [p for p in report.days if p.planned or p.trade_date in already_done]
    await store.update_progress(
        run_id, days_total=len(planned_days), days_done=len(already_done)
    )

    # ── ledger (re-seeded from persisted trades on resume) ──
    # ORDER MATTERS: the resume day's outputs (incl. any carried-trade exit
    # booked on it during a crash window) must be dropped/re-opened BEFORE
    # the seed, or the seeded ledger would show the trade closed and the
    # re-run could never re-adopt and re-book it deterministically.
    first_pending = next(
        (
            date.fromisoformat(p.trade_date)
            for p in report.days
            if p.planned and p.trade_date not in already_done
        ),
        None,
    )
    if first_pending is not None:
        await store.delete_day_outputs(run_id, first_pending)
    ledger = InMemoryLedger(starting_balance, balance_mode)
    prior = await store.prior_closed_trades(run_id)
    if prior:
        ledger.seed_closed(prior)

    equity = starting_balance + sum(
        float(t["pnl_rupees"] or 0) for t in prior if t.get("exit_ts") is not None
    )
    days_done = len(already_done)
    carried_pause = ""

    # ── pipelined prefetch: while a day's minute loop burns CPU, the NEXT
    # day's frame loads in the background (its SQL round-trip vanishes from
    # the wall clock). Frames are only ever consumed in loop order.
    exec_days: list[tuple[date, str, date]] = []
    for p in report.days:
        if not p.planned or p.trade_date in already_done or not p.expiry:
            continue
        d0 = date.fromisoformat(p.trade_date)
        dc0 = cfg.days[_WEEKDAYS[d0.weekday()]]
        exec_days.append((d0, dc0.index_symbol, date.fromisoformat(p.expiry)))

    prefetch: dict[date, asyncio.Task] = {}

    def _prefetch_after(cur: date) -> None:
        # Keep TWO future days in flight: the pipeline is data-load-bound
        # (a day's SQL takes longer than its minute loop), so a single
        # lookahead leaves the loop waiting on the bundle.
        ahead = 0
        for d1, sym1, exp1 in exec_days:
            if d1 > cur:
                if d1 not in prefetch:
                    t = asyncio.create_task(
                        _load_bundle(sym1, exp1, d1, strike_steps.get(sym1, 50), cfg)
                    )
                    # retrieve exceptions so an abandoned task never warns;
                    # awaiting it later still re-raises.
                    t.add_done_callback(
                        lambda t: t.cancelled() or t.exception()
                    )
                    prefetch[d1] = t
                ahead += 1
                if ahead >= 2:
                    return

    _prefetch_after(date.min)

    for plan in report.days:
        if not plan.planned or plan.trade_date in already_done:
            continue
        if job.cancel_requested:
            for t in prefetch.values():
                t.cancel()
            await store.set_status(run_id, "cancelled", stamp_finished=True)
            return

        day = date.fromisoformat(plan.trade_date)
        weekday = _WEEKDAYS[day.weekday()]
        day_cfg = cfg.days[weekday]
        symbol = day_cfg.index_symbol
        expiry = date.fromisoformat(plan.expiry) if plan.expiry else None
        if expiry is None:
            await store.upsert_day(run_id, day, "skipped", skip_reason="no expiry")
            continue

        await store.delete_day_outputs(run_id, day)   # idempotent partial re-run
        await store.update_progress(run_id, cursor_date=day)

        _prefetch_after(day)   # start the NEXT day's load before this day's CPU
        task = prefetch.pop(day, None)
        bundle = (await task) if task is not None else await _load_bundle(
            symbol, expiry, day, strike_steps.get(symbol, 50), cfg
        )
        frame, warm, eod = bundle if bundle is not None else (None, None, None)
        if frame is None:
            await store.upsert_day(
                run_id, day, "skipped", skip_reason="no data at run time",
                detail={"symbol": symbol, "expiry": plan.expiry},
            )
            continue

        collector = EventCollector()
        deps = BacktestDeps(
            cfg=cfg, frame=frame, ledger=ledger, collector=collector,
            lot_sizes=lot_sizes,
            premium_minutes=warm if warm is not None else {},
            _premium_warmed=warm is not None,
            official_closes=eod if eod is not None else {},
            _official_warmed=eod is not None,
            config_version=run.get("config_version"),
            platform_holidays=frozenset(settings.get("platform_holidays") or []),
        )
        orch = ZoneOrchestrator(deps.as_orchestrator_deps())
        if carry_pause:
            orch.paused_reason = carried_pause

        # Overnight carry: a still-open ledger row from a prior simulated day
        # is re-adopted at day start (the SAME code path live uses across a
        # restart — exercised every carried morning, resume-deterministic).
        deps.set_now(datetime.combine(day, _FIRST_MINUTE))
        try:
            await orch.adopt_open_trade(today=day)
        except Exception:
            log.exception("algo.backtest.adopt_failed", run_id=run_id)
        if orch.position is not None and orch.position.contract.expiry != expiry:
            # Calendar edge: the carried contract is off today's frame — its
            # minute bars would never arrive. Direct management data comes
            # from the full-life cache via build_engine, but latest_minute
            # reads the frame; close at the first boundary instead of
            # silently starving the exit ladder.
            pos = orch.position
            ref = pos.engine.last_close if pos.engine.last_close is not None else pos.entry_fill
            fill = sell_fill(cfg.global_.paper, ref).price
            lot = lot_sizes.get(symbol, 0) or 1
            pnl, fees_ = round_trip_pnl(
                cfg.global_.fees, entry_fill=pos.entry_fill, exit_fill=fill,
                lot_size=lot, lots=pos.lots,
            )
            ledger.close(
                pos.trade_id,
                exit_ts=datetime.combine(day, _FIRST_MINUTE),
                exit_price=fill, pnl_rupees=pnl, pnl_pct=None,
                exit_reason="EXPIRY_FORCE_CLOSE", fees=fees_.as_dict(),
            )
            collector.now = datetime.combine(day, _FIRST_MINUTE)
            collector.notify(
                f"off-frame-close-{pos.trade_id}",
                f"⏱ Carried contract off today's frame (exp "
                f"{pos.contract.expiry}) — closed at first boundary.",
            )
            orch.position = None

        engines_by_trade: dict[int, Any] = {}
        prev_trade_id: Optional[int] = None
        prev_hunts: list[Any] = []
        # Entry funnel — the permanent answer to "why so few trades": where
        # in direction → hunt → trigger → entry the day's attempts died.
        funnel = {
            "direction_minutes": 0,
            "hunts_started": 0,
            "hunts_discarded": 0,
            "hold_minutes": 0,
            "hunt_minutes": 0,
            "trigger_armed_minutes": 0,
            "band_blocked_minutes": 0,
        }
        boundary = datetime.combine(day, _FIRST_MINUTE)
        # The date's OWN close (15:40 from 2026-08-03, 15:30 before) — a fixed
        # 15:40 ran ten phantom minutes on pre-change days.
        close_min = session_close_min(day)
        last_boundary = datetime.combine(day, time(close_min // 60, close_min % 60))
        # Gap detection BEFORE execution: runs of ≥3 session minutes with no
        # basket tick. Recorded in the day detail and announced up front —
        # the staleness gate will block entries across them, exactly as live.
        gaps = _frame_gaps(frame)
        if gaps:
            collector.now = boundary
            collector.notify(
                f"gaps-{day.isoformat()}",
                f"⚠️ {day}: {len(gaps)} data gap(s) of ≥3 minutes ("
                + ", ".join(f"{a}–{b} {n}m" for a, b, n in gaps[:5])
                + ") — new entries are blocked while the feed is stale.",
            )
        while boundary <= last_boundary:
            deps.set_now(boundary)
            await orch.evaluate_minute(boundary)
            st = orch.status
            collector.status_transition(
                boundary, st.state, st.active_zone, st.direction
            )
            if st.direction in ("CALL", "PUT"):
                funnel["direction_minutes"] += 1
            if st.state == "hunting_hold":
                funnel["hold_minutes"] += 1
            if st.state == "no_strike_in_band":
                funnel["band_blocked_minutes"] += 1
            cur_hunts = list(orch.hunts)
            if cur_hunts:
                funnel["hunt_minutes"] += 1
                # Set-membership additions: a K-strike spawn counts K starts
                # (identity comparison against live references — safe from
                # id() reuse because prev_hunts keeps them alive).
                for h in cur_hunts:
                    if not any(h is p for p in prev_hunts):
                        funnel["hunts_started"] += 1
                if any(getattr(h.engine, "trig", False) for h in cur_hunts):
                    funnel["trigger_armed_minutes"] += 1
            pos = orch.position
            # Event-level (preserves single-strike-era numbers): one discard
            # event when a live hunt set vanished without a position.
            if prev_hunts and not cur_hunts and pos is None:
                funnel["hunts_discarded"] += 1
            prev_hunts = cur_hunts
            cur_id = pos.trade_id if pos is not None else None
            if pos is not None:
                engines_by_trade[pos.trade_id] = pos.engine
            if prev_trade_id is not None and prev_trade_id != cur_id:
                eng = engines_by_trade.get(prev_trade_id)
                if eng is not None:
                    collector.capture_engine(prev_trade_id, eng)
            prev_trade_id = cur_id
            boundary += timedelta(minutes=1)
            await asyncio.sleep(0)   # mandatory yield — see module docstring

        # ── EOD ──
        # Overnight carry ON: the position RIDES the gap (Pine parity) — the
        # ledger row stays open and tomorrow's fresh orchestrator re-adopts
        # it. The in-loop expiry force-close (15:25) guarantees no position
        # survives its own expiry day — assert that invariant.
        forced_close = False
        carried_open: Optional[dict[str, Any]] = None
        if orch.position is not None and carry_on:
            pos = orch.position
            if pos.contract.expiry is not None and day >= pos.contract.expiry:
                raise RuntimeError(
                    f"expiry-day position #{pos.trade_id} survived the 15:25 "
                    "force-close — invariant broken, run aborted"
                )
            carried_open = {
                "trade_seq": pos.trade_id,
                "strike": pos.contract.strike,
                "option_type": pos.contract.option_type,
                "lots": pos.lots,
                "entry_date": next(
                    (str(t["trade_date"]) for t in ledger.trades
                     if t["seq"] == pos.trade_id), "",
                ),
            }
            collector.now = last_boundary
            collector.notify(
                f"carried-open-{pos.trade_id}",
                f"🌙 Position carried overnight: trade #{pos.trade_id} "
                f"{pos.contract.strike}{pos.contract.option_type} × "
                f"{pos.lots} lot(s) — the exit ladder resumes next session.",
            )
            collector.capture_engine(pos.trade_id, pos.engine)
            orch.position = None      # continuity flows through the ledger row
        elif orch.position is not None:
            forced_close = True
            pos = orch.position
            ref = pos.engine.last_close if pos.engine.last_close is not None else pos.entry_fill
            fill = sell_fill(cfg.global_.paper, ref).price
            lot = lot_sizes.get(symbol, 0)
            if lot < 1:
                # Unreachable (run creation validates the registry and the
                # orchestrator refuses entries at lot<1) — but if it ever
                # fires, failing the run beats silently booking EOD P&L at a
                # fraction of reality.
                raise RuntimeError(
                    f"EOD force-close: no lot size for {symbol!r} — run aborted"
                )
            pnl, fees = round_trip_pnl(
                cfg.global_.fees,
                entry_fill=pos.entry_fill,
                exit_fill=fill,
                lot_size=lot,
                lots=pos.lots,
            )
            balance = ledger.balance_for(day)
            alloc_pct = 100.0 if day_cfg.all_in else day_cfg.allocation_pct
            allocated = balance * alloc_pct / 100
            ledger.close(
                pos.trade_id,
                exit_ts=last_boundary,
                exit_price=fill,
                pnl_rupees=pnl,
                pnl_pct=round(pnl / allocated * 10000) / 100 if allocated > 0 else None,
                exit_reason="EOD_FORCE_CLOSE",
                fees=fees.as_dict(),
            )
            collector.now = last_boundary
            collector.notify(
                f"eod-force-close-{pos.trade_id}",
                f"⏱ EOD force-close: trade #{pos.trade_id} "
                f"{pos.contract.strike}{pos.contract.option_type} @ {fill:.2f} "
                f"(P&L {pnl:+.0f} ₹) — position crossed the session end.",
            )
            collector.capture_engine(pos.trade_id, pos.engine)
            orch.position = None

        carried_pause = orch.paused_reason

        # ── flush the day ──
        # Rows ENTERED today are inserted (open rows persist with NULL exit —
        # the carry mechanism); a CARRIED row that exited today gets its exit
        # half UPDATEd on its entry-day row. Day economics = trades EXITED
        # today (exit-day attribution, matching the live ledger).
        day_rows = ledger.rows_for_day(day)
        closed_rows = [
            t for t in ledger.trades if InMemoryLedger._exit_day(t) == day
        ]
        exited_carried = [t for t in closed_rows if t["trade_date"] != day]
        day_net = round(sum(float(t["pnl_rupees"] or 0) for t in closed_rows), 2)
        day_fees = round(
            sum(float((t.get("fees") or {}).get("total", 0) or 0) for t in closed_rows), 2
        )
        equity = round(equity + day_net, 2)

        await store.bulk_insert_trades(run_id, day_rows)
        for t in exited_carried:
            await store.update_trade_exit(run_id, t)
        signal_rows = list(collector.signals)
        for n in collector.notifications:
            signal_rows.append(
                {
                    "ts": n["ts"], "trade_date": day, "day": weekday,
                    "zone_id": "", "indicator": "notify", "reading": n["key"],
                    "payload": {"text": n["text"]},
                }
            )
        for stt in collector.status_timeline:
            signal_rows.append(
                {
                    "ts": stt["ts"], "trade_date": day, "day": weekday,
                    "zone_id": stt["zone"], "indicator": "status",
                    "reading": stt["state"],
                    "payload": {"direction": stt["direction"]},
                }
            )
        for seq, bundle in collector.ump_captures.items():
            signal_rows.append(
                {
                    "ts": last_boundary, "trade_date": day, "day": weekday,
                    "zone_id": "", "indicator": "ump_capture",
                    "reading": str(seq), "payload": bundle,
                }
            )
        await store.bulk_insert_signals(run_id, signal_rows)
        if settings.get("record_decisions", True):
            # The per-minute decision trace (filter-by-filter record). Store
            # doubles used by the unit suite may not implement it.
            _ins = getattr(store, "bulk_insert_decisions", None)
            if _ins is not None:
                await _ins(run_id, collector.decisions)

        zone_counts: dict[str, int] = {}
        for t in closed_rows:
            zone_counts[t["zone_id"]] = zone_counts.get(t["zone_id"], 0) + 1
        funnel["entries"] = len(day_rows)
        await store.upsert_day(
            run_id, day, "done",
            detail={
                "symbol": symbol,
                "expiry": plan.expiry,
                "minutes": plan.minutes or frame.minutes_with_data,
                "spotless": frame.spotless,
                "forced_eod_close": forced_close,
                "carried_open": carried_open,
                "trades": len(closed_rows),
                "net": day_net,
                "fees": day_fees,
                "gross": round(day_net + day_fees, 2),
                "equity_after": equity,
                "zones": zone_counts,
                "paused_reason": orch.paused_reason,
                "funnel": funnel,
                "gaps": [list(g) for g in gaps],
                "minutes_per_day": frame.minutes_per_day,
            },
        )
        days_done += 1
        await store.update_progress(run_id, days_done=days_done, cursor_date=day)

    for t in prefetch.values():
        t.cancel()

    summary = await store.summarize(run_id)
    summary["preflight"] = report.as_dict()
    await store.set_status(run_id, "done", summary=summary, stamp_finished=True)
    log.info("algo.backtest.done", run_id=run_id, days=days_done)
