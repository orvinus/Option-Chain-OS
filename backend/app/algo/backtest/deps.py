"""BacktestDeps — the OrchestratorDeps implementation for replay.

Maps every dep to an in-memory provider over the prefetched ``DayFrame``
(plus one real SQL per hunt start for the UMP premium history, which is
already historical-capable). The frozen run config always has
``paper_mode=True`` / ``shadow_mode=False`` pinned at run creation, so the
orchestrator derives ledger="paper", the Shadow/reconcile paths stay inert
and ``execute_entry``/``execute_exit`` (the REAL broker.execution functions,
reused) route straight to the paper simulator — identical fills to live
paper trading.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal, Optional

from ...core.time_utils import IST, ist_naive_to_utc
from .. import series as ser
from ..broker.execution import execute_entry, execute_exit
from ..config_models import AlgoConfig, IndicatorKey, Weekday, ZoneConfig, ZoneId
from ..engines import mqae, mtf_ratio, oi_structure
from ..orchestrator import (
    disarm_entries,
    rearm_entries,
    Contract,
    MinuteBar,
    OrchestratorDeps,
    Side,
    warmup_ump_engine_full_life,
)
from ..trade_store import OpenTrade
from .data import (
    DayFrame,
    band_candidate_strikes,
    minute_bar,
    oi_change_pair_at,
    pick_strikes_in_band,
    ratio_pair_at,
)

_WEEKDAY_NAMES = [
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
]


async def prefetch_premium_minutes(
    frame: DayFrame, cfg: AlgoConfig
) -> dict[tuple, list]:
    """One batched SQL for every band-candidate contract of ``frame``'s day —
    the unbounded-lookback premium query pays its (large) plan cost once
    instead of once per hunted contract. Superset by construction; a contract
    it misses still gets the exact single-contract fetch on demand."""
    strikes = _band_strikes(frame, cfg)
    if not strikes:
        return {}
    return await ser.fetch_premium_minutes_batch(
        frame.symbol, frame.expiry, strikes,
        frame.open_utc + timedelta(minutes=frame.minutes_per_day),
    )


def _band_strikes(frame: DayFrame, cfg: AlgoConfig) -> list[int]:
    """Every strike any zone of ``frame``'s weekday could hunt."""
    day_cfg = cfg.days[_WEEKDAY_NAMES[frame.trade_date.weekday()]]
    lo: Optional[float] = None
    hi: Optional[float] = None
    for z in day_cfg.zones.values():
        lo = z.premium_min if lo is None else min(lo, z.premium_min)
        hi = z.premium_max if hi is None else max(hi, z.premium_max)
    if lo is None or hi is None:
        return []
    return band_candidate_strikes(frame, lo, hi)


async def prefetch_official_closes(
    frame: DayFrame, cfg: AlgoConfig
) -> dict[tuple, dict]:
    """One batched EOD query for the whole day's candidate band.

    The per-contract form was called once per contract per day (10-30 queries
    a day, ~4,000 over a 145-day run) while the batched form already existed.
    The result is the engine's daily/weekly context — official H/L/C for the
    days a contract traded, plus the settlement bars for the days it was listed
    and idle — so it is read on literally every hunt.
    """
    strikes = _band_strikes(frame, cfg)
    if not strikes:
        return {}
    return await ser.fetch_official_closes_batch(frame.symbol, frame.expiry, strikes)

BalanceMode = Literal["compounding", "fixed_per_day"]


