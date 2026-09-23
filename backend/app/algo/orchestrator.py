"""The zone orchestrator — where the two layers meet (§2–§7, §14).

One evaluation per CLOSED minute during the session, clock-driven:

    weekday/holiday → master kill → day kill → risk counters → open-trade
    management (exits + End-Exit) → sequencing rule → zone by clock → zone
    gates (kill, §14 completeness, max trades, profit lock, pause) →
    indicator evaluation (per §4.2 cadence) → unanimous-among-enabled rule →
    §5.3 direction lifecycle (idle / discard-on-flip / hunt) → strike-in-band
    selection → Ultra Master Pro entry hunting → position sizing → routing
    (paper simulator now; live broker in the final milestone).

Design rules honoured:

- The engine layer is consulted, never bypassed: entries come ONLY from the
  UmpEngine's events; exits come ONLY from its exit chain or the day-level
  End-Exit (§2.3, owned here because it is a day rule, not an engine rule).
- Once a trade is open the filter layer is NEVER consulted for it (§6): the
  management path runs before, and instead of, any signal evaluation, and the
  sequencing rule (§2.3) keeps later zones from evaluating entries while the
  trade lives.
- The §14 safety gate calls the SAME ``zone_completeness`` the Validation tab
  shows — a blocked zone is alerted once per day, never silent.
- The master kill blocks evaluation and NEW entries; an already-open trade
  keeps being MANAGED (abandoning a live position unmanaged would be worse
  than the kill's intent).
- Restart safety: every counter (trades per zone, realized P&L, consecutive
  losses) derives from the trade ledger, and an open trade resumes by
  deterministically replaying the session through a fresh engine.

Everything external is injected via ``OrchestratorDeps`` so the whole
decision path is testable without a database or feed; the runtime wiring at
the bottom of the module provides the real implementations.
"""
from __future__ import annotations

import asyncio
import time as _perf_time
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Awaitable, Callable, Literal, Optional

import structlog

from ..core.time_utils import IST as _IST_TZ
from .config_models import (
    AlgoConfig,
    DayConfig,
    IndicatorKey,
    Weekday,
    ZoneConfig,
    ZoneId,
    zone_completeness,
)
from .alerts import entry_message, exit_message, kill_message, pause_message, risk_kill_message
from .combine import CombinedDecision, combine_readings
from .engines.ump.engine import UmpEngine
from .paper import buy_fill, round_trip_pnl, sell_fill

log = structlog.get_logger(__name__)

Side = Literal["CALL", "PUT"]

_WEEKDAYS: tuple[Weekday, ...] = ("monday", "tuesday", "wednesday", "thursday", "friday")


@dataclass
class Contract:
    symbol: str
    expiry: date
    strike: int
    option_type: Literal["CE", "PE"]

    @property
    def token(self) -> str:
        return f"{self.symbol}:{self.expiry:%y%m%d}:{self.strike}:{self.option_type}"


@dataclass
class MinuteBar:
    ts: datetime            # IST wall clock, naive, bucket start
    o: float
    h: float
    l: float
    c: float


@dataclass
class IndicatorEval:
    """An indicator evaluation WITH its explanation. ``signal`` is what the
    decision path consumes (unchanged semantics); ``detail`` is the
    JSON-safe trace (rule checks, contributions, model logs, basket, as-of)
    that the per-minute decision record carries. Deps may still return a bare
    string — it is normalised to an eval with an empty detail."""
    signal: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class OrchestratorDeps:
    """Everything the decision core needs from the outside world."""
    get_config: Callable[[], Awaitable[AlgoConfig]]
    evaluate_indicator: Callable[
        [IndicatorKey, Weekday, ZoneId, ZoneConfig, str], Awaitable[Any]
    ]                                       # → "CALL"/"PUT"/"NO_TRADE" or IndicatorEval
    select_strikes: Callable[
        [str, Side, ZoneConfig], Awaitable[list[tuple[Contract, float]]]
    ]                            # → top-N (contract, premium), nearest-mid first
    build_engine: Callable[[Contract, ZoneConfig, bool], Awaitable[Optional[UmpEngine]]]
    latest_minute: Callable[[Contract, datetime], Awaitable[Optional[MinuteBar]]]
    lot_size: Callable[[str], int]
    current_balance: Callable[[AlgoConfig], Awaitable[float]]
    today_closed: Callable[[date, str], Awaitable[list[tuple[str, float]]]]
    insert_trade: Callable[..., Awaitable[int]]
    close_trade: Callable[..., Awaitable[None]]
    record_signal: Callable[..., Awaitable[bool]]
    notify: Callable[[str, str], None]
    # Execution routing (paper simulator now; the live Lakshmishree adapter
    # takes over when paper_mode is off AND the Interactive API is configured).
    # Entry: None = refused → the entry is skipped. Exit: None = the LIVE
    # order failed → the position stays under management and retries.
    execute_entry: Callable[..., Awaitable[Optional[Any]]] = None  # type: ignore[assignment]
    execute_exit: Callable[..., Awaitable[Optional[Any]]] = None  # type: ignore[assignment]
    # Broker-side position audit for LIVE trades: (match, detail) where match
    # None means "cannot verify". Optional — absent in pure-decision tests.
    reconcile_live: Optional[Callable[..., Awaitable[tuple[Optional[bool], str]]]] = None
    # LIVE-runtime-only extras (None in backtests and pure-decision tests, so
    # decision behavior stays byte-identical there):
    #   open_trade_today(date, ledger) → the open ledger row for restart
    #   re-adoption; data_age_s(symbol) → seconds since the freshest stored
    #   option row FOR THE TRADED SYMBOL (async DB probe — symbol-scoped so
    #   another symbol's ticks can never forge freshness), gating NEW entries
    #   and flagging a data stall over an open position.
    open_trade_today: Optional[Callable[..., Awaitable[Optional[Any]]]] = None
    #   open_trade_latest(ledger) → the newest open row of ANY entry date —
    #   the overnight-carry adoption source (a carried position must be
    #   re-adopted the next session, not abandoned to its entry day).
    open_trade_latest: Optional[Callable[..., Awaitable[Optional[Any]]]] = None
    #   today_entries(date, ledger) → zone_ids ENTERED today (open or closed):
    #   the max_trades consumption list. Entry-day attribution — a carried
    #   trade closing today must not eat today's allowance. None → fall back
    #   to the closed list (pre-carry behavior, fine for pure-decision tests).
    today_entries: Optional[Callable[[date, str], Awaitable[list[str]]]] = None
    data_age_s: Optional[Callable[[str], Awaitable[Optional[float]]]] = None
    #   is_platform_holiday(date) → the platform's NSE holiday file says this
    #   date is a market holiday. Unioned with the config's own holiday list so
    #   a stale config can never trade a known holiday. None (backtests/unit
    #   tests) = config list only → decision behavior byte-identical there.
    is_platform_holiday: Optional[Callable[[date], bool]] = None
    #   audit_event(event_type, detail_text, extra) → append-only audit row
    #   for runtime transitions (§11.3: kills, gate blocks, trade fills, risk
    #   events). Sync fire-and-forget; None = not audited (tests/backtest).
    audit_event: Optional[Callable[[str, str, dict], None]] = None
    #   record_decision(row) → the per-minute, per-candidate decision record
    #   (algo_decisions / algo_backtest_decisions). Sync fire-and-forget like
    #   audit_event; None = not recorded (pure-decision tests). Never affects
    #   the decision itself.
    record_decision: Optional[Callable[[dict], None]] = None
    #   config_version() → the version number of the document get_config
    #   returns (None for an inline/sandbox document). Trace metadata only.
    config_version: Optional[Callable[[], Optional[int]]] = None
    #   latest_quote(contract) → (last traded price, tz-aware ts) of that
    #   contract's newest tick. Manual square-off reference only; None in
    #   backtests/tests (falls back to the last closed minute).
    latest_quote: Optional[Callable[[Contract], Awaitable[Optional[tuple[float, datetime]]]]] = None


class ManualOrderError(Exception):
    """A manual test entry / square-off was refused for a stated reason
    (nothing was placed, or the order's outcome is reported in the message)."""


@dataclass
class _Position:
    trade_id: int
    contract: Contract
    zone_id: ZoneId
    day: Weekday
    side: Side
    entry_fill: float
    lots: int
    ledger: str
    engine: UmpEngine
    broker_order_id: str = ""      # empty = paper position
    entry_ts: Optional[datetime] = None   # naive IST minute of the fill (holding time)
    # §8 Shadow Mode — the paper twin of a LIVE trade (id + its paper fill).
    shadow_trade_id: Optional[int] = None
    shadow_entry_fill: float = 0.0
    # Consecutive minutes with no premium bar while this position is open —
    # drives the data-stall alarm (live runtime only).
    stall_minutes: int = 0
    # Latched engine exit decision (price, reason). Once the engine reports an
    # exit, the retry loop works from THIS latch — never from trades[-1],
    # which a re-entering engine could repoint at a phantom trade.
    pending_exit: Optional[tuple[float, str]] = None


class _AdoptedShellEngine:
    """Placeholder engine for an adopted position when the session replay
    could not rebuild the real UMP engine (no data / config drift). Keeps the
    position under management — End-Exit, broker reconcile, sequencing — with
    no engine-driven exits. Never enters."""

    in_trade = False
    sub = "ADOPTED"
    trail_sl: Optional[float] = None
    max_sl: Optional[float] = None
    last_close: Optional[float] = None
    entry_guard = None

    def __init__(self) -> None:
        self.trades: list[Any] = []

    def process_minute(self, ts: Any, o: float, h: float, l: float, c: float) -> None:
        self.last_close = c


@dataclass
class _Hunt:
    contract: Contract
    zone_id: ZoneId
    side: Side
    engine: UmpEngine
    started: Optional[datetime] = None   # for the direction-hold window


@dataclass
class OrchestratorStatus:
    running: bool = True
    paused_reason: str = ""
    state: str = "idle"
    active_zone: str = ""
    direction: str = ""
    readings: dict[str, str] = field(default_factory=dict)
    position: Optional[dict[str, Any]] = None
    gate_blocks: list[str] = field(default_factory=list)
    realized_pnl_today: float = 0.0
    trades_today: int = 0
    last_evaluated: str = ""
    hunting_strikes: int = 0     # how many strikes are being hunted in parallel


# New entries are blocked when the freshest feed flush is older than this
# (aligned with the session steward's 180s staleness verdict). Position
# MANAGEMENT continues regardless — a stop is worth attempting on any data.
_ENTRY_STALE_AFTER_S = 180.0

# Consecutive bar-less minutes over an OPEN position before the data-stall
# alarm fires (live runtime only).
_POSITION_STALL_ALERT_MIN = 3


def _zone_snapshot(z: ZoneConfig) -> dict[str, Any]:
    """Compact per-zone settings for the decision record (the full document is
    reachable through config_version / the run's frozen config)."""
    return {
        "start": z.start, "end": z.end,
        "premium_min": z.premium_min, "premium_max": z.premium_max,
        "enabled_indicators": list(z.enabled_indicators),
        # Which RULE combined those indicators. Without this a stored decision
        # cannot be re-derived after the setting is changed — the row would
        # say which indicators voted but not how the votes were counted.
        "combine_rule": z.combine_rule,
        "neutral_mode": z.neutral_mode,
        "reeval_cadence": z.reeval_cadence,
        "direction_hold_min": z.direction_hold_min,
        "strike_scan_count": z.strike_scan_count,
        "max_trades": z.max_trades,
        "strategy_active": z.strategy_active,
        "atm_windows": {
            "oi_change": z.oi_structure.strikes_atm_window,
            "multi_tf": z.mtf_ratio.strikes_atm_window,
            "ratio": z.mqae.strikes_atm_window,
        },
        "ump": {
            "trigger_timeout_bars": z.ump.entry.trigger_timeout_bars,
            "max_sl_pct": z.ump.entry.max_sl_pct,
            "enable_long": z.ump.entry.enable_long,
            "enable_retest": z.ump.entry.enable_retest,
            "entry_timeframe_min": z.ump.entry.entry_timeframe_min,
            "scenarios_off": sorted(k for k, v in z.ump.entry.scenarios.items() if not v),
        },
    }