class InMemoryLedger:
    """The run's trade ledger — the source of truth for risk counters during
    the day, flushed to ``algo_backtest_trades`` at day end.

    ``compounding`` reproduces live paper-session semantics exactly
    (``virtual_balance + realized_since(session_start)``, which includes
    today's realized P&L intraday); ``fixed_per_day`` keeps the intraday
    behavior but drops the cross-day carry.
    """

    def __init__(self, starting_balance: float, mode: BalanceMode) -> None:
        self.starting_balance = starting_balance
        self.mode = mode
        self.trades: list[dict[str, Any]] = []
        self._seq = 0

    @staticmethod
    def _exit_day(t: dict[str, Any]) -> Optional[date]:
        """Exit calendar day, tolerant of in-memory naive datetimes and
        DB-seeded tz-aware ones (both carry IST wall-clock semantics)."""
        v = t.get("exit_ts")
        if v is None:
            return None
        return v.date() if hasattr(v, "date") else date.fromisoformat(str(v)[:10])

    def balance_for(self, today: date) -> float:
        # EXIT-day attribution (overnight carry): realized P&L belongs to the
        # day the position closed — same convention as the live ledger.
        if self.mode == "compounding":
            realized = sum(
                t["pnl_rupees"] for t in self.trades if t.get("exit_ts") is not None
            )
        else:
            realized = sum(
                t["pnl_rupees"]
                for t in self.trades
                if self._exit_day(t) == today
            )
        return self.starting_balance + realized

    def today_closed(self, today: date) -> list[tuple[str, float]]:
        """(zone_id, pnl) of trades that CLOSED today (exit-day attribution,
        any entry day) in exit order — feeds the risk counters."""
        closed = [t for t in self.trades if self._exit_day(t) == today]
        closed.sort(key=lambda t: str(t["exit_ts"]))
        return [(t["zone_id"], float(t["pnl_rupees"])) for t in closed]

    def today_entries(self, today: date) -> list[str]:
        """zone_ids ENTERED today (open or closed) — max_trades consumption."""
        return [t["zone_id"] for t in self.trades if t["trade_date"] == today]

    def insert(self, **kw: Any) -> int:
        self._seq += 1
        row = dict(kw)
        row["seq"] = self._seq
        row["exit_ts"] = None
        self.trades.append(row)
        return self._seq

    def close(self, trade_id: int, **kw: Any) -> None:
        for t in self.trades:
            if t["seq"] == trade_id:
                t.update(kw)
                return

    def open_trade(self) -> Optional[dict[str, Any]]:
        for t in self.trades:
            if t.get("exit_ts") is None:
                return t
        return None

    def rows_for_day(self, today: date) -> list[dict[str, Any]]:
        return [t for t in self.trades if t["trade_date"] == today]

    def seed_closed(self, prior: list[dict[str, Any]]) -> None:
        """Resume support: replay previously-persisted trades into the ledger
        so cross-day compounding continues deterministically."""
        for t in prior:
            self.trades.append(dict(t))
            self._seq = max(self._seq, int(t["seq"]))