def _candidate_snapshot(h: "_Hunt", zone_cfg: ZoneConfig, bar_present: bool, *, fired: bool) -> dict[str, Any]:
    eng = h.engine
    last = getattr(eng, "last_close", None)
    in_band = (
        last is not None and zone_cfg.premium_min <= last <= zone_cfg.premium_max
    )
    return {
        "strike": h.contract.strike,
        "option_type": h.contract.option_type,
        "side": h.side,
        "bar_present": bar_present,
        "last_close": last,
        "in_band": in_band,
        "ump": {
            "state": getattr(eng, "state", None),
            "sub": getattr(eng, "sub", ""),
            "trig": bool(getattr(eng, "trig", False)),
            "levels_ready": bool(getattr(eng, "levels_ready", False)),
            "trail_sl": getattr(eng, "trail_sl", None),
            "max_sl": getattr(eng, "max_sl", None),
        },
        "hunt_started": h.started.isoformat() if h.started else None,
        "fired": fired,
    }


class ZoneOrchestrator:
    def __init__(self, deps: OrchestratorDeps) -> None:
        self.deps = deps
        self.position: Optional[_Position] = None
        # Multi-strike hunting: every in-band candidate gets its own engine.
        # List order IS priority order (nearest band-mid first, then lower
        # strike) — the same-minute multi-fire tie-break simply takes the
        # first fired element.
        self.hunts: list[_Hunt] = []
        # (contract token, entry-candle start) of the latest exit. Pine locks
        # out any re-entry inside the candle a trade exited in; a hunt engine
        # built after the exit inherits that lock for the same contract.
        self._exit_lock: Optional[tuple[str, datetime]] = None
        self.paused_reason = ""
        self._zone_start_readings: dict[tuple[date, ZoneId], dict[str, str]] = {}
        self._alerted: set[str] = set()
        # Kill / pause events of the day (IST time, reason) for the 15:45
        # summary; cleared with the alert set on date rollover.
        self.kill_events: list[tuple[datetime, str]] = []
        self._eval_now: Optional[datetime] = None
        self._cache_date: Optional[date] = None
        self._holiday_today = False
        self.status = OrchestratorStatus()
        # Decision-trace scratch for the minute being evaluated (reset per pass).
        self._reading_details: dict[str, dict[str, Any]] = {}
        self._decision: dict[str, Any] = {}
        # Serialises the per-minute pass with the admin's manual test entry /
        # square-off, so a manual order can never interleave with an engine
        # entry or exit for the same position in the same instant.
        self._pass_lock = asyncio.Lock()

    @property
    def hunt(self) -> Optional[_Hunt]:
        """The primary (highest-priority) hunt — kept for status displays,
        funnel counters and tests that predate multi-strike hunting."""
        return self.hunts[0] if self.hunts else None

    def _discard_hunts(self) -> None:
        self.hunts = []

    # ────────────────────────────────────────────────────────── evaluation

    async def evaluate_minute(self, now: datetime) -> None:
        """Run one full decision pass for the minute that just CLOSED.
        ``now`` is IST wall clock (naive) — the closed minute is now-1min.

        Every pass ends by emitting ONE decision record (``record_decision``
        dep) describing the stage reached, the gates, the indicator readings
        with their traces, the candidate strikes and the outcome — whatever
        path the pass took. The record is built from the pass's own state
        and can never alter it."""
        self.status = OrchestratorStatus(paused_reason=self.paused_reason)
        self.status.last_evaluated = now.isoformat()
        self._reading_details = {}
        self._decision = {
            "ts": now, "trade_date": now.date(), "candidates": None, "sizing": None,
            "data_age_s": None, "unanimous": None, "entered_trade_id": None,
            "reason": "", "day": "", "zone_id": "", "symbol": "", "expiry": None,
            "ledger": "", "config_version": None, "zone_snapshot": None,
        }
        async with self._pass_lock:
            try:
                await self._evaluate_minute_inner(now)
            finally:
                self._emit_decision(now)

    # ─────────────────────────────────────────────── manual test orders

    async def manual_test_entry(
        self,
        now: datetime,
        *,
        side: Side,
        max_notional: float,
        premium_min: float,
        premium_max: float,
    ) -> dict[str, Any]:
        """Admin burn-in: buy ONE small position right now through the SAME
        execution path the engine uses (paper simulator or the live
        Lakshmishree adapter, decided by Paper Mode exactly as for an engine
        entry). Skips the signal and UMP decision on purpose — it exists to
        prove order placement, fill, ledger, UI and latency.

        Sizing is bounded by ``max_notional`` (the API caps it at ₹1,000):
        lots = floor(max_notional / (premium × lot size)), refused below 1 lot.
        The position is managed like an adopted one: the Square-off button, the
        15:25 expiry force-close and the broker reconcile — no engine exits."""
        t0 = _perf_time.perf_counter()
        timings: dict[str, float] = {}

        def mark(k: str) -> None:
            timings[k] = round((_perf_time.perf_counter() - t0) * 1000, 1)

        async with self._pass_lock:
            mark("lock_acquired")
            if self.position is not None:
                raise ManualOrderError(
                    f"a position is already open (trade #{self.position.trade_id}) "
                    "— square it off first"
                )
            cfg = await self.deps.get_config()
            wd = now.date().weekday()
            if wd > 4:
                raise ManualOrderError("market closed (weekend)")
            day_key: Weekday = _WEEKDAYS[wd]
            day_cfg = cfg.days.get(day_key)
            if day_cfg is None:
                raise ManualOrderError(f"no day configuration for {day_key}")
            zone_id = self._zone_by_clock(day_cfg, now) or cfg.last_zone_id(day_key) or "Z1"
            zone_cfg = day_cfg.zones[zone_id]
            probe = zone_cfg.model_copy(update={
                "premium_min": premium_min,
                "premium_max": premium_max,
                "strike_scan_count": 1,
            })
            picked = await self.deps.select_strikes(day_cfg.index_symbol, side, probe)
            mark("strike_selected")
            if not picked:
                raise ManualOrderError(
                    f"no {side} strike of {day_cfg.index_symbol} has a premium inside "
                    f"₹{premium_min:g}–₹{premium_max:g} right now"
                )
            contract, premium = picked[0]
            lot = self.deps.lot_size(contract.symbol)
            if lot < 1:
                raise ManualOrderError(f"lot size unknown for {contract.symbol}")
            lots = int(max_notional // (premium * lot)) if premium > 0 else 0
            if lots < 1:
                raise ManualOrderError(
                    f"one lot of {contract.strike}{contract.option_type} costs "
                    f"₹{premium * lot:,.2f} — above the ₹{max_notional:,.0f} cap"
                )
            ledger = "paper" if cfg.global_.paper.paper_mode else "live"
            # No engine may enter alongside the manual position.
            self._discard_hunts()
            try:
                result = await self.deps.execute_entry(
                    cfg,
                    symbol=contract.symbol,
                    expiry=contract.expiry,
                    strike=contract.strike,
                    option_type=contract.option_type,
                    raw_price=premium,
                    lots=lots,
                    lot_size=lot,
                    unique_id=f"algoT-{now:%H%M%S}",
                )
            except Exception as e:
                # Same rule as an engine entry: an UNKNOWN order state pauses
                # the whole engine and pages a human — never a silent retry.
                self.paused_reason = (
                    "manual test entry order state UNKNOWN — verify in the broker "
                    "app, then Resume"
                )
                self.deps.notify(
                    f"manual-entry-unknown-{now:%H%M%S}",
                    f"🚨 CRITICAL: manual test entry {contract.strike}"
                    f"{contract.option_type} × {lots} lot(s) ended in an UNKNOWN "
                    f"state ({e}). Engine PAUSED — check the broker app.",
                )
                raise ManualOrderError(f"order state unknown: {e}") from e
            mark("order_filled")
            if result is None:
                raise ManualOrderError(
                    "the order was refused (broker not configured, instrument not "
                    "resolved, or rejected) — see the alert log"
                )
            shell = _AdoptedShellEngine()
            shell.sub = "MANUAL_TEST"
            shell.last_close = premium
            self.position = _Position(
                trade_id=-1,
                contract=contract,
                zone_id=zone_id,
                day=day_key,
                side=side,
                entry_fill=result.fill_price,
                lots=lots,
                ledger=ledger,
                engine=shell,  # type: ignore[arg-type]
                broker_order_id=result.broker_order_id or "",
                entry_ts=now,
            )
            recorded_error = ""
            try:
                trade_id = await self.deps.insert_trade(
                    trade_date=now.date(),
                    day=day_key,
                    zone_id=zone_id,
                    index_symbol=contract.symbol,
                    side=side,
                    token=contract.token,
                    strike=contract.strike,
                    expiry=contract.expiry,
                    entry_ts=now,
                    entry_price=result.fill_price,
                    lots=lots,
                    ledger=ledger,
                    sub_scenario="MANUAL_TEST",
                    broker_order_id=result.broker_order_id or "",
                )
                self.position.trade_id = trade_id
            except Exception as e:
                trade_id = -1
                recorded_error = str(e)
                self.deps.notify(
                    f"manual-record-fail-{now:%H%M%S}",
                    f"🚨 CRITICAL: manual test entry FILLED but could not be "
                    f"recorded ({e}) — {contract.strike}{contract.option_type} "
                    f"@ {result.fill_price:.2f} × {lots} lot(s). Position IS managed.",
                )
            mark("recorded")
            if trade_id >= 0 and self.deps.audit_event is not None:
                self.deps.audit_event(
                    "runtime_trade_entry",
                    f"MANUAL TEST ENTRY {side} {contract.symbol} {contract.strike}"
                    f"{contract.option_type} @ {result.fill_price:.2f} × {lots} "
                    f"lot(s) [{day_key} {zone_id}, {ledger}]",
                    {"trade_id": trade_id, "manual": True, "strike": contract.strike,
                     "lots": lots, "fill": result.fill_price, "ledger": ledger,
                     "timings_ms": dict(timings)},
                )
            if day_cfg.alerts.telegram_trade_entry_exit:
                self.deps.notify(
                    f"trade-entry-{trade_id}",
                    entry_message(
                        now=now, side=side, symbol=contract.symbol,
                        strike=contract.strike, option_type=contract.option_type,
                        expiry=contract.expiry.isoformat() if contract.expiry else None,
                        fill=result.fill_price, lots=lots, lot_size=lot,
                        sub_scenario="MANUAL_TEST", day=day_key,
                        zone=zone_id, ledger=ledger, trade_id=trade_id,
                    ),
                )
            self.status.state = "in_trade"
            self._fill_position_status()
            mark("total")
            return {
                "trade_id": trade_id,
                "ledger": ledger,
                "symbol": contract.symbol,
                "expiry": contract.expiry.isoformat() if contract.expiry else None,
                "strike": contract.strike,
                "option_type": contract.option_type,
                "side": side,
                "premium_at_decision": premium,
                "fill_price": result.fill_price,
                "lots": lots,
                "lot_size": lot,
                "quantity": lots * lot,
                "notional": round(result.fill_price * lots * lot, 2),
                "broker_order_id": result.broker_order_id or "",
                "broker_status": result.note,
                "zone_id": zone_id,
                "recorded_error": recorded_error,
                "timings_ms": timings,
            }

    async def manual_square_off(self, now: datetime) -> dict[str, Any]:
        """Admin square-off of the open position, RIGHT NOW, through the exact
        exit code the engine uses (``_manage_position``): latch a
        MANUAL_SQUARE_OFF exit, then run management immediately. A failed live
        sell keeps the latch, so the per-minute loop retries it every minute
        with a CRITICAL page — the same guarantee as an engine exit."""
        result = await self._square_off_now(now, "MANUAL_SQUARE_OFF")
        assert result is not None
        return result

    async def kill_square_off(self, now: datetime) -> Optional[dict[str, Any]]:
        """A kill switch was just switched ON: if it covers the open position,
        square it off RIGHT NOW (user rule 2026-09-23 — "kill ON while a trade
        runs = the trade exits"), instead of waiting up to a minute for the
        next pass. Returns None when there is no position or no switch covers
        it. The per-minute check in ``_manage_position`` stays the backstop."""
        return await self._square_off_now(now, None)

    async def _square_off_now(
        self, now: datetime, reason: Optional[str]
    ) -> Optional[dict[str, Any]]:
        """Shared immediate exit. ``reason=None`` = decide from the kill
        switches, and do nothing if none covers the position."""
        t0 = _perf_time.perf_counter()
        timings: dict[str, float] = {}

        def mark(k: str) -> None:
            timings[k] = round((_perf_time.perf_counter() - t0) * 1000, 1)

        async with self._pass_lock:
            mark("lock_acquired")
            pos = self.position
            if pos is None:
                if reason is None:
                    return None
                raise ManualOrderError("no open position to square off")
            cfg = await self.deps.get_config()
            day_cfg = cfg.days.get(pos.day)
            if reason is None:
                kill = self._kill_covering(cfg, pos, now)
                if kill is None:
                    return None
                reason = kill[0]
                self._audit_kill_close(pos, kill)
            # Reference = the contract's NEWEST tick. The last closed minute's
            # close was up to ~60 s old and read like a quote (2026-09-15:
            # shown ₹1.15 while the market and the fill were ₹0.95).
            ref: Optional[float] = None
            ref_ts: Optional[datetime] = None
            ref_source = "last traded price"
            if self.deps.latest_quote is not None:
                try:
                    q = await self.deps.latest_quote(pos.contract)
                except Exception as e:  # a quote read must never block an exit
                    log.warning("algo.orch.latest_quote_failed", error=str(e))
                    q = None
                if q is not None:
                    ref, ref_ts = q
            if ref is None:
                ref_source = "last closed minute"
                ref = pos.engine.last_close
                bar = await self.deps.latest_minute(pos.contract, now)
                if bar is not None:
                    ref = bar.c
            if ref is None:
                ref_source = "entry fill (no price available)"
                ref = pos.entry_fill
            mark("price_ref")
            if pos.pending_exit is None:
                pos.pending_exit = (ref, reason)
            trade_id, ledger = pos.trade_id, pos.ledger
            await self._manage_position(now, cfg, day_cfg)
            mark("exit_done")
            closed = self.position is None
            if closed:
                # The strip reads ``status.position``, which the per-minute pass
                # only rebuilds at its next run — clear it now or the closed
                # position stays on screen for up to a minute.
                self.status.position = None
                self.status.state = "idle"
            else:
                self.status.state = "in_trade"
                self._fill_position_status()
            mark("total")
            return {
                "trade_id": trade_id,
                "ledger": ledger,
                "closed": closed,
                "exit_reason": pos.pending_exit[1] if pos.pending_exit else reason,
                "reference_price": ref,
                "reference_source": ref_source,
                "reference_ts": ref_ts.astimezone(_IST_TZ).isoformat() if ref_ts else None,
                "message": (
                    "position closed" if closed else
                    "the exit order failed — the position is still open and the "
                    "engine retries the sell every minute (see the alert log)"
                ),
                "timings_ms": timings,
            }

    async def _evaluate_minute_inner(self, now: datetime) -> None:
        self._eval_now = now
        cfg = await self.deps.get_config()
        if self.deps.config_version is not None:
            try:
                self._decision["config_version"] = self.deps.config_version()
            except Exception:  # noqa: BLE001
                pass
        today = now.date()
        if self._cache_date != today:
            # Date rollover: yesterday's zone-start readings, one-shot alert
            # keys and audit dedup keys are dead weight in a long-lived
            # process — drop them.
            self._cache_date = today
            self._zone_start_readings.clear()
            self._alerted.clear()
            self.kill_events.clear()
        wd_index = today.weekday()
        if wd_index > 4:
            self.status.state = "weekend"
            return
        day_key: Weekday = _WEEKDAYS[wd_index]
        day_cfg = cfg.days.get(day_key)
        self._decision["day"] = day_key
        if day_cfg is not None:
            self._decision["symbol"] = day_cfg.index_symbol

        # Holiday = the config's own list ∪ the platform's NSE holiday file
        # (when wired) — a stale config list can never trade a known holiday.
        holiday = any(h.date == today.isoformat() for h in cfg.global_.holidays)
        if not holiday and self.deps.is_platform_holiday is not None:
            try:
                holiday = self.deps.is_platform_holiday(today)
            except Exception as e:
                # Fail-open here is acceptable (the config's own list still
                # applies) but never silent — a corrupted holiday file would
                # otherwise erase the union backstop invisibly.
                log.warning("algo.orch.platform_holiday_probe_failed", error=str(e))
                holiday = False
        # A carried position's management runs on holidays too — the data
        # stall alarm must know no bars are EXPECTED (see _manage_position).
        self._holiday_today = holiday
        killed = (
            cfg.global_.master_kill
            or holiday
            or day_cfg is None
            or day_cfg.day_kill
        )
        if killed:
            self.status.state = "killed"
            kill_reason = (
                "master kill" if cfg.global_.master_kill
                else "holiday" if holiday else "day kill"
            )
            self.status.gate_blocks.append(kill_reason)
            # §10 — a kill is alerted (once/day, per-day toggle) and audited,
            # never silent.
            if day_cfg is None or day_cfg.alerts.telegram_trade_entry_exit:
                self._alert_once(
                    f"kill-{today}-{kill_reason}",
                    kill_message(now=now, scope="day", reason=kill_reason, day=day_key),
                )
            self._audit_once(
                f"kill-{today}-{kill_reason}", "runtime_kill",
                f"{day_key}: trading blocked by {kill_reason}",
                {"reason": kill_reason},
            )
            # A dead switch discards any hunts; an open trade keeps being
            # managed below (never abandoned unmanaged).
            self._discard_hunts()

        ledger = "paper" if cfg.global_.paper.paper_mode else "live"
        self._decision["ledger"] = ledger
        # ── risk counters (recomputed from the ledger — restart-safe) ──
        # GUARDED: a DB hiccup here must never abort the pass — the open
        # position's management below is the one thing that may never be
        # skipped ("an already-open trade keeps being MANAGED"). On failure:
        # degraded counters, entries blocked, loud once. (2026-08-18: these
        # awaits sat unguarded ABOVE management, so a 4-minute DB outage
        # froze the stop-loss with no alert.)
        closed: list[tuple[str, float]] = []
        entered_zones: Optional[list[str]] = None
        realized = 0.0
        allocated = 0.0
        counters_ok = True
        try:
            closed = await self.deps.today_closed(today, ledger)
            if self.deps.today_entries is not None:
                entered_zones = await self.deps.today_entries(today, ledger)
            realized = sum(p for _, p in closed)
            balance = await self.deps.current_balance(cfg)
            if day_cfg is not None:
                alloc_pct = 100.0 if day_cfg.all_in else day_cfg.allocation_pct
                allocated = balance * alloc_pct / 100
        except Exception as e:
            counters_ok = False
            log.warning("algo.orch.counters_failed", error=str(e))
            self._alert_once(
                f"counters-fail-{today}",
                "⚠️ Ledger/balance reads are failing — risk counters degraded, "
                "NEW entries blocked; the open position (if any) keeps being "
                "managed.",
            )
        self.status.realized_pnl_today = round(realized * 100) / 100
        self.status.trades_today = len(closed) + (1 if self.position else 0)
        risk_blocks = (
            self._risk_gate(day_cfg, today, closed, realized, allocated)
            if counters_ok
            else ["ledger unavailable — entries blocked"]
        )
        self.status.paused_reason = self.paused_reason
        self.status.gate_blocks.extend(risk_blocks)

        # ── open-trade management (before, and instead of, any signals §6) ──
        if self.position is not None:
            await self._manage_position(now, cfg, day_cfg)
            if self.position is not None:
                # Sequencing rule (§2.3): the open trade suppresses every
                # later zone's entry evaluation until it fully closes.
                self.status.state = "in_trade"
                self._fill_position_status()
                return
            # The position closed THIS minute. Refresh the ledger-derived
            # counters before any entry logic below, or the day kills, the
            # loss streak and max-trades all under-count by exactly the trade
            # that just closed (an extra entry could slip past every cap on
            # the exit minute). Guarded like the block above: on failure,
            # block entries rather than abort.
            try:
                closed = await self.deps.today_closed(today, ledger)
                realized = sum(p for _, p in closed)
                self.status.realized_pnl_today = round(realized * 100) / 100
                self.status.trades_today = len(closed)
                risk_blocks = self._risk_gate(day_cfg, today, closed, realized, allocated, now=now)
            except Exception as e:
                log.warning("algo.orch.counters_refresh_failed", error=str(e))
                risk_blocks = ["ledger unavailable — entries blocked"]
            self.status.paused_reason = self.paused_reason
            for b in risk_blocks:
                if b not in self.status.gate_blocks:
                    self.status.gate_blocks.append(b)

        if killed or day_cfg is None:
            return

        # ── live-data freshness gate (NEW entries only; an open position is
        # managed above regardless). Wired only in the live runtime — the
        # backtest and unit tests leave data_age_s None and are unaffected.
        if self.deps.data_age_s is not None:
            # FAIL-SAFE: any error here means "freshness unknown" → entries
            # blocked + alerted. It must never abort the pass — a crash here
            # killed every evaluation silently for a whole session
            # (ImportError, 2026-08-18) because the loop's catch-all ate it.
            try:
                age = await self.deps.data_age_s(day_cfg.index_symbol)
            except Exception as e:
                log.warning("algo.orch.data_age_failed", error=str(e))
                age = None
            self._decision["data_age_s"] = age
            if age is None or age > _ENTRY_STALE_AFTER_S:
                self._discard_hunts()
                self.status.state = "stale_data"
                self.status.gate_blocks.append("market data stale — entries blocked")
                self._alert_once(
                    f"stale-{today}",
                    "⚠️ Market data is "
                    + ("absent" if age is None else f"{age:.0f}s old")
                    + f" (> {_ENTRY_STALE_AFTER_S:.0f}s) — new entries are "
                    "blocked until the feed recovers.",
                )
                return

        # ── zone by clock ──
        zone_id = self._zone_by_clock(day_cfg, now)
        if zone_id is None:
            self.status.state = "idle"
            self._discard_hunts()
            return
        zone_cfg = day_cfg.zones[zone_id]
        self.status.active_zone = f"{day_key}/{zone_id}"
        self._decision["zone_id"] = zone_id
        self._decision["zone_snapshot"] = _zone_snapshot(zone_cfg)

        # ── zone gates ──
        blocks = list(risk_blocks)
        if self.paused_reason:
            blocks.append(f"paused: {self.paused_reason}")
        if zone_cfg.zone_kill:
            blocks.append("zone kill switch")
            # §10 — a killed zone is alerted (once/day, toggle) + audited.
            if day_cfg.alerts.telegram_trade_entry_exit:
                self._alert_once(
                    f"zonekill-{today}-{zone_id}",
                    kill_message(
                        now=now, scope="zone", reason="zone kill switch",
                        day=day_key, zone=zone_id,
                    ),
                )
            self._audit_once(
                f"zonekill-{today}-{zone_id}", "runtime_kill",
                f"{day_key} {zone_id}: zone kill switch", {"zone": zone_id},
            )
        problems = zone_completeness(cfg, day_key, zone_id)
        if problems:
            blocks.append("incomplete configuration (§14)")
            self._alert_once(
                f"gate-{today}-{zone_id}",
                f"⚠️ {day_key} {zone_id} blocked from trading — incomplete "
                f"configuration: {problems[0]}",
            )
            self._audit_once(
                f"gate-{today}-{zone_id}", "runtime_gate_block",
                f"{day_key} {zone_id}: incomplete configuration — {problems[0]}",
                {"zone": zone_id, "problems": problems[:5]},
            )
        # max_trades is consumed on the ENTRY day (today_entries dep); the
        # closed-list fallback preserves pre-carry behavior in bare tests.
        entries_this_zone = (
            entered_zones.count(zone_id)
            if entered_zones is not None
            else sum(1 for z, _ in closed if z == zone_id)
        )
        if entries_this_zone >= zone_cfg.max_trades:
            blocks.append(f"max trades reached ({zone_cfg.max_trades})")
        if blocks:
            self.status.state = "gated"
            self.status.gate_blocks = blocks
            self._discard_hunts()
            return

        # ── indicator evaluation (per §4.2 cadence) + unanimous rule ──
        readings = await self._readings(cfg, day_key, zone_id, zone_cfg, now, today)
        self.status.readings = readings
        combined = self._combine(zone_cfg, readings)
        direction = combined.direction
        self.status.direction = direction or "NO_TRADE"
        # Column name predates F1; it has always meant "a direction was
        # produced", which is still exactly what it means under Majority.
        self._decision["unanimous"] = direction is not None
        self._decision["combine_reason"] = combined.reason
        await self.deps.record_signal(
            ts=now, trade_date=today, day=day_key, zone_id=zone_id,
            indicator="combined", reading=direction or "NO_TRADE",
            payload={"readings": readings},
        )

        # ── §2.4 Strategy Active — observation without execution. Indicators
        # were evaluated and recorded above (deliberately: the spec's stated
        # purpose is watching the filter layer risk-free); with the switch OFF
        # the execution engine stays completely locked — no hunts, no entries.
        if not zone_cfg.strategy_active:
            self._discard_hunts()
            self.status.state = "strategy_inactive"
            self.status.gate_blocks.append("strategy inactive (zone switch)")
            return

        # ── §5.3 direction lifecycle ──
        if direction is None:
            # No Trade (or undetermined): normally the engine sits completely
            # idle — any in-progress hunts are discarded. EXPERIMENTAL
            # direction-hold (user-approved): live hunts in THIS zone stay
            # alive through NO_TRADE flickers for up to N minutes from their
            # start; an OPPOSITE side below still discards instantly.
            hold = zone_cfg.direction_hold_min
            first = self.hunt
            if (
                hold > 0
                and first is not None
                and first.zone_id == zone_id
                and first.started is not None
                and now - first.started < timedelta(minutes=hold)
            ):
                self.status.state = "hunting_hold"
                await self._feed_hunt(
                    now, cfg, day_cfg, day_key, zone_cfg, ledger, allocated
                )
                if self.position is not None:
                    self.status.state = "in_trade"
                    self._fill_position_status()
                else:
                    self.status.hunting_strikes = len(self.hunts)
                return
            self._discard_hunts()
            self.status.state = "no_trade"
            return
        first = self.hunt
        if first is not None and (
            first.side != direction or first.zone_id != zone_id
        ):
            self._discard_hunts()  # flip → discard ALL, start fresh for the new side

        if not self.hunts:
            picked = await self.deps.select_strikes(
                day_cfg.index_symbol, direction, zone_cfg
            )
            if not picked:
                self.status.state = "no_strike_in_band"
                self._alert_once(
                    f"band-{today}-{zone_id}-{direction}",
                    f"⚠️ {day_key} {zone_id}: no {direction} strike has a premium "
                    f"inside the {zone_cfg.premium_min}–{zone_cfg.premium_max} band. "
                    f"If this persists all session, check that the live feed is "
                    f"actually following {day_cfg.index_symbol} and that the band "
                    "fits its premium scale.",
                )
                return
            # Every in-band candidate (nearest band-mid first) hunts with its
            # own engine; list order carries the entry tie-break priority.
            for contract, _premium in picked:
                engine = await self.deps.build_engine(contract, zone_cfg, False)
                if engine is None:
                    log.warning(
                        "algo.orch.candidate_no_engine",
                        strike=contract.strike, option_type=contract.option_type,
                    )
                    continue
                engine.entry_guard = self._band_guard(zone_cfg, engine)
                lock = self._exit_lock
                if lock is not None and lock[0] == contract.token and hasattr(engine, "lock_candle"):
                    engine.lock_candle(lock[1])
                self.hunts.append(_Hunt(
                    contract=contract, zone_id=zone_id, side=direction,
                    engine=engine, started=now,
                ))
            if not self.hunts:
                self.status.state = "no_engine_data"
                return

        # ── feed every hunting engine this minute; the first to fire enters ──
        await self._feed_hunt(now, cfg, day_cfg, day_key, zone_cfg, ledger, allocated)
        if self.position is not None:
            self.status.state = "in_trade"
            self._fill_position_status()
        else:
            self.status.state = (
                "hunting" if self.hunts else self.status.state or "idle"
            )
            self.status.hunting_strikes = len(self.hunts)

    # ─────────────────────────────────────────────────────────── internals

    async def _readings(
        self,
        cfg: AlgoConfig,
        day_key: Weekday,
        zone_id: ZoneId,
        zone_cfg: ZoneConfig,
        now: datetime,
        today: date,
    ) -> dict[str, str]:
        cache_key = (today, zone_id)
        if zone_cfg.reeval_cadence == "zone_start" and cache_key in self._zone_start_readings:
            return self._zone_start_readings[cache_key]
        readings: dict[str, str] = {}
        for ind in zone_cfg.enabled_indicators:
            try:
                result = await self.deps.evaluate_indicator(
                    ind, day_key, zone_id, zone_cfg, cfg.days[day_key].index_symbol
                )
            except Exception as e:
                log.warning("algo.orch.indicator_error", indicator=ind, error=str(e))
                result = IndicatorEval("NO_TRADE", {"error": str(e)[:200]})
            if isinstance(result, IndicatorEval):
                reading = result.signal
                self._reading_details[ind] = dict(result.detail)
            else:
                reading = str(result)
                self._reading_details[ind] = {}
            readings[ind] = reading
            await self.deps.record_signal(
                ts=now, trade_date=today, day=day_key, zone_id=zone_id,
                indicator=ind, reading=reading,
            )
        if zone_cfg.reeval_cadence == "zone_start":
            self._zone_start_readings[cache_key] = readings
        return readings

    @staticmethod
    def _combine(zone_cfg: ZoneConfig, readings: dict[str, str]) -> CombinedDecision:
        """§4.1 + Signal Console F1 — the zone's configured combination rule
        applied to whichever indicators are enabled. Defaults reproduce the
        original hard-coded unanimity exactly. The live strip calls the same
        function (``combine_readings``) so the screen cannot disagree with the
        engine."""
        return combine_readings(
            zone_cfg.enabled_indicators,
            readings,
            rule=zone_cfg.combine_rule,
            neutral_mode=zone_cfg.neutral_mode,
        )

    def _zone_by_clock(self, day_cfg: DayConfig, now: datetime) -> Optional[ZoneId]:
        hm = now.time()
        for zid, z in day_cfg.zones.items():
            try:
                start = time(*map(int, z.start.split(":")))
                end = time(*map(int, z.end.split(":")))
            except ValueError:
                continue
            if start <= hm < end:
                return zid
        return None

    def _band_guard(self, zone_cfg: ZoneConfig, engine: UmpEngine) -> Callable[[], bool]:
        """§4.2 — the live premium must sit inside the zone's band at the
        moment an entry fires. Per-hunt: each engine is checked against ITS
        OWN last close. If the engine is no longer being hunted (post-entry
        re-arm, adoption replay) the guard steps aside — exactly the old
        "no hunt → True" semantics."""
        orch = self

        def guard() -> bool:
            if not any(h.engine is engine for h in orch.hunts):
                return True
            last = engine.last_close
            if last is None:
                return False
            return zone_cfg.premium_min <= last <= zone_cfg.premium_max

        return guard

    async def _feed_hunt(
        self,
        now: datetime,
        cfg: AlgoConfig,
        day_cfg: DayConfig,
        day_key: Weekday,
        zone_cfg: ZoneConfig,
        ledger: str,
        allocated: float,
    ) -> None:
        assert self.hunts
        # Phase 1 — feed EVERY hunting engine its own contract's closed
        # minute. A missing bar skips that hunt only (other contracts may
        # have printed).
        bars_present: dict[int, bool] = {}
        bars_seen: dict[int, Any] = {}
        for h_i in self.hunts:
            bar = await self.deps.latest_minute(h_i.contract, now)
            bars_present[id(h_i)] = bar is not None
            if bar is None:
                continue
            bars_seen[id(h_i)] = bar
            seen = getattr(h_i.engine, "last_minute_ts", None)
            if seen is not None and bar.ts <= seen:
                # The warm-up already processed this minute with entries
                # DISARMED — it is the minute whose data produced the
                # direction. Re-running it armed would let a setup that
                # formed before the direction existed become an entry, so
                # armed evaluation starts with the NEXT minute.
                continue
            h_i.engine.process_minute(bar.ts, bar.o, bar.h, bar.l, bar.c)

        # Phase 2 — the first fired hunt wins. List order is the tie-break
        # (nearest band-mid, then lowest strike), so a same-minute multi-fire
        # resolves deterministically.
        fired = [h_i for h_i in self.hunts if h_i.engine.in_trade]
        # Decision trace: every candidate's state THIS minute, winner marked.
        self._decision["candidates"] = [
            _candidate_snapshot(h_i, zone_cfg, bars_present.get(id(h_i), False),
                                fired=bool(fired) and h_i is fired[0])
            for h_i in self.hunts
        ]
        if not fired:
            return
        h = fired[0]

        # ── the winning engine entered — size and route ──
        entry_event = h.engine.trades[-1]
        lot = self.deps.lot_size(h.contract.symbol)
        # ENTRY PRICE = the entry minute's CLOSE (user decision 2026-09-23,
        # option A). The engine fires when a minute TOUCHES its level and
        # records the level — Pine parity, and its own stops stay anchored
        # there. But that minute is only known once it closes, and its close is
        # the earliest price any real order can get: across 341 backtest trades
        # the close sat +2.3 % (R1) / +3.9 % (R2) above the level, so paper and
        # backtest booked fills live trading never could ("signal 195, filled
        # 198"). Sizing, the fill and every report now use the close; the level
        # stays the SIGNAL. Falls back to the level only without a bar.
        signal_price = entry_event.entry_price
        entry_bar = bars_seen.get(id(h))
        raw_entry = float(entry_bar.c) if entry_bar is not None else signal_price
        if lot < 1:
            # Registry could not resolve a lot size — refuse loudly rather
            # than trade wrong contract math (old code silently assumed 75).
            # Once per symbol per day: the condition re-fires every minute
            # while the registry stays broken.
            self._alert_once(
                f"lot-size-unknown-{now.date()}-{h.contract.symbol}",
                f"🚨 CRITICAL: lot size unknown for {h.contract.symbol} — "
                "entry refused. Check data/symbols.json / the symbol registry.",
            )
            self._discard_hunts()
            return
        lots = int(allocated // (raw_entry * lot)) if raw_entry > 0 else 0
        self._decision["sizing"] = {
            "allocated": round(allocated, 2), "lot_size": lot, "raw_entry": raw_entry,
            "signal_price": signal_price,
            "lots": lots, "sub_scenario": entry_event.sub_scenario,
        }
        if lots < 1:
            self._alert_once(
                f"lots-{now.date()}-{h.zone_id}",
                f"⚠️ {day_key} {h.zone_id}: allocation ₹{allocated:.0f} cannot buy "
                f"one lot at premium {raw_entry:.2f} — entry skipped.",
            )
            self._discard_hunts()
            return

        try:
            result = await self.deps.execute_entry(
                cfg,
                symbol=h.contract.symbol,
                expiry=h.contract.expiry,
                strike=h.contract.strike,
                option_type=h.contract.option_type,
                raw_price=raw_entry,
                lots=lots,
                lot_size=lot,
                unique_id=f"algo-{now:%H%M%S}",
            )
        except Exception as e:
            # UNKNOWN order state (transport timeout mid-placement): the BUY
            # may be live at the exchange. The one forbidden response is
            # "try again next minute" — the fired engine would still be
            # in_trade and this path would re-order every minute until
            # something broke (repeat-BUY hazard, found 2026-08-18). Pause
            # the whole engine and page a human to reconcile.
            self._discard_hunts()
            self.paused_reason = (
                "entry order state UNKNOWN (transport failure) — verify in "
                "the broker app, then Resume"
            )
            self.deps.notify(
                f"entry-state-unknown-{now:%H%M%S}",
                f"🚨 CRITICAL: entry order for {h.side} {h.contract.symbol} "
                f"{h.contract.strike}{h.contract.option_type} × {lots} lot(s) "
                f"ended in an UNKNOWN state ({e}). The engine is PAUSED — "
                "check the broker app for a filled position, square off or "
                "adopt manually, then press Resume.",
            )
            self._audit_once(
                f"entry-unknown-{now:%H%M%S}", "runtime_entry_unknown",
                f"entry order state unknown: {e}",
                {"strike": h.contract.strike, "lots": lots},
            )
            return
        if result is None:
            # Execution refused (live broker unconfigured/failed) — the
            # engine's internal entry is discarded with the hunts; nothing
            # was recorded and nothing is at risk.
            self._discard_hunts()
            return
        # Take ownership of the position IN MEMORY before anything that can
        # fail: the fill already happened. If an exception unwound past this
        # point (e.g. a DB hiccup in insert_trade), the engine would still be
        # in_trade next minute and _feed_hunt would route a DUPLICATE order
        # every minute until the failure cleared.
        self.position = _Position(
            trade_id=-1,
            contract=h.contract,
            zone_id=h.zone_id,
            day=day_key,
            side=h.side,
            entry_fill=result.fill_price,
            lots=lots,
            ledger=ledger,
            engine=h.engine,
            broker_order_id=result.broker_order_id,
            entry_ts=now,
        )
        # Wholesale discard: the losers' engines (even one internally
        # in_trade from this same minute) become unreachable — they are never
        # fed again and nothing else holds a reference to them.
        self._discard_hunts()
        # Disarm the POSITION engine's entry models: with its guard gone from
        # the hunt set it would otherwise re-enter internally after its exit
        # fires (e.g. while a live exit order is being retried), repointing
        # trades[-1] at a phantom trade and silently cancelling the retry
        # (found 2026-08-18). A position engine manages; it never re-enters.
        h.engine.entry_guard = lambda: False
        try:
            trade_id = await self.deps.insert_trade(
                trade_date=now.date(),
                day=day_key,
                zone_id=h.zone_id,
                index_symbol=h.contract.symbol,
                side=h.side,
                token=h.contract.token,
                strike=h.contract.strike,
                expiry=h.contract.expiry,
                entry_ts=now,
                entry_price=result.fill_price,
                lots=lots,
                ledger=ledger,
                sub_scenario=entry_event.sub_scenario,
                broker_order_id=result.broker_order_id,
            )
            self.position.trade_id = trade_id
            self._decision["entered_trade_id"] = trade_id
            self._decision["sizing"]["fill"] = result.fill_price
        except Exception as e:
            trade_id = -1
            self.deps.notify(
                f"trade-record-fail-{now:%H%M%S}",
                f"🚨 CRITICAL: entry FILLED but could not be recorded ({e}) — "
                f"{h.side} {h.contract.symbol} {h.contract.strike}"
                f"{h.contract.option_type} @ {result.fill_price:.2f} × {lots} "
                f"lot(s), order {result.broker_order_id or 'paper'}. The position "
                "IS under management; add the ledger row manually.",
            )
        # Audited OUTSIDE the try: an audit-layer failure must never fake a
        # "could not be recorded" critical (which invites a duplicate manual
        # ledger row).
        if trade_id >= 0 and self.deps.audit_event is not None:
            self.deps.audit_event(
                "runtime_trade_entry",
                f"ENTRY {h.side} {h.contract.symbol} {h.contract.strike}"
                f"{h.contract.option_type} @ {result.fill_price:.2f} × {lots} "
                f"lot(s) [{day_key} {h.zone_id}, {ledger}]",
                {"trade_id": trade_id, "zone": h.zone_id,
                 "strike": h.contract.strike, "lots": lots,
                 "fill": result.fill_price, "ledger": ledger},
            )
        # §8 Shadow Mode — mirror a LIVE entry into the paper ledger with the
        # identical trade (same contract/lots), paper-simulated fill. Best
        # effort: a shadow failure never touches the live trade.
        if ledger == "live" and cfg.global_.paper.shadow_mode:
            try:
                shadow_fill = buy_fill(cfg.global_.paper, raw_entry).price
                self.position.shadow_trade_id = await self.deps.insert_trade(
                    trade_date=now.date(),
                    day=day_key,
                    zone_id=h.zone_id,
                    index_symbol=h.contract.symbol,
                    side=h.side,
                    token=h.contract.token,
                    strike=h.contract.strike,
                    expiry=h.contract.expiry,
                    entry_ts=now,
                    entry_price=shadow_fill,
                    lots=lots,
                    ledger="paper",
                    sub_scenario=entry_event.sub_scenario,
                )
                self.position.shadow_entry_fill = shadow_fill
            except Exception as e:
                self.deps.notify(
                    f"shadow-entry-fail-{trade_id}",
                    f"⚠️ Shadow-Mode mirror entry failed ({e}) — live trade "
                    "unaffected, shadow twin skipped.",
                )
        if day_cfg.alerts.telegram_trade_entry_exit:
            self.deps.notify(
                f"trade-entry-{trade_id}",
                entry_message(
                    now=now, side=h.side, symbol=h.contract.symbol,
                    strike=h.contract.strike, option_type=h.contract.option_type,
                    expiry=h.contract.expiry.isoformat() if h.contract.expiry else None,
                    fill=result.fill_price, lots=lots, lot_size=lot,
                    sub_scenario=entry_event.sub_scenario, day=day_key,
                    zone=h.zone_id, ledger=ledger, trade_id=trade_id,
                ),
            )

    @staticmethod
    def _kill_covering(
        cfg: AlgoConfig, pos: _Position, now: datetime
    ) -> Optional[tuple[str, str]]:
        """The kill switch, if any, that covers the open position, as
        (exit_reason, label). Master kill covers everything. The day kill is
        TODAY's weekday, so it also exits a position carried in overnight. A
        zone kill covers the trade opened in that zone."""
        if cfg.global_.master_kill:
            return "MASTER_KILL", "master kill"
        wd = now.date().weekday()
        today_cfg = cfg.days.get(_WEEKDAYS[wd]) if wd <= 4 else None
        if today_cfg is not None and today_cfg.day_kill:
            return "DAY_KILL", "day kill"
        own_day = cfg.days.get(pos.day)
        own_zone = own_day.zones.get(pos.zone_id) if own_day is not None else None
        if own_zone is not None and own_zone.zone_kill:
            return "ZONE_KILL", f"zone kill {pos.zone_id}"
        return None

    def _audit_kill_close(self, pos: _Position, kill: tuple[str, str]) -> None:
        reason, label = kill
        # The master-kill event name predates the other switches; kept so
        # existing audit readers keep matching.
        event = "runtime_master_kill_close" if reason == "MASTER_KILL" else "runtime_kill_close"
        self._audit_once(
            f"{reason.lower()}-close-{pos.trade_id}",
            event,
            f"{label} — squaring off trade #{pos.trade_id} "
            f"{pos.contract.strike}{pos.contract.option_type}",
            {"trade_id": pos.trade_id, "reason": reason},
        )

    async def _manage_position(
        self, now: datetime, cfg: AlgoConfig, day_cfg: Optional[DayConfig]
    ) -> None:
        assert self.position is not None
        pos = self.position
        bar = await self.deps.latest_minute(pos.contract, now)
        if bar is not None:
            pos.engine.process_minute(bar.ts, bar.o, bar.h, bar.l, bar.c)
            pos.stall_minutes = 0
        elif self.deps.data_age_s is not None and not getattr(self, "_holiday_today", False):
            # No premium bar for this contract: the whole exit ladder
            # (MAX_SL / trail / target) is frozen. That must never stay
            # silent while real money is open. Live runtime only. (A carried
            # position on a HOLIDAY has no bars by definition — no alarm.)
            pos.stall_minutes += 1
            if pos.stall_minutes == _POSITION_STALL_ALERT_MIN:
                self.deps.notify(
                    f"position-data-stall-{pos.trade_id}",
                    f"🚨 CRITICAL: no market data for the OPEN position "
                    f"{pos.contract.strike}{pos.contract.option_type} for "
                    f"{pos.stall_minutes} minutes — stop-loss/trailing are "
                    "FROZEN. Watch the position in the broker app and close "
                    "manually if the feed stays down.",
                )

        # Broker-side audit of a LIVE position every 5th minute: catches a
        # manual square-off, a fill we wrongly assumed, or a partial.
        if (
            pos.ledger == "live"
            and pos.broker_order_id
            and self.deps.reconcile_live is not None
            and now.minute % 5 == 0
        ):
            try:
                match, detail = await self.deps.reconcile_live(
                    symbol=pos.contract.symbol,
                    expiry=pos.contract.expiry,
                    strike=pos.contract.strike,
                    option_type=pos.contract.option_type,
                    expected_qty=pos.lots * self.deps.lot_size(pos.contract.symbol),
                )
                if match is False:
                    self._alert_once(
                        f"reconcile-mismatch-{pos.trade_id}",
                        f"🚨 CRITICAL: broker position differs from the engine "
                        f"({detail}) — trade #{pos.trade_id} "
                        f"{pos.contract.strike}{pos.contract.option_type}. "
                        "Verify in the broker app NOW.",
                    )
            except Exception as e:  # audit must never break management
                log.warning("algo.orch.reconcile_error", error=str(e))

        # LATCH the engine's exit decision the first time it appears. The
        # retry loop then works from the latch, never re-reading trades[-1]
        # (belt & braces with the entry-guard disarm: a re-entering engine
        # could otherwise repoint trades[-1] at a phantom trade and the real
        # exit would never be retried — found 2026-08-18).
        if (
            pos.pending_exit is None
            and not pos.engine.in_trade
            and pos.engine.trades
        ):
            last = pos.engine.trades[-1]
            if last.exit_price is not None:
                pos.pending_exit = (last.exit_price, last.exit_reason)
        exit_price: Optional[float] = None
        exit_reason = ""
        if pos.pending_exit is not None:
            exit_price, exit_reason = pos.pending_exit

        # EXPIRY-DAY FORCE-CLOSE — unconditional (overnight carry, End-Exit,
        # kills and §14 notwithstanding): the contract ceases to exist at the
        # 15:30 expiry settlement. 15:25 leaves 4–5 retry minutes for a live
        # order before the halt and avoids the expiring weekly's erratic final
        # prints. ``>=`` on the date: an adopted position found PAST expiry
        # closes on its first managed minute (a live sell for a dead contract
        # will be broker-rejected → the retry+CRITICAL path pages a human to
        # book the settlement manually).
        if (
            exit_price is None
            and pos.contract.expiry is not None
            and now.date() >= pos.contract.expiry
            and now.time() >= time(15, 25)
        ):
            ref = bar.c if bar is not None else pos.engine.last_close
            if ref is not None:
                exit_price = ref
                exit_reason = "EXPIRY_FORCE_CLOSE"
                self._audit_once(
                    f"expiry-close-{pos.trade_id}",
                    "runtime_expiry_force_close",
                    f"expiry-day force-close of trade #{pos.trade_id} "
                    f"{pos.contract.strike}{pos.contract.option_type} at 15:25 IST",
                    {"trade_id": pos.trade_id, "expiry": pos.contract.expiry.isoformat()},
                )

        # KILL-SWITCH FORCE-CLOSE. One rule for every switch (user rule
        # 2026-09-23, extending the 2026-09-17 Master Kill decision): a kill
        # that is ON while a trade runs exits the trade; while it stays ON no
        # new entry is taken; switching it OFF lets trading resume. Saving the
        # config also exits immediately (``kill_square_off``) — this per-minute
        # check is the backstop that also covers a failed live sell and a kill
        # already ON at the open. Ranks below the expiry close (that one is
        # unconditional) and above End-Exit.
        kill = self._kill_covering(cfg, pos, now) if exit_price is None else None
        if kill is not None:
            ref = bar.c if bar is not None else pos.engine.last_close
            if ref is not None:
                exit_price = ref
                exit_reason = kill[0]
                self._audit_kill_close(pos, kill)

        # §2.3 End-Exit — day-level, LAST zone only, on its End time.
        # INERT while overnight carry is ON (locked user decision 2026-08-19:
        # Pine parity — the exit ladder alone closes positions; a deliberate
        # §2.3 override, documented in the spec-conformance register).
        if (
            exit_price is None
            and not cfg.global_.overnight_carry
            and day_cfg is not None
            and day_cfg.end_exit_enabled
        ):
            last_zone = cfg.last_zone_id(pos.day)
            if last_zone is not None:
                z = day_cfg.zones.get(last_zone)
                if z is not None:
                    try:
                        end_t = time(*map(int, z.end.split(":")))
                    except ValueError:
                        end_t = None
                    if end_t is not None and now.time() >= end_t:
                        ref = bar.c if bar is not None else pos.engine.last_close
                        if ref is not None:
                            exit_price = ref
                            exit_reason = "ZONE_END_EXIT"

        if exit_price is None:
            return

        lot = self.deps.lot_size(pos.contract.symbol)
        result = await self.deps.execute_exit(
            cfg,
            symbol=pos.contract.symbol,
            expiry=pos.contract.expiry,
            strike=pos.contract.strike,
            option_type=pos.contract.option_type,
            raw_price=exit_price,
            lots=pos.lots,
            lot_size=lot,
            unique_id=f"algoX-{now:%H%M%S}",
            entry_order_id=pos.broker_order_id,
        )
        if result is None:
            # LIVE exit failed — position stays under management; the engine's
            # recorded exit persists, so the next minute retries the order.
            return
        pnl, fees = round_trip_pnl(
            cfg.global_.fees,
            entry_fill=pos.entry_fill,
            exit_fill=result.fill_price,
            lot_size=lot,
            lots=pos.lots,
        )
        balance = await self.deps.current_balance(cfg)
        alloc_pct = 100.0 if (day_cfg and day_cfg.all_in) else (
            day_cfg.allocation_pct if day_cfg else 0.0
        )
        allocated = balance * alloc_pct / 100
        pnl_pct = round(pnl / allocated * 10000) / 100 if allocated > 0 else None
        # The broker/paper exit already FILLED — a ledger failure past this
        # point must never resurrect or crash position management. Alert and
        # let the human patch the row instead.
        try:
            if pos.trade_id >= 0:
                await self.deps.close_trade(
                    pos.trade_id,
                    exit_ts=now,
                    exit_price=result.fill_price,
                    pnl_rupees=pnl,
                    pnl_pct=pnl_pct,
                    exit_reason=exit_reason,
                    fees=fees.as_dict(),
                )
            else:
                raise RuntimeError("trade was never recorded (trade_id=-1)")
            close_recorded = True
        except Exception as e:
            close_recorded = False
            self.deps.notify(
                f"trade-close-record-fail-{pos.trade_id}-{now:%H%M%S}",
                f"🚨 CRITICAL: exit FILLED but could not be recorded ({e}) — "
                f"{pos.side} {pos.contract.symbol} {pos.contract.strike}"
                f"{pos.contract.option_type} exit @ {result.fill_price:.2f}, "
                f"P&L {pnl:+.0f} ₹ — patch the ledger manually.",
            )
        # Audited OUTSIDE the try (a false "not recorded" critical invites a
        # duplicate manual row).
        if close_recorded and self.deps.audit_event is not None:
            self.deps.audit_event(
                "runtime_trade_exit",
                f"EXIT {pos.side} {pos.contract.symbol} {pos.contract.strike}"
                f"{pos.contract.option_type} @ {result.fill_price:.2f} — "
                f"{exit_reason} — P&L {pnl:+.0f} ₹ [{pos.day} {pos.zone_id}, "
                f"{pos.ledger}]",
                {"trade_id": pos.trade_id, "zone": pos.zone_id,
                 "exit_reason": exit_reason, "pnl": pnl,
                 "fill": result.fill_price, "ledger": pos.ledger},
            )
        # §8 Shadow Mode — close the paper twin with paper-simulated fills
        # off the same engine decision price. Best effort, like the entry.
        if pos.shadow_trade_id is not None:
            try:
                shadow_exit = sell_fill(cfg.global_.paper, exit_price).price
                s_pnl, s_fees = round_trip_pnl(
                    cfg.global_.fees,
                    entry_fill=pos.shadow_entry_fill,
                    exit_fill=shadow_exit,
                    lot_size=lot,
                    lots=pos.lots,
                )
                # The shadow twin carries the SAME lots as the live trade (§8
                # "fires the exact same command"), so its pnl_pct must use the
                # SAME allocated-capital denominator — dividing by the virtual
                # balance made the twin incomparable with its live original.
                await self.deps.close_trade(
                    pos.shadow_trade_id,
                    exit_ts=now,
                    exit_price=shadow_exit,
                    pnl_rupees=s_pnl,
                    pnl_pct=round(s_pnl / allocated * 10000) / 100 if allocated > 0 else None,
                    exit_reason=exit_reason,
                    fees=s_fees.as_dict(),
                )
            except Exception as e:
                self.deps.notify(
                    f"shadow-exit-fail-{pos.trade_id}",
                    f"⚠️ Shadow-Mode mirror exit failed ({e}) — live trade "
                    "unaffected, shadow twin left open in the paper ledger.",
                )
        if day_cfg is not None and day_cfg.alerts.telegram_trade_entry_exit:
            held = (
                int((now - pos.entry_ts).total_seconds() // 60)
                if pos.entry_ts is not None else None
            )
            self.deps.notify(
                f"trade-exit-{pos.trade_id}",
                exit_message(
                    now=now, side=pos.side, symbol=pos.contract.symbol,
                    strike=pos.contract.strike, option_type=pos.contract.option_type,
                    entry_fill=pos.entry_fill, exit_fill=result.fill_price,
                    exit_reason=exit_reason, pnl=pnl, pnl_pct=pnl_pct,
                    fees_total=fees.total, held_min=held, day=pos.day,
                    zone=pos.zone_id, ledger=pos.ledger, trade_id=pos.trade_id,
                ),
            )
        exit_candle = getattr(pos.engine, "last_exit_candle", None)
        if exit_candle is not None:
            self._exit_lock = (pos.contract.token, exit_candle)
        self.position = None

    def _risk_gate(
        self,
        day_cfg: Optional[DayConfig],
        today: date,
        closed: list[tuple[str, float]],
        realized: float,
        allocated: float,
        now: Optional[datetime] = None,
    ) -> list[str]:
        """§7 day-level risk gates off the CLOSED-trade ledger. Extracted so
        evaluate_minute can re-apply it after an exit books mid-pass. ``now``
        (the evaluated minute) stamps the kill/pause alerts."""
        risk_blocks: list[str] = []
        if day_cfg is None:
            return risk_blocks
        now = now or self._eval_now
        if allocated > 0:
            loss_limit = allocated * day_cfg.max_loss_pct / 100
            if realized <= -loss_limit:
                risk_blocks.append("max daily loss breached — day auto-killed")
                self._alert_once(
                    f"risk-daykill-{today}",
                    risk_kill_message(
                        "max_loss", now=now, realized=realized, limit=loss_limit,
                        pct=day_cfg.max_loss_pct, allocated=allocated,
                    ),
                )
                self._audit_once(
                    f"risk-daykill-{today}", "runtime_risk_kill",
                    f"max daily loss breached ({realized:.0f} ₹) — day auto-killed",
                    {"realized": realized, "allocated": allocated},
                )
            lock_limit = allocated * day_cfg.max_profit_lock_pct / 100
            if realized >= lock_limit:
                risk_blocks.append("profit lock reached — no new entries")
                self._alert_once(
                    f"risk-profitlock-{today}",
                    risk_kill_message(
                        "profit_lock", now=now, realized=realized, limit=lock_limit,
                        pct=day_cfg.max_profit_lock_pct, allocated=allocated,
                    ),
                )
                self._audit_once(
                    f"risk-profitlock-{today}", "runtime_risk_kill",
                    f"profit lock reached (+{realized:.0f} ₹) — no new entries",
                    {"realized": realized, "allocated": allocated},
                )
        streak = 0
        streak_loss = 0.0
        for _, pnl in reversed(closed):
            if pnl <= 0:
                streak += 1
                streak_loss += -pnl
            else:
                break
        if streak >= day_cfg.max_consec_losses or (
            allocated > 0
            and streak_loss >= allocated * day_cfg.max_consec_loss_pct / 100
            and streak > 0
        ):
            if not self.paused_reason:
                self.paused_reason = (
                    f"{streak} consecutive losses (−{streak_loss:.0f} ₹) — "
                    "manual resume required"
                )
                self._alert_once(
                    f"risk-pause-{today}",
                    pause_message(
                        now=now, streak=streak, streak_loss=streak_loss,
                        cap_trades=day_cfg.max_consec_losses,
                        cap_pct=day_cfg.max_consec_loss_pct,
                    ),
                )
                self._audit_once(
                    f"risk-pause-{today}", "runtime_risk_pause",
                    self.paused_reason,
                    {"streak": streak, "streak_loss": streak_loss},
                )
        return risk_blocks

    async def adopt_open_trade(self, today: Optional[date] = None) -> None:
        """Restart / overnight recovery: re-adopt the newest open ledger
        position (live or paper) — today's OR a prior session's carried one —
        so exit management, the broker reconcile and the §2.3 sequencing rule
        all continue. The backtest passes its simulated ``today``; live uses
        the wall clock. No-op when the deps hooks or a position are absent."""
        if self.position is not None:
            return
        if self.deps.open_trade_latest is None and self.deps.open_trade_today is None:
            return
        if today is None:
            from ..core.time_utils import now_ist

            today = now_ist().date()
        row = None
        for ledger in ("live", "paper"):
            if self.deps.open_trade_latest is not None:
                row = await self.deps.open_trade_latest(ledger)
            elif self.deps.open_trade_today is not None:
                row = await self.deps.open_trade_today(today, ledger)
            if row is not None:
                break
        if row is None:
            return
        cfg = await self.deps.get_config()
        carried = row.trade_date < today
        if carried and not cfg.global_.overnight_carry:
            # Never abandon it — adopt and manage — but this state should not
            # exist with carry OFF: page a human.
            self._alert_once(
                f"stale-open-{row.id}",
                f"🚨 CRITICAL: open position from {row.trade_date.isoformat()} "
                "found with overnight carry OFF — square off manually or "
                "re-enable carry. Adopted and managed meanwhile.",
            )
        if carried and row.expiry is not None and row.expiry < today:
            self._alert_once(
                f"expired-open-{row.id}",
                f"🚨 CRITICAL: open position's contract EXPIRED "
                f"({row.expiry.isoformat()}) while unmanaged — the ledger row "
                "will be closed at the first bar; book the actual settlement "
                "manually in the broker app.",
            )
        day_cfg = cfg.days.get(row.day)
        zone_cfg = day_cfg.zones.get(row.zone_id) if day_cfg is not None else None
        ot = "CE" if row.side == "CALL" else "PE"
        contract = Contract(
            symbol=row.index_symbol,
            expiry=row.expiry,
            strike=int(row.strike or 0),
            option_type=ot,  # type: ignore[arg-type]
        )
        engine: Any = None
        if zone_cfg is not None:
            try:
                # The documented resume mechanism: deterministically replay
                # the session through a fresh engine WITH entries armed, so it
                # re-discovers the trade and its current trail state.
                engine = await self.deps.build_engine(contract, zone_cfg, True)
            except Exception as e:
                log.warning("algo.orch.adopt_engine_failed", error=str(e))
        rebuilt = engine is not None and engine.in_trade
        if engine is None:
            engine = _AdoptedShellEngine()
        elif hasattr(engine, "entry_guard"):
            # Same rule as a fresh entry: a POSITION engine manages, it never
            # re-enters (the replay above ran with entries armed only to
            # rediscover the trade's state).
            engine.entry_guard = lambda: False
        self.position = _Position(
            trade_id=row.id,
            contract=contract,
            zone_id=row.zone_id,
            day=row.day,
            side=row.side,
            entry_fill=row.entry_price,
            lots=row.lots,
            ledger=row.ledger,
            engine=engine,
            broker_order_id=row.broker_order_id,
            entry_ts=(
                row.entry_ts.astimezone(_IST_TZ).replace(tzinfo=None)
                if getattr(row.entry_ts, "tzinfo", None) is not None else row.entry_ts
            ),
        )
        head = (
            f"🌙 OVERNIGHT RE-ADOPTION: position carried from "
            f"{row.trade_date.isoformat()} — trade "
            if carried
            else "🚨 RESTART RECOVERY: open position found — trade "
        )
        self.deps.notify(
            f"adopted-open-trade-{row.id}",
            head
            + f"#{row.id} {row.side} {row.index_symbol} {row.strike}{ot} × "
            f"{row.lots} lot(s) @ {row.entry_price:.2f} [{row.ledger}]. "
            + ("Engine REBUILT via full-life replay (trail/System-B state exact)."
               if rebuilt else
               "Engine NOT rebuilt — only the expiry force-close and the broker "
               "reconcile protect it. Verify in the broker app."),
        )
        log.info(
            "algo.orch.adopted_open_trade",
            trade_id=row.id, ledger=row.ledger, engine_rebuilt=rebuilt,
        )

    def resume(self) -> None:
        """Manual resume after a consecutive-loss auto-pause (§7.2)."""
        self.paused_reason = ""

    def _fill_position_status(self) -> None:
        if self.position is None:
            return
        p = self.position
        # Mark-to-market off the engine's last processed close (the same
        # bar the exit ladder sees) — the paper-session tiles read these.
        last = getattr(p.engine, "last_close", None)
        try:
            lot = int(self.deps.lot_size(p.contract.symbol))
        except Exception:
            lot = 0
        unreal_gross: Optional[float] = None
        unreal_pct: Optional[float] = None
        if last is not None and p.entry_fill:
            unreal_gross = round((float(last) - p.entry_fill) * lot * p.lots, 2)
            unreal_pct = round((float(last) - p.entry_fill) / p.entry_fill * 100, 2)
        entry_ts = getattr(p, "entry_ts", None)
        self.status.position = {
            "trade_id": p.trade_id,
            "side": p.side,
            "contract": p.contract.token,
            "entry": p.entry_fill,
            "lots": p.lots,
            "zone": p.zone_id,
            "ledger": p.ledger,
            "sub_scenario": p.engine.sub,
            "trail_sl": p.engine.trail_sl,
            "max_sl": p.engine.max_sl,
            "last_close": last,
            "lot_size": lot,
            "unrealized_gross": unreal_gross,
            "unrealized_pct": unreal_pct,
            "entry_ts": entry_ts.isoformat() if isinstance(entry_ts, datetime) else entry_ts,
            "strike": p.contract.strike,
            "option_type": p.contract.option_type,
            "expiry": p.contract.expiry.isoformat() if p.contract.expiry else None,
        }

    def _emit_decision(self, now: datetime) -> None:
        """Build and hand off the minute's decision record. Never raises."""
        if self.deps.record_decision is None:
            return
        try:
            d = self._decision
            st = self.status
            entered = d.get("entered_trade_id")
            if entered is not None:
                sub = (d.get("sizing") or {}).get("sub_scenario", "")
                decision, reason = "accept", f"entered via {sub}".strip()
            elif st.state == "in_trade":
                decision, reason = "manage", "open position managed (no new signals while in trade)"
            elif st.state in ("weekend", "idle"):
                decision, reason = "skip", st.state
            else:
                decision = "reject"
                reason = (
                    st.gate_blocks[0] if st.gate_blocks
                    else {
                        "killed": "killed",
                        "stale_data": "market data stale",
                        "gated": "zone gated",
                        "strategy_inactive": "strategy inactive",
                        # F1: the exact evidence, not a rule name that may no
                        # longer be the configured one.
                        "no_trade": self._decision.get("combine_reason")
                        or "indicators produced no direction",
                        "hunting_hold": "direction hold - hunts kept alive",
                        "no_strike_in_band": "no strike inside the premium band",
                        "no_engine_data": "no premium history for candidates",
                        "hunting": "hunting - no engine fired",
                    }.get(st.state, st.state)
                )
            # "Why did the combination produce CALL, PUT or NO TRADE?" must be
            # answerable for EVERY row, not only for rejections. When the
            # combination withheld a direction the reason above already IS the
            # evidence; when it produced one, the stage reason describes what
            # happened next, so append the evidence rather than lose it.
            combine_reason = self._decision.get("combine_reason")
            if combine_reason and st.direction in ("CALL", "PUT"):
                reason = f"{reason} · {combine_reason}" if reason else combine_reason
            readings = {
                k: {"signal": v, **(self._reading_details.get(k) or {})}
                for k, v in (st.readings or {}).items()
            }
            row = {
                "ts": now,
                "trade_date": now.date(),
                "day": d.get("day", ""),
                "zone_id": d.get("zone_id", ""),
                "symbol": d.get("symbol", ""),
                "expiry": d.get("expiry"),
                "ledger": d.get("ledger", ""),
                "config_version": d.get("config_version"),
                "stage": st.state,
                "state": st.state,
                "direction": st.direction or "",
                "unanimous": d.get("unanimous"),
                "decision": decision,
                "reason": reason,
                "gate_blocks": list(st.gate_blocks) if st.gate_blocks else None,
                "readings": readings or None,
                "candidates": d.get("candidates"),
                "sizing": d.get("sizing"),
                "position": st.position,
                "zone_snapshot": d.get("zone_snapshot"),
                "data_age_s": d.get("data_age_s"),
                "trade_id": entered if entered is not None else (
                    st.position.get("trade_id") if st.position else None
                ),
            }
            self.deps.record_decision(row)
        except Exception as e:  # noqa: BLE001 - tracing must never break a pass
            log.warning("algo.orch.decision_record_failed", error=str(e))

    def _alert_once(self, key: str, message: str) -> None:
        if key in self._alerted:
            return
        self._alerted.add(key)
        if key.startswith(("risk-", "kill-", "zonekill-")) and self._eval_now is not None:
            # Feeds the 15:45 summary's "Kills:" line (time + reason).
            head = message.split(" — ", 1)[-1].split(":")[0].strip() if " — " in message else message
            self.kill_events.append((self._eval_now, head[:80]))
        self.deps.notify(key, message)

    def _audit_once(
        self, key: str, event_type: str, detail: str, extra: dict
    ) -> None:
        """Append-only audit row for a runtime TRANSITION (§11.3) — deduped by
        key so a per-minute re-observation never floods the ledger."""
        if self.deps.audit_event is None:
            return
        akey = f"audit:{key}"
        if akey in self._alerted:
            return
        self._alerted.add(akey)
        try:
            self.deps.audit_event(event_type, detail, extra)
        except Exception:  # auditing must never break a decision pass
            log.warning("algo.orch.audit_failed", event_type=event_type)


# ══════════════════════════════════════════════════════════════════════════
# Runtime wiring — real providers + the supervised loop
# ══════════════════════════════════════════════════════════════════════════

async def evaluate_indicator_from_pairs(
    ind: IndicatorKey,
    zone_cfg: ZoneConfig,
    oi_change_pair: Callable[[int], Awaitable[Any]],
    ratio_pair: Callable[[int], Awaitable[Any]],
) -> IndicatorEval:
    """THE one indicator evaluation used by live AND backtest: the callers
    only differ in how the series pair is produced (SQL builders vs the
    day-frame twin). Returns the signal plus the JSON-safe trace so the
    decision record and the dashboard endpoints share one serialisation."""
    from .engines import mqae, mtf_ratio, oi_structure

    def _basket(p: Any) -> dict[str, Any]:
        return {
            "strike_min": getattr(p, "strike_min", None),
            "strike_max": getattr(p, "strike_max", None),
            "spot": getattr(p, "spot", None),
            "as_of": (p.timestamps[-1] if getattr(p, "timestamps", None) else None),
            "closed_minutes": len(getattr(p, "timestamps", []) or []),
        }

    if ind == "oi_change":
        pair = await oi_change_pair(zone_cfg.oi_structure.strikes_atm_window)
        if pair is None or len(pair.call_change_cr) < 2:
            return IndicatorEval("NO_TRADE", {"why": "insufficient series"})
        res = oi_structure.evaluate(pair.call_change_cr, pair.put_change_cr, zone_cfg.oi_structure)
        return IndicatorEval(res.signal, {
            "basket": _basket(pair),
            **oi_structure.result_to_dict(
                res, last_call_cr=pair.call_change_cr[-1], last_put_cr=pair.put_change_cr[-1]
            ),
        })
    if ind == "multi_tf":
        pair = await oi_change_pair(zone_cfg.mtf_ratio.strikes_atm_window)
        if pair is None or len(pair.call_change_cr) < 2:
            return IndicatorEval("NO_TRADE", {"why": "insufficient series"})
        from .series import mtf_input

        rows = mtf_ratio.rows_from_cumulative_series(
            *mtf_input(pair), zone_cfg.mtf_ratio.timeframes
        )
        res = mtf_ratio.evaluate(rows, zone_cfg.mtf_ratio)
        return IndicatorEval(res.reading, {"basket": _basket(pair), **mtf_ratio.result_to_dict(res)})
    from .series import ratio_pair_for_timeframe

    pair2 = ratio_pair_for_timeframe(
        await ratio_pair(zone_cfg.mqae.strikes_atm_window), zone_cfg.mqae.timeframe
    )
    if pair2 is None or len(pair2.green_pcr) < 2:
        return IndicatorEval("NO_TRADE", {
            "why": "insufficient series", "timeframe": zone_cfg.mqae.timeframe,
        })
    res = mqae.evaluate(pair2.green_pcr, pair2.yellow_ratio, zone_cfg.mqae)
    return IndicatorEval(res.signal, {
        "basket": _basket(pair2),
        **mqae.result_to_dict(res, last_green=pair2.green_pcr[-1], last_yellow=pair2.yellow_ratio[-1]),
    })


_orchestrator: Optional[ZoneOrchestrator] = None


def get_orchestrator() -> Optional[ZoneOrchestrator]:
    return _orchestrator


def disarm_entries(params: Any) -> None:
    """Warm-up rule (§5.3): the engine may not act on history it was never
    asked to watch. The two master switches cover every sub-scenario, so the
    per-kind ``scenarios`` map is left alone."""
    params.entry.enable_long = False
    params.entry.enable_retest = False


def rearm_entries(params: Any, src: Any) -> None:
    """Restore the zone's REAL entry configuration after a warm-up: master
    switches, the per-kind scenario map and the entry timeframe — every field
    that decides whether/when an entry may commit."""
    params.entry.enable_long = src.entry.enable_long
    params.entry.enable_retest = src.entry.enable_retest
    params.entry.scenarios = dict(src.entry.scenarios)
    params.entry.entry_timeframe_min = src.entry.entry_timeframe_min


def warmup_ump_engine(
    hist: Any, zone_cfg: ZoneConfig, entries_live_in_replay: bool
) -> UmpEngine:
    """Build a UmpEngine from a ``series.PremiumHistory`` and replay the
    session so far — the SINGLE warmup used by live wiring and the backtest
    runner alike, so the two can never drift.

    Warm-up runs with entries DISABLED (§5.3 — the engine may not act on
    history it was never asked to watch), unless the caller is
    deterministically resuming an open trade; afterwards the zone's real
    entry switches are re-armed.
    """
    from ..core.time_utils import IST
    from .engines.ump import Candle, UmpFeeds

    params = zone_cfg.ump if entries_live_in_replay else zone_cfg.ump.model_copy(deep=True)
    if not entries_live_in_replay:
        disarm_entries(params)
    engine = UmpEngine(params)
    engine.set_feeds(
        UmpFeeds(
            h1_candles=[Candle(o=o, h=h, l=l, c=c) for (o, h, l, c) in hist.prior_h1],
            daily=hist.daily,
            weekly=hist.weekly,
        )
    )
    for m in hist.session_minutes:
        ist = m.ts.astimezone(IST).replace(tzinfo=None)
        engine.process_minute(ist, m.o, m.h, m.l, m.c)
    if not entries_live_in_replay:
        # Warm-up done — arm the real entry switches for live hunting.
        rearm_entries(params, zone_cfg.ump)
    return engine


def warmup_ump_engine_full_life(
    minutes: Any,
    zone_cfg: ZoneConfig,
    entries_live_in_replay: bool,
    official_close: Optional[dict] = None,
) -> UmpEngine:
    """TV-parity warmup: replay the contract's ENTIRE stored life (all days,
    ``series.PremiumLife.minutes``) through a fresh engine with EMPTY feeds —
    daily/weekly/1H grow inside the engine via day rollover, so a live hunt's
    level set is byte-identical to the UMP dashboard's (both = the TradingView
    chart evaluated from the contract's first bar).

    Entries disarmed during the replay unless deterministically resuming an
    open trade (adoption: armed replay re-discovers the trade and its exact
    trail/System-B state, ~120 ms for a 4-week contract); afterwards the
    zone's real switches are re-armed."""
    from ..core.time_utils import IST

    from . import series as _ser

    params = zone_cfg.ump if entries_live_in_replay else zone_cfg.ump.model_copy(deep=True)
    if not entries_live_in_replay:
        disarm_entries(params)
    engine = UmpEngine(params, official_close=official_close)
    feed = list(minutes)
    # Daily/weekly context from BEFORE the first bar we replay. A contract is
    # listed days or weeks before it first trades, and the exchange publishes a
    # settlement price for every one of those idle days; TradingView plots them
    # and Pine's D/W feeds read them, so without this the gate opens late and
    # the DISCOVERY/BRIDGE rungs are built from the wrong candles. Anchored
    # strictly before the first replayed bar, so it adds no look-ahead.
    if feed:
        first_ist = feed[0].ts.astimezone(IST).replace(tzinfo=None).date()
        engine.seed_htf(_ser.htf_seed_from_official(official_close, first_ist))
    for m in feed:
        ist = m.ts.astimezone(IST).replace(tzinfo=None)
        engine.process_minute(ist, m.o, m.h, m.l, m.c)
    if not entries_live_in_replay:
        rearm_entries(params, zone_cfg.ump)
    return engine


def _runtime_deps() -> OrchestratorDeps:
    from ..core.notify import notify as _notify
    from ..market.symbols import get_registry
    from . import series as ser
    from . import trade_store as store
    from .config_store import get_config_store
    from .decisions import record_decision_live

    _live_version: list[Optional[int]] = [None]

    async def get_config() -> AlgoConfig:
        cv = await get_config_store().get_live()
        _live_version[0] = cv.version
        return cv.config

    async def evaluate_indicator(
        ind: IndicatorKey, day: Weekday, zone_id: ZoneId, zone_cfg: ZoneConfig, symbol: str
    ) -> IndicatorEval:
        from ..api._expiry_utils import resolve_expiry

        exp = await resolve_expiry(None, symbol)
        return await evaluate_indicator_from_pairs(
            ind, zone_cfg,
            lambda w: ser.build_oi_change_pair(symbol, exp, w),
            lambda w: ser.build_ratio_pair(symbol, exp, w),
        )

    async def select_strikes(
        symbol: str, side: Side, zone_cfg: ZoneConfig
    ) -> list[tuple[Contract, float]]:
        from ..api._expiry_utils import resolve_expiry

        exp = await resolve_expiry(None, symbol)
        ot: Literal["CE", "PE"] = "CE" if side == "CALL" else "PE"
        picked = await ser.select_strikes_in_band(
            symbol, exp, ot, zone_cfg.premium_min, zone_cfg.premium_max,
            zone_cfg.strike_scan_count,
        )
        return [
            (Contract(symbol=symbol, expiry=exp, strike=strike, option_type=ot),
             premium)
            for strike, premium in picked
        ]

    async def build_engine(
        contract: Contract, zone_cfg: ZoneConfig, entries_live_in_replay: bool
    ) -> Optional[UmpEngine]:
        # FULL-LIFE warmup (2026-08-19): live engines replay the contract's
        # whole stored life so their levels match the UMP dashboard / the
        # TradingView chart exactly — and an adoption replay reproduces a
        # carried overnight position's precise trail state.
        life = await ser.build_premium_life(
            contract.symbol, contract.expiry, contract.strike, contract.option_type
        )
        if life is None or not life.minutes:
            return None
        return warmup_ump_engine_full_life(
            life.minutes, zone_cfg, entries_live_in_replay, life.official_close
        )

    async def latest_quote(contract: Contract) -> Optional[tuple[float, datetime]]:
        return await ser.latest_contract_quote(
            contract.symbol, contract.expiry, contract.strike, contract.option_type
        )

    async def latest_minute(contract: Contract, now: datetime) -> Optional[MinuteBar]:
        from ..core.time_utils import ist_naive_to_utc

        target = (now - timedelta(minutes=1)).replace(second=0, microsecond=0)
        bar = await ser.fetch_premium_minute(
            contract.symbol, contract.expiry, contract.strike, contract.option_type,
            ist_naive_to_utc(target),
        )
        if bar is None:
            return None
        return MinuteBar(ts=target, o=bar.o, h=bar.h, l=bar.l, c=bar.c)

    def lot_size(symbol: str) -> int:
        # 0 = unknown → the orchestrator refuses the entry LOUDLY. The old
        # silent 75 fallback could size a trade with the wrong contract math
        # (NIFTY's real lot is 65; SENSEX's is 20).
        entry = get_registry().get(symbol)
        return entry.lot_size if entry is not None else 0

    async def current_balance(cfg: AlgoConfig) -> float:
        if cfg.global_.paper.paper_mode:
            since = cfg.global_.paper.session_started or date.today().isoformat()
            realized = await store.paper_realized_since(since)
            return cfg.global_.paper.virtual_balance + realized
        return cfg.global_.demat_balance

    from .broker.execution import execute_entry, execute_exit, reconcile_live_position

    async def _latency_wait(latency_ms: int) -> None:
        """§8.2 simulated latency, LIVE paper fills only: the order takes
        this long to reach the market (capped 2 s). Price impact stays
        modelled by the slippage %, NOT by re-pricing — re-pricing after
        sizing broke the §7 allocation cap, de-anchored the engine's max-SL
        from the actual fill, and made paper fills diverge from the backtest
        (found 2026-08-18). The backtest wires the raw executors directly."""
        import asyncio

        await asyncio.sleep(min(int(latency_ms), 2000) / 1000)

    async def execute_entry_live(cfg: AlgoConfig, **kw: Any) -> Optional[Any]:
        p = cfg.global_.paper
        if p.paper_mode and p.latency_ms > 0:
            await _latency_wait(p.latency_ms)
        return await execute_entry(cfg, **kw)

    async def execute_exit_live(cfg: AlgoConfig, **kw: Any) -> Optional[Any]:
        p = cfg.global_.paper
        # PAPER fills only (paper mode AND no broker order behind the
        # position) — a live broker exit must never be artificially delayed,
        # and pre-0012 live rows with an empty broker_order_id must not be
        # mistaken for paper.
        if p.paper_mode and not kw.get("entry_order_id") and p.latency_ms > 0:
            await _latency_wait(p.latency_ms)
        return await execute_exit(cfg, **kw)

    async def data_age_s(symbol: str) -> Optional[float]:
        """Seconds since the freshest stored option row for the TRADED
        symbol — a ~3 ms indexed probe, once per minute. Measures exactly
        what the strike selector and engine feeds depend on; a process-global
        flush timestamp could read "fresh" off another symbol's ticks (and
        its old wiring imported a name that didn't exist, crashing every
        pass — 2026-08-18)."""
        return await ser.freshest_option_row_age_s(symbol)

    def is_platform_holiday(d: date) -> bool:
        from ..core.holidays import is_nse_holiday

        return is_nse_holiday(d)

    # The loop holds only WEAK refs to tasks — a fire-and-forget task with no
    # strong reference can be garbage-collected before it runs and the audit
    # row silently vanishes. Keep each alive until done.
    _audit_tasks: set = set()

    def audit_event(event_type: str, detail: str, extra: dict) -> None:
        """Fire-and-forget append-only audit row (§11.3). Runs as a task so
        the sync decision path never blocks on the audit INSERT; ``audit``
        itself never raises."""
        import asyncio

        from .audit import audit as _audit_write

        try:
            t = asyncio.get_running_loop().create_task(
                _audit_write(
                    event_type, username="engine", scope="runtime",
                    new_value=detail, detail=extra,
                )
            )
            _audit_tasks.add(t)
            t.add_done_callback(_audit_tasks.discard)
        except RuntimeError:
            pass  # no loop (shouldn't happen in the runtime) — drop silently

    return OrchestratorDeps(
        get_config=get_config,
        evaluate_indicator=evaluate_indicator,
        select_strikes=select_strikes,
        build_engine=build_engine,
        latest_minute=latest_minute,
        lot_size=lot_size,
        current_balance=current_balance,
        today_closed=store.today_closed,
        insert_trade=store.insert_trade,
        close_trade=store.close_trade,
        record_signal=store.record_signal_transition,
        notify=_notify,
        execute_entry=execute_entry_live,
        execute_exit=execute_exit_live,
        reconcile_live=reconcile_live_position,
        open_trade_today=store.open_trade_today,
        open_trade_latest=store.open_trade_latest,
        today_entries=store.today_entries,
        data_age_s=data_age_s,
        is_platform_holiday=is_platform_holiday,
        audit_event=audit_event,
        record_decision=record_decision_live,
        config_version=lambda: _live_version[0],
        latest_quote=latest_quote,
    )


async def run_orchestrator_loop() -> None:
    """Supervised clock loop: one evaluation per closed minute during the
    session, a few seconds past the boundary so the bucket has persisted."""
    import asyncio

    from ..core.time_utils import IST, now_ist

    global _orchestrator
    _orchestrator = ZoneOrchestrator(_runtime_deps())
    log.info("algo.orch.started")

    # Restart recovery FIRST: if the previous process died holding an open
    # position, re-adopt it before the first evaluation (and alert loudly).
    try:
        await _orchestrator.adopt_open_trade()
    except Exception:
        log.exception("algo.orch.adopt_failed")

    last_evaluated: Optional[datetime] = None
    was_in_session = False
    # §5 — the 15:45 daily summary is time-based and restart-safe: the day
    # it was last sent for is recovered from the audit log at boot.
    from .daily_summary import SUMMARY_EVENT, build_daily_summary, summary_due

    summary_sent_for: Optional[date] = None
    try:
        from .audit import last_event_date

        summary_sent_for = await last_event_date(SUMMARY_EVENT)
    except Exception:
        log.warning("algo.orch.summary_marker_read_failed")
    while True:
        try:
            now = now_ist().replace(tzinfo=None)
            in_session = (
                now.weekday() < 5
                and time(9, 15) <= now.time() <= time(15, 45)
            )
            if was_in_session and not in_session and _orchestrator.position is not None:
                p = _orchestrator.position
                try:
                    carry_cfg = await _orchestrator.deps.get_config()
                    carry_on = carry_cfg.global_.overnight_carry
                except Exception:
                    carry_on = False
                past_expiry = (
                    p.contract.expiry is not None
                    and now.date() >= p.contract.expiry
                )
                if carry_on and not past_expiry:
                    # Pine parity: carrying overnight is the INTENDED
                    # lifecycle — informative, not critical.
                    _orchestrator.deps.notify(
                        f"overnight-open-{p.trade_id}",
                        f"🌙 Position carried overnight (Pine parity): trade "
                        f"#{p.trade_id} {p.contract.strike}"
                        f"{p.contract.option_type} × {p.lots} lot(s) "
                        f"[{p.ledger}], NRML product — the exit ladder "
                        "resumes 09:15 next session.",
                    )
                    if _orchestrator.deps.audit_event is not None:
                        try:
                            _orchestrator.deps.audit_event(
                                "runtime_overnight_carry",
                                f"trade #{p.trade_id} carried overnight",
                                {"trade_id": p.trade_id, "ledger": p.ledger},
                            )
                        except Exception:
                            log.warning("algo.orch.carry_audit_failed")
                else:
                    # Carry OFF, or the 15:25 expiry close failed every
                    # retry: exit retries have STOPPED. A human must act.
                    _orchestrator.deps.notify(
                        f"overnight-open-{p.trade_id}",
                        f"🚨 CRITICAL: session over but trade #{p.trade_id} "
                        f"{p.contract.strike}{p.contract.option_type} × "
                        f"{p.lots} lot(s) [{p.ledger}] is STILL OPEN"
                        + (" past its EXPIRY" if past_expiry else "")
                        + " — the engine will not retry again today. "
                        "Square off manually in the broker app.",
                    )
            holiday_now = False
            try:
                if _orchestrator.deps.is_platform_holiday is not None:
                    holiday_now = bool(_orchestrator.deps.is_platform_holiday(now.date()))
            except Exception:
                holiday_now = False
            if summary_due(now, summary_sent_for, holiday_now):
                # §5 — the 15:45 daily summary (per-day toggle; delivered via
                # Telegram, the platform's wired alert transport). Marked sent
                # BEFORE building so a failing build can never re-fire every
                # tick; the audit row makes it restart-safe.
                summary_sent_for = now.date()
                try:
                    cfg = await _orchestrator.deps.get_config()
                    day_cfg = cfg.days.get(_WEEKDAYS[now.weekday()])
                    if day_cfg is not None and day_cfg.alerts.telegram_trade_entry_exit:
                        ledger = "paper" if cfg.global_.paper.paper_mode else "live"
                        from . import trade_store as _ts

                        closed_rows = await _ts.closed_on(now.date(), ledger)
                        try:
                            balance = await _orchestrator.deps.current_balance(cfg)
                            alloc_pct = 100.0 if day_cfg.all_in else day_cfg.allocation_pct
                            allocated = balance * alloc_pct / 100
                        except Exception:
                            allocated = 0.0
                        open_pos = None
                        p = _orchestrator.position
                        if p is not None:
                            open_pos = {
                                "side": p.side, "strike": p.contract.strike,
                                "option_type": p.contract.option_type, "lots": p.lots,
                                "entry_fill": p.entry_fill,
                                "unrealized_gross": (
                                    (p.engine.last_close - p.entry_fill)
                                    * _orchestrator.deps.lot_size(p.contract.symbol) * p.lots
                                    if p.engine.last_close is not None else None
                                ),
                            }
                        text_ = build_daily_summary(
                            day=now.date(), ledger=ledger, closed=closed_rows,
                            allocated=allocated, kill_events=list(_orchestrator.kill_events),
                            open_position=open_pos, now=now,
                        )
                        _orchestrator.deps.notify(f"daily-summary-{now.date()}", text_)
                    if _orchestrator.deps.audit_event is not None:
                        _orchestrator.deps.audit_event(
                            SUMMARY_EVENT, f"daily summary sent for {now.date()}",
                            {"date": now.date().isoformat()},
                        )
                except Exception:
                    log.exception("algo.orch.daily_summary_failed")
            was_in_session = in_session
            boundary = now.replace(second=0, microsecond=0)
            due = in_session and now.second >= 3 and boundary != last_evaluated
            if due:
                last_evaluated = boundary
                await _orchestrator.evaluate_minute(boundary)
        except Exception:
            log.exception("algo.orch.evaluate_failed")
        await asyncio.sleep(1.0)