class EventCollector:
    """Signals (transition-deduped like ``record_signal_transition``),
    notifications, orchestrator status transitions and per-trade UMP engine
    captures — everything the day-replay UI scrubs through."""

    def __init__(self) -> None:
        self.signals: list[dict[str, Any]] = []
        self.notifications: list[dict[str, Any]] = []
        self.status_timeline: list[dict[str, Any]] = []
        self.ump_captures: dict[int, dict[str, Any]] = {}   # trade seq → bundle
        self._last_reading: dict[tuple[date, str, str], str] = {}
        self.now: datetime = datetime(1970, 1, 1)
        # Per-minute decision records (the filter-by-filter trace); flushed
        # to algo_backtest_decisions by the runner at day end.
        self.decisions: list[dict[str, Any]] = []

    def record_signal(
        self,
        *,
        ts: datetime,
        trade_date: date,
        day: str,
        zone_id: str,
        indicator: str,
        reading: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> bool:
        key = (trade_date, zone_id, indicator)
        if self._last_reading.get(key) == reading:
            return False
        self._last_reading[key] = reading
        self.signals.append(
            {
                "ts": ts,
                "trade_date": trade_date,
                "day": day,
                "zone_id": zone_id,
                "indicator": indicator,
                "reading": reading,
                "payload": payload,
            }
        )
        return True

    def notify(self, key: str, text: str) -> None:
        self.notifications.append({"ts": self.now, "key": key, "text": text})

    def record_decision(self, row: dict[str, Any]) -> None:
        self.decisions.append(dict(row))

    def status_transition(self, ts: datetime, state: str, zone: str, direction: str) -> None:
        last = self.status_timeline[-1] if self.status_timeline else None
        if last and (last["state"], last["zone"], last["direction"]) == (
            state, zone, direction
        ):
            return
        self.status_timeline.append(
            {"ts": ts, "state": state, "zone": zone, "direction": direction}
        )

    def capture_engine(self, trade_seq: int, engine: Any) -> None:
        self.ump_captures[trade_seq] = {
            "events": [
                {"ts": e.ts, "kind": e.kind, "price": e.price, "text": e.text}
                for e in engine.events
            ],
            "levels": [
                {"price": lv.price, "type": lv.type, "name": lv.name}
                for lv in engine.levels
            ],
            "engine_trades": [
                {
                    "entry_ts": t.entry_ts,
                    "entry_price": t.entry_price,
                    "sub_scenario": t.sub_scenario,
                    "base_level": t.base_level,
                    "exit_ts": t.exit_ts,
                    "exit_price": t.exit_price,
                    "exit_reason": t.exit_reason,
                }
                for t in engine.trades
            ],
        }


@dataclass
class BacktestDeps:
    """Builds the OrchestratorDeps for one simulated day."""
    cfg: AlgoConfig
    frame: DayFrame
    ledger: InMemoryLedger
    collector: EventCollector
    lot_sizes: dict[str, int] = field(default_factory=dict)
    now: datetime = datetime(1970, 1, 1)     # IST naive; runner sets per minute
    config_version: Optional[int] = None      # trace metadata (None = inline doc)
    # FROZEN at run creation (settings["platform_holidays"]): the platform's
    # NSE holiday file as it stood, so a later file edit cannot change a
    # resumed run. Empty = pre-feature run → config list only (byte-identical).
    platform_holidays: frozenset = frozenset()
    # Session-filtered full-life feed per (strike, ot) (False = no data).
    life_cache: dict[tuple, Any] = field(default_factory=dict)
    # Day-open engines per (strike, ot, id(zone_cfg)): the contract's PRIOR
    # life replayed once — each hunt deep-copies and replays only today.
    day_open_engines: dict[tuple, Any] = field(default_factory=dict)
    # Raw premium minutes per (strike, option_type), warmed in one batch.
    premium_minutes: dict[tuple, list] = field(default_factory=dict)
    _premium_warmed: bool = False
    # Official daily bars per (strike, option_type), warmed in one batch.
    official_closes: dict[tuple, dict] = field(default_factory=dict)
    _official_warmed: bool = False

    def set_now(self, now_ist_naive: datetime) -> None:
        self.now = now_ist_naive
        self.collector.now = now_ist_naive

    async def _warm_premium_minutes(self) -> None:
        """Lazy fallback for the non-pipelined path (the runner normally
        passes a pre-warmed ``premium_minutes`` from its prefetch task)."""
        self._premium_warmed = True
        fetched = await prefetch_premium_minutes(self.frame, self.cfg)
        for k, v in fetched.items():
            self.premium_minutes.setdefault(k, v)

    async def _warm_official_closes(self) -> None:
        """Lazy fallback for the non-pipelined path (see the premium twin)."""
        self._official_warmed = True
        fetched = await prefetch_official_closes(self.frame, self.cfg)
        for k, v in fetched.items():
            self.official_closes.setdefault(k, v)

    def as_orchestrator_deps(self) -> OrchestratorDeps:
        cfg = self.cfg
        frame = self.frame
        ledger = self.ledger
        collector = self.collector

        async def get_config() -> AlgoConfig:
            return cfg

        async def evaluate_indicator(
            ind: IndicatorKey, day: Weekday, zone_id: ZoneId,
            zone_cfg: ZoneConfig, symbol: str,
        ):
            # The SAME evaluation function as the live runtime
            # (orchestrator.evaluate_indicator_from_pairs); only the series
            # producers differ: the frame's per-cursor twins replace the SQL
            # builders, with the frame's expiry replacing resolve_expiry.
            from ..orchestrator import evaluate_indicator_from_pairs

            async def _oi(w: int):
                return oi_change_pair_at(frame, w, self.now)

            async def _ratio(w: int):
                return ratio_pair_at(frame, w, self.now)

            return await evaluate_indicator_from_pairs(ind, zone_cfg, _oi, _ratio)

        async def select_strikes(
            symbol: str, side: Side, zone_cfg: ZoneConfig
        ) -> list[tuple[Contract, float]]:
            ot = "CE" if side == "CALL" else "PE"
            picked = pick_strikes_in_band(
                frame, ot, zone_cfg.premium_min, zone_cfg.premium_max, self.now,
                zone_cfg.strike_scan_count,
            )
            return [
                (Contract(symbol=symbol, expiry=frame.expiry, strike=strike,
                          option_type=ot),
                 premium)
                for strike, premium in picked
            ]

        async def build_engine(
            contract: Contract, zone_cfg: ZoneConfig, entries_live_in_replay: bool
        ):
            # FULL-LIFE warmup (TV parity, 2026-08-19) — identical level
            # evolution to live/dashboard. Hunts restart constantly under
            # signal flicker (profiled: 300+/day), so the cost is controlled
            # by a two-level cache:
            #   1. the contract's session-filtered full-life feed, once/day;
            #   2. a DAY-OPEN engine per (contract, zone) — the prior life
            #      replayed once; each hunt deep-copies it (exact state) and
            #      replays only today's minutes to the cursor, disarmed, then
            #      re-arms — byte-equivalent to one continuous disarmed
            #      replay (pinned by test_rebuild_equals_continuous_feed).
            mkey = (contract.strike, contract.option_type)
            life = self.life_cache.get(mkey)
            if life is None:
                if not self._premium_warmed:
                    await self._warm_premium_minutes()
                minutes = self.premium_minutes.get(mkey)
                if minutes is None:
                    minutes = await ser.fetch_premium_minutes(
                        contract.symbol, contract.expiry, contract.strike,
                        contract.option_type,
                        frame.open_utc + timedelta(minutes=frame.minutes_per_day),
                    )
                    self.premium_minutes[mkey] = minutes
                if not self._official_warmed:
                    await self._warm_official_closes()
                official = self.official_closes.get(mkey)
                if official is None:
                    official = await ser.fetch_official_closes(
                        contract.symbol, contract.expiry, contract.strike,
                        contract.option_type,
                    )
                    self.official_closes[mkey] = official
                life = ser.premium_life_from_minutes(
                    minutes, contract.strike, contract.option_type,
                    now_utc=frame.open_utc + timedelta(minutes=frame.minutes_per_day + 1),
                    official_close=official,
                )
                self.life_cache[mkey] = life if life is not None else False
            if life is None or life is False:
                return None
            now_utc = ist_naive_to_utc(self.now)

            if entries_live_in_replay:
                # Adoption (once per day at most): full ARMED replay to the
                # cursor re-discovers the carried trade's exact state.
                feed = [
                    m for m in life.minutes
                    if m.ts + timedelta(minutes=1) <= now_utc
                ]
                if not feed:
                    return None
                return warmup_ump_engine_full_life(
                    feed, zone_cfg, True, life.official_close
                )

            ekey = (contract.strike, contract.option_type, id(zone_cfg))
            base = self.day_open_engines.get(ekey)
            if base is None:
                prior = [m for m in life.minutes if m.ts < frame.open_utc]
                base = (
                    warmup_ump_engine_full_life(
                        prior, zone_cfg, False, life.official_close
                    )
                    if prior
                    else False
                )
                self.day_open_engines[ekey] = base
            today_tail = [
                m for m in life.minutes
                if m.ts >= frame.open_utc and m.ts + timedelta(minutes=1) <= now_utc
            ]
            if base is False:
                # First stored day of the contract — no prior life.
                if not today_tail:
                    return None
                return warmup_ump_engine_full_life(
                    today_tail, zone_cfg, False, life.official_close
                )
            eng = copy.deepcopy(base)
            p = eng.p
            disarm_entries(p)
            for m in today_tail:
                ist = m.ts.astimezone(IST).replace(tzinfo=None)
                eng.process_minute(ist, m.o, m.h, m.l, m.c)
            rearm_entries(p, zone_cfg.ump)
            return eng

        async def latest_minute(contract: Contract, now: datetime) -> Optional[MinuteBar]:
            from datetime import timedelta

            target = (now - timedelta(minutes=1)).replace(second=0, microsecond=0)
            bar = minute_bar(frame, contract.strike, contract.option_type, target)
            if bar is None:
                return None
            ts, o, h, l, c = bar
            return MinuteBar(ts=ts, o=o, h=h, l=l, c=c)

        def lot_size(symbol: str) -> int:
            # 0 = unknown → the orchestrator refuses the entry loudly (run
            # creation validates the registry, so this should never hit).
            return int(self.lot_sizes.get(symbol, 0))

        async def current_balance(_cfg: AlgoConfig) -> float:
            return ledger.balance_for(self.now.date())

        async def today_closed(trade_date: date, _ledger: str) -> list[tuple[str, float]]:
            return ledger.today_closed(trade_date)

        async def today_entries(trade_date: date, _ledger: str) -> list[str]:
            return ledger.today_entries(trade_date)

        async def open_trade_latest(ledger_name: str) -> Optional[OpenTrade]:
            row = ledger.open_trade()
            if row is None or row.get("ledger", "paper") != ledger_name:
                return None
            return OpenTrade(
                id=int(row["seq"]),
                trade_date=row["trade_date"],
                day=row["day"],
                zone_id=row["zone_id"],
                index_symbol=row["index_symbol"],
                side=row["side"],
                token=row.get("token", ""),
                strike=row.get("strike"),
                expiry=row.get("expiry"),
                entry_ts=row["entry_ts"],
                entry_price=float(row["entry_price"]),
                lots=int(row["lots"]),
                ledger=row.get("ledger", "paper"),
                sub_scenario=row.get("sub_scenario", ""),
                broker_order_id=str(row.get("broker_order_id") or ""),
            )

        async def insert_trade(**kw: Any) -> int:
            return ledger.insert(**kw)

        async def close_trade(trade_id: int, **kw: Any) -> None:
            ledger.close(trade_id, **kw)

        async def record_signal(**kw: Any) -> bool:
            return collector.record_signal(**kw)

        async def data_age_s(symbol: str) -> Optional[float]:
            """Backtest twin of the live freshness probe: whole minutes since
            the last basket tick at/before the closed minute, in seconds. The
            live probe reads 0–59 s on a live minute; this reads 0 s — the same
            side of the 180 s gate. None = no tick yet today (entries blocked,
            exactly as live behaves before the first row lands)."""
            i = frame.cursor_idx(self.now) - 1
            if i < 0 or not frame.last_tick_upto:
                return None
            lt = frame.last_tick_upto[min(i, len(frame.last_tick_upto) - 1)]
            if lt < 0:
                return None
            return float((i - lt) * 60)

        holidays = self.platform_holidays

        def is_platform_holiday(d: date) -> bool:
            return d.isoformat() in holidays

        return OrchestratorDeps(
            get_config=get_config,
            evaluate_indicator=evaluate_indicator,
            select_strikes=select_strikes,
            build_engine=build_engine,
            latest_minute=latest_minute,
            lot_size=lot_size,
            current_balance=current_balance,
            today_closed=today_closed,
            insert_trade=insert_trade,
            close_trade=close_trade,
            record_signal=record_signal,
            notify=collector.notify,
            execute_entry=execute_entry,
            execute_exit=execute_exit,
            reconcile_live=None,
            open_trade_latest=open_trade_latest,
            today_entries=today_entries,
            record_decision=collector.record_decision,
            config_version=lambda: self.config_version,
            # Live-gate parity (2026-09-09, user-approved): the feed-staleness
            # gate and the platform-holiday union now run in the backtest too.
            data_age_s=data_age_s,
            is_platform_holiday=is_platform_holiday if holidays else None,
        )
