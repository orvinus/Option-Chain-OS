"""Ultra Master Pro — entry/exit state machine (Pine v24 lines 397–1313).

The engine consumes 1-minute premium bars, aggregates exchange-aligned
entry candles (``entry.entry_timeframe_min`` = 5 or 15 minutes; "5m" below
means that candle) and mirrors the Pine script's per-tick evaluation order:

    level build (freeze-gated) → levelsReady latch → Step 1 trigger check
    (confirmed close only) → Step 2 test-candle entries (every tick) →
    Retest entries (every tick) → Step 3 trade management (trail systems +
    exit chain P1→P2→P2b→P3→P4).

Port-fidelity facts, each anchored to the source:

- The script's "confirmed" and "live" 5m series are the SAME security call
  (no offset), so both are the FORMING candle's running values; they only
  equal a full candle on its closing tick. The port therefore keeps ONE set
  of running values per tick, with ``new5m_close`` true on the closing tick.
- Timestamps: ``em5_time`` is the forming candle's START time — all
  non-repaint latches key on it, and the two-candle guard is
  ``bar_index > trig_bar AND em5_time > trig_time``.
- Pine's ``var`` rollback: intrabar ticks REPLAY the bar from the previous
  candle's committed state. Three consequences are encoded explicitly here
  (per-candle stamps stand in for rollback):
    * ERROR-1 — during the ENTRY candle ``post_high`` is the running CLOSE
      (each Pine tick re-seeds it); only from the NEXT candle does it ratchet
      on running highs. A pre-entry spike can never advance the ladder.
    * ERROR-3 — during the candle a trail was set/raised, ``trail_low`` is
      the running CLOSE (re-seeded per tick); accumulation of lows starts on
      the NEXT candle, so the wick that created the entry/raise can never
      satisfy the trail exit. Same for System B's ``sb_trail_low``.
    * System B's arm→raise advances at most ONE rung per 5-minute candle —
      every Pine tick evaluates its if/elif chain from the COMMITTED value,
      so a candle nets a single step. System A's trail ladder is DIFFERENT
      since Pine vl72 (2026-09-02, "MULTI-LEVEL JUMP FIX"): the target rung
      is computed from ``post_high`` in one pass, so a single candle can walk
      Base→Q1→Q2(→Q3 for Retest) and lands on the highest rung it cleared.
      Because ``post_high`` is monotone inside a candle, applying the pass
      in place per minute tick nets the same rung as Pine's per-tick
      rollback replay.
- ERROR-5: the exit chain is one strict if/elif ladder per tick and the
  ``exit_fired``/``exit_time`` latch survives for the rest of the candle, so
  the FIRST exit that fires stands (no rollback exists here, but the latch
  still blocks the Retest re-assertion path from reopening a just-closed
  trade inside the same candle).
- P1 Max SL deliberately uses the whole-candle running low (no post-entry
  guard) — as in the source.
- P4 Base SL fires on confirmed close only, and only while the trail has not
  risen above Base; a Retest trade pins it to the FROZEN ``entry_base``
  (the v24 fix) while ``base_level`` re-anchors upward for the Q-ladder.
- System B (Body Closing only): dormant until post-entry high crosses
  NB×(1−zonePct/100); ratchets Bottom → Mid; its labels draw on confirmed
  close (SB_stage vs SB_stage_labelled) while the STATE ratchets intrabar.
- Levels FREEZE from entry until full exit (rebuilds resume next confirmed
  close); rebuilds happen only on confirmed 5m closes (intrabar stability).

What the engine deliberately does NOT do: End-Exit (a day-level rule owned by
the orchestrator, §2.3), direction selection (the filter layer's job — the
engine runs on whichever option-premium series it is given), and position
sizing (§7 is the risk manager's).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable, Literal, Optional

from ....core.time_utils import session_close_min, session_last_bar_min
from ...config_models import UmpParams
from .levels import Candle, Level, build_levels, detect_h1_reversals

EngineState = Literal["IDLE", "WATCHING", "IN_TRADE"]


@dataclass
class UmpFeeds:
    """Higher-timeframe context derived from the same premium series."""
    h1_candles: list[Candle] = field(default_factory=list)
    daily: list[tuple[float, float, float]] = field(default_factory=list)  # (H,L,C) newest first
    weekly: Optional[tuple[float, float, float]] = None

    @property
    def loaded(self) -> bool:
        # Pine's Data-Ready Gate: daily×3, weekly and the 1H feed must all
        # have resolved before any entry activity is allowed.
        return len(self.daily) >= 3 and self.weekly is not None and len(self.h1_candles) >= 1


@dataclass(frozen=True)
class HtfSeed:
    """The daily/weekly context that already exists at the OPEN of the first
    bar we replay — i.e. what Pine's ``request.security(.., "D"/"W", [..][1],
    lookahead_on)`` feeds resolve to on that bar.

    We fold our own D/W candles out of the 1-minute bars we replay, so a
    contract whose first REPLAYED minute is not its first LISTED day starts
    blind: the Data-Ready Gate waits days for 3 dailies plus a week, and the
    DISCOVERY/BRIDGE rungs are then built from the wrong candles. TradingView
    has no such blind spot because the exchange publishes a settlement price
    for every listed-but-idle day and TV plots it as a flat bar. Seeding those
    days in closes the gap without changing one line of the fold arithmetic.

    ``daily`` is newest-first and already capped at Pine's d1/d2/d3.
    ``weekly`` is the last COMPLETED ISO week. The ``week_*`` fields describe
    the RUNNING (still-open) ISO week, so that when it completes mid-replay it
    publishes its true extremes rather than only the part we replayed — for a
    contract that starts trading on a Thursday, the week's real high lives in
    the Monday settlement bar.
    """
    daily: list[tuple[float, float, float]] = field(default_factory=list)
    weekly: Optional[tuple[float, float, float]] = None
    # Running-week carry-in: ISO (year, week), how many sessions of it are
    # already accounted for, and their official high / low / last close.
    week_iso: Optional[tuple[int, int]] = None
    week_days: int = 0
    week_h: Optional[float] = None
    week_l: Optional[float] = None
    week_c: Optional[float] = None

    @property
    def empty(self) -> bool:
        return not self.daily and self.weekly is None and self.week_iso is None


@dataclass
class UmpEvent:
    ts: str
    kind: str            # S1A..S3C, R1, R2, TRAIL_SET, TRAIL_RAISE, SB_SET,
    #                      SB_RAISE, MAX_SL, TRAIL_EXIT, SB_EXIT, TARGET, BASE_SL
    price: float
    text: str


@dataclass
class UmpTrade:
    entry_ts: str
    entry_price: float
    sub_scenario: str
    base_level: float
    exit_ts: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: str = ""

    @property
    def open(self) -> bool:
        return self.exit_ts is None


@dataclass
class _Zone:
    b: float
    zt: float
    um: float
    lm: float
    zb: float


class UmpEngine:
    def __init__(
        self,
        params: UmpParams,
        level_provider: Optional[Callable[[], list[Level]]] = None,
        official_close: Optional[dict] = None,
        record_history: bool = False,
    ) -> None:
        self.p = params
        self._external_levels = level_provider
        # Trade-entry candle timeframe in minutes (5 or 15; §1 2026-09-09).
        # Both align on the wall clock (60 is divisible), so ``minute // tf``
        # is session-anchored for either. Every other timeframe (1H, daily,
        # weekly) is independent of it.
        self._tf = int(getattr(params.entry, "entry_timeframe_min", 5) or 5)
        # Replay honesty (UMP chart replay): when asked, the engine keeps a
        # compact history of the level set (one row per CHANGE, at most one
        # per confirmed candle) and of its trade state (one row per change),
        # so a client can reconstruct "what was known at bar N" without
        # re-running the engine. Off by default — zero cost, zero behaviour
        # change for live, backtest and the tests.
        self._record_history = record_history
        self.levels_history: list[tuple[str, list[Level]]] = []
        self.state_history: list[dict] = []
        self._last_state_row: Optional[tuple] = None
        self.feeds = UmpFeeds()
        # Exchange OFFICIAL daily (high, low, close) per IST date (NSE
        # bhavcopy; the close is the last-30-minute weighted average, NOT the
        # last trade). TradingView's D/W bars carry these values, so Pine's
        # d1..d3 / weekly feeds — and every Bullish/Bearish-Trend and
        # Body-Match level derived from them — do too. A legacy close-only
        # float per date is accepted. Missing dates fold H/L from the bars
        # and fall back to the mean of the session's last 30 1-min closes
        # (within ~0.5 % of the official print; measured 2026-09-02 across
        # 12 contract-days).
        self.official_close: dict = dict(official_close or {})
        self._day_tail: list[float] = []
        # Optional external precondition consulted immediately before ANY
        # entry commits (§4.2 — e.g. the zone's premium band). Returning
        # False suppresses that entry attempt; armed triggers stay armed.
        self.entry_guard: Optional[Callable[[], bool]] = None

        # ── unified level set (Pine outP) ──
        self.levels: list[Level] = []
        self.levels_ready = False

        # ── 5m aggregation ──
        self._cur_start: Optional[datetime] = None
        self._cur: Optional[list[float]] = None      # [o, h, l, c] running
        # Fresh OI-integrated entry cycle (2026-09-25): when a hunt is armed
        # mid-candle, THAT candle is read only from the first armed minute on
        # — its pre-signal minutes can never trigger, test or retest.
        self._cycle_pending = False
        self._fresh: Optional[list[float]] = None
        self._fresh_candle: Optional[datetime] = None
        # Entry-candle trail option: the minute the entry fired and the high of
        # the minute being processed (process_minute stores it before _tick).
        self._entry_minute: Optional[datetime] = None
        self._minute_h: Optional[float] = None
        self._bar_index = -1                          # 5m candle index
        self._em5_time: Optional[datetime] = None     # forming candle START
        # Confirmed-close bookkeeping (Pine L131-134): the previous CONFIRMED
        # 5m close — a candle closing at exactly the same price does NOT
        # count as a new close (new5mClose stays false: Step 1, P4 and the
        # SB label all skip that candle). _closed_for marks which candle has
        # had its confirmed-close tick, so a missing final minute still gets
        # one when the next window's first bar arrives.
        self._prev_5m_close: Optional[float] = None
        self._closed_for: Optional[datetime] = None
        self._last_min_ts: Optional[datetime] = None
        # ── session-anchored intraday 1H accumulation (Pine's h1_struct
        # grows ALL session; NSE hourly bars run 09:15–10:15, 10:15–11:15…) ──
        self._h1_bucket: Optional[tuple] = None
        self._h1_cur: Optional[list[float]] = None
        # ── multi-day continuity: running day/ISO-week H/L/C, folded into
        # feeds.daily / feeds.weekly at the first bar of the next period —
        # exactly when Pine's lookahead_on [1]-offset feeds resolve the
        # completed bar. Seeded single-day replays never cross a date, so
        # this machinery is inert there and the seeded feeds stay frozen. ──
        self._day_key: Optional[date] = None
        self._day_hlc: Optional[list[float]] = None
        self._week_key: Optional[tuple] = None
        self._week_hlc: Optional[list[float]] = None
        self._week_days = 0                     # sessions seen in the running week
        self._week_done_h = float("-inf")       # official H/L of the week's COMPLETED days
        self._week_done_l = float("inf")

        # ── trigger state (Step 1/2) ──
        self.trig = False
        self._trig_bar = 0
        self._trig_time: Optional[datetime] = None
        self._trig_scen = 0
        self._trig_zone: Optional[_Zone] = None

        # ── trade state ──
        self.in_trade = False
        self.entry_price: Optional[float] = None
        self.base_level: Optional[float] = None
        self.entry_base: Optional[float] = None       # frozen for Retest P4
        self.zone: Optional[_Zone] = None
        self.max_sl: Optional[float] = None
        self.trail_sl: Optional[float] = None
        self.sub = ""
        self.post_high: Optional[float] = None        # ERROR-1
        self.trail_low: Optional[float] = None        # ERROR-3
        self.sb_trail_sl: Optional[float] = None      # System B
        self.sb_trail_low: Optional[float] = None
        self.sb_stage = 0
        self.sb_stage_labelled = 0
        self._exit_fired = False                      # ERROR-5
        self._exit_time: Optional[datetime] = None
        self._retest_fired = False                    # ERROR-2
        self._retest_lock_time: Optional[datetime] = None
        # Rollback-equivalence stamps (see module docstring):
        self._entry_candle: Optional[datetime] = None
        self._trail_seed_candle: Optional[datetime] = None
        self._sb_seed_candle: Optional[datetime] = None

        self.events: list[UmpEvent] = []
        self.trades: list[UmpTrade] = []

    # ------------------------------------------------------------------ api

    def set_feeds(self, feeds: UmpFeeds) -> None:
        self.feeds = feeds

    def seed_htf(self, seed: Optional[HtfSeed]) -> None:
        """Install pre-replay daily/weekly context (see ``HtfSeed``).

        Call ONCE, before the first ``process_minute``. Everything here is
        strictly earlier than the first replayed bar, so no look-ahead is
        introduced; ``_roll_htf`` then extends the seeded feeds exactly as it
        grows empty ones, and the cap of 3 dailies / one weekly is preserved.
        """
        if seed is None or seed.empty:
            return
        if seed.daily:
            self.feeds.daily = list(seed.daily)[:3]
        if seed.weekly is not None:
            self.feeds.weekly = seed.weekly
        if seed.week_iso is not None and seed.week_days > 0:
            assert seed.week_h is not None and seed.week_l is not None
            assert seed.week_c is not None
            self._week_key = seed.week_iso
            self._week_days = seed.week_days
            self._week_done_h = seed.week_h
            self._week_done_l = seed.week_l
            self._week_hlc = [seed.week_h, seed.week_l, seed.week_c]

    @property
    def last_minute_ts(self) -> Optional[datetime]:
        """Newest 1-minute bar this engine has processed (warm-up included)."""
        return self._last_min_ts

    @property
    def last_exit_candle(self) -> Optional[datetime]:
        """Start of the entry candle in which the latest exit fired."""
        return self._exit_time if self._exit_fired else None

    def lock_candle(self, candle_start: datetime) -> None:
        """Carry Pine's exit-candle lockout into a freshly built engine: no
        entry of any kind inside the candle in which a position on this same
        contract just exited (a new hunt engine would otherwise have
        forgotten it)."""
        if self.in_trade:
            return
        self._exit_fired = True
        self._exit_time = candle_start

    def start_entry_cycle(self) -> None:
        """Start a FRESH entry calculation at the OI signal (user rule
        2026-09-25): levels and higher-timeframe feeds keep running, but every
        entry latch is cleared and the candle in progress is re-read from the
        next minute onward, so nothing that happened before the signal can
        influence this cycle's entry. Called by the orchestrator for every
        hunt it builds — i.e. at each new OI signal / direction change."""
        if self.in_trade:
            return
        self.trig = False
        self._trig_scen = 0
        self._trig_zone = None
        self._trig_time = None
        self._retest_fired = False
        self._retest_lock_time = None
        self.post_high = None
        self._cycle_pending = True
        self._fresh = None
        self._fresh_candle = None

    def reset_direction_state(self) -> None:
        """§5.3: when the filter layer's direction flips while the engine is
        still hunting (not in a trade), every in-progress calculation is
        discarded — never carried into the new direction."""
        if self.in_trade:
            return
        self.trig = False
        self._trig_scen = 0
        self._trig_zone = None

    @property
    def state(self) -> EngineState:
        if self.in_trade:
            return "IN_TRADE"
        return "WATCHING" if self.trig else "IDLE"

    @property
    def last_close(self) -> Optional[float]:
        """The forming 5m candle's running close (the live premium)."""
        return self._cur[3] if self._cur is not None else None

    def nearest_above(self, price: float) -> Optional[float]:
        nb: Optional[float] = None
        for lv in self.levels:
            if lv.price > price and (nb is None or lv.price < nb):
                nb = lv.price
        return nb

    def nearest_below(self, price: float) -> Optional[float]:
        """Pine ``f_nearest``'s support half — the closest level strictly
        below ``price`` (dashboard's 🟢 Support row)."""
        ns: Optional[float] = None
        for lv in self.levels:
            if lv.price < price and (ns is None or lv.price > ns):
                ns = lv.price
        return ns

    # ---------------------------------------------------------- level build

    def _build(self, fallback_close: float) -> list[Level]:
        if self._external_levels is not None:
            return self._external_levels()
        h1_struct = detect_h1_reversals(
            self.feeds.h1_candles,
            self.p.structural.body_match_pts,
            self.p.institutional.price_floor_inr,
        )
        return build_levels(
            h1_struct, self.feeds.daily, self.feeds.weekly, self.p, fallback_close
        )

    def _feeds_loaded(self) -> bool:
        if self._external_levels is not None:
            return True
        return self.feeds.loaded

    # ------------------------------------------------------------- geometry

    def _zone_of(self, b: float) -> _Zone:
        zp = self.p.visual.zone_width_pct
        return _Zone(
            b=b,
            zt=b * (1 + zp / 100),
            um=b * (1 + zp / 200),
            lm=b * (1 - zp / 200),
            zb=b * (1 - zp / 100),
        )

    def _find_zone(self, c5: float, o5: float) -> tuple[Optional[_Zone], int]:
        """Pine ``f_findZone`` — unified origin rule: the trigger candle's
        open must be strictly BELOW Base. SC1: close > ZT; SC2: B < close <
        UM; SC3: UM ≤ close ≤ ZT. Nearest candidate wins by scenario-specific
        distance."""
        best: Optional[_Zone] = None
        best_scen = 0
        best_d: Optional[float] = None
        for lv in self.levels:
            b = lv.price
            if b <= 0.0:
                continue
            z = self._zone_of(b)
            if o5 >= b:
                continue
            if c5 > z.zt:
                d = c5 - z.zt
                scen = 1
            elif c5 > b and c5 < z.um:
                d = abs(c5 - b)
                scen = 2
            elif c5 >= z.um and c5 <= z.zt:
                d = abs(c5 - z.um)
                scen = 3
            else:
                continue
            if best_d is None or d < best_d:
                best_d = d
                best = z
                best_scen = scen
        return best, best_scen

    # ----------------------------------------------------------- main entry

    # NSE session anchor: 09:15 = minute 555. Hourly bars are session-anchored
    # (09:15–10:15, …). The final partial hour (15:15–close) never completes
    # INTRADAY, but on the NSE 60m chart it IS a real completed short bar the
    # next morning — the day-change bucket flush below appends it then,
    # matching both TV and the prior-day fold in series.py. The session's
    # last bar is DATE-DEPENDENT (15:29 before 2026-08-03, 15:39 since — see
    # core.time_utils.session_last_bar_min).
    _SESSION_OPEN_MIN = 9 * 60 + 15

    @staticmethod
    def _in_session(ts: datetime, mins: int) -> bool:
        return UmpEngine._SESSION_OPEN_MIN <= mins <= session_last_bar_min(ts.date())

    def _accumulate_session_h1(self, ts: datetime, o: float, h: float, l: float, c: float) -> None:
        """Fold the 1-minute stream into session-anchored 1H candles and
        append each COMPLETED hour to the 1H feed, so intraday structural
        reversals become levels during the session (Pine's ``h1_struct``
        grows all day; the old port froze the feed at the prior day)."""
        mins = ts.hour * 60 + ts.minute
        key = (
            (ts.date(), (mins - self._SESSION_OPEN_MIN) // 60)
            if self._in_session(ts, mins)
            else None
        )
        if key != self._h1_bucket:
            if self._h1_cur is not None:
                self.feeds.h1_candles.append(
                    Candle(o=self._h1_cur[0], h=self._h1_cur[1],
                           l=self._h1_cur[2], c=self._h1_cur[3])
                )
            self._h1_cur = [o, h, l, c] if key is not None else None
            self._h1_bucket = key
        elif self._h1_cur is not None:
            self._h1_cur[1] = max(self._h1_cur[1], h)
            self._h1_cur[2] = min(self._h1_cur[2], l)
            self._h1_cur[3] = c

    def _confirmed_close_tick(self, ts: datetime) -> None:
        """The candle's confirmed-close evaluation (Pine's ``new5mClose``
        tick), with the equal-close suppression of L131-134: a close exactly
        equal to the previous confirmed close is NOT a new close."""
        assert self._cur is not None and self._cur_start is not None
        completed_close = self._cur[3]
        new5m = (
            self._prev_5m_close is None
            or completed_close != self._prev_5m_close
        )
        self._prev_5m_close = completed_close
        self._closed_for = self._cur_start
        self._tick(ts, new5m)

    def _accumulate_day(self, ts: datetime, h: float, l: float, c: float) -> None:
        """Running session-day and ISO-week H/L/C (session-window bars only —
        out-of-session prints must never shape a daily candle)."""
        mins = ts.hour * 60 + ts.minute
        if not self._in_session(ts, mins):
            return
        self._day_key = ts.date()
        if self._day_hlc is None:
            self._day_hlc = [h, l, c]
            self._day_tail = []
            self._week_days += 1
        else:
            self._day_hlc[0] = max(self._day_hlc[0], h)
            self._day_hlc[1] = min(self._day_hlc[1], l)
            self._day_hlc[2] = c
        if mins >= session_close_min(ts.date()) - 30:
            self._day_tail.append(c)
        if self._week_key is None:
            self._week_key = ts.date().isocalendar()[:2]
        if self._week_hlc is None:
            self._week_hlc = [h, l, c]
        else:
            self._week_hlc[0] = max(self._week_hlc[0], h)
            self._week_hlc[1] = min(self._week_hlc[1], l)
            self._week_hlc[2] = c

    def _official_day(self) -> Optional[tuple[Optional[float], Optional[float], float]]:
        if self._day_key is None:
            return None
        v = self.official_close.get(self._day_key)
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return (None, None, float(v)) if v > 0 else None
        # (H, L, C) or the wider (H, L, C, O) the settlement reader returns.
        h, l, c = v[0], v[1], v[2]
        if c is None or c <= 0:
            return None
        return (h, l, float(c))

    def _official_day_close(self) -> float:
        """The completed day's close as TradingView's daily bar carries it:
        the exchange's official close when known, else the last-30-minute
        mean of 1-min closes, else the last trade."""
        assert self._day_hlc is not None
        known = self._official_day()
        if known is not None:
            return known[2]
        if self._day_tail:
            return sum(self._day_tail) / len(self._day_tail)
        return self._day_hlc[2]

    def _official_day_hlc(self) -> tuple[float, float, float]:
        """(H, L, C) of the completed day as the chart's D bar shows it —
        official high/low when the bhavcopy row exists, else the bar fold."""
        assert self._day_hlc is not None
        h, l = self._day_hlc[0], self._day_hlc[1]
        known = self._official_day()
        if known is not None:
            oh, ol, _c = known
            h = oh if oh is not None else h
            l = ol if ol is not None else l
        return (h, l, self._official_day_close())

    def _roll_htf(self, new_date: date) -> None:
        """Day rollover: fold the completed day into ``feeds.daily`` (newest
        first, Pine d1..d3 → cap 3) and, at an ISO-week boundary, the
        completed week into ``feeds.weekly``. Fires on the FIRST bar of the
        new date — the moment Pine's lookahead_on completed-prior-bar feeds
        resolve the new values. Extends seeded feeds just as correctly as it
        grows empty ones."""
        if self._day_hlc is not None:
            day_h, day_l, day_close = self._official_day_hlc()
            self.feeds.daily.insert(0, (day_h, day_l, day_close))
            del self.feeds.daily[3:]
            if self._week_hlc is not None:
                # The week = max/min of its OFFICIAL daily H/L, closing at
                # its last session's OFFICIAL close. _week_hlc accumulates
                # the completed days' official values plus the running day.
                if self._week_days <= 1:
                    self._week_hlc[0], self._week_hlc[1] = day_h, day_l
                else:
                    self._week_hlc[0] = max(self._week_done_h, day_h)
                    self._week_hlc[1] = min(self._week_done_l, day_l)
                self._week_done_h, self._week_done_l = self._week_hlc[0], self._week_hlc[1]
                self._week_hlc[2] = day_close
        self._day_key = None
        self._day_hlc = None
        self._day_tail = []
        if self._week_key is not None and new_date.isocalendar()[:2] != self._week_key:
            if self._week_hlc is not None:
                self.feeds.weekly = (
                    self._week_hlc[0], self._week_hlc[1], self._week_hlc[2]
                )
            self._week_key = None
            self._week_hlc = None
            self._week_days = 0
            self._week_done_h = float("-inf")
            self._week_done_l = float("inf")

    def process_minute(self, ts: datetime, o: float, h: float, l: float, c: float) -> None:
        """Advance one closed 1-minute bar (one 'tick' of the 5m candle).

        Multi-day contract: bars from consecutive sessions may arrive
        back-to-back (the TV chart has no overnight bars). Ordering is
        load-bearing — the deferred confirmed close of the PREVIOUS candle
        must evaluate under the previous period's feeds (on TV that close
        tick still sees daily[1] = the day before and the pre-flush 1H set),
        and only then do the day rollover and 1H flush run."""
        tf = self._tf
        window_start = ts.replace(minute=(ts.minute // tf) * tf, second=0, microsecond=0)
        if (
            self._cur_start is not None
            and window_start != self._cur_start
            and self._closed_for != self._cur_start
            and self._last_min_ts is not None
        ):
            # The previous candle's final minute never arrived (feed gap or
            # session end): fire its confirmed-close tick from its stored
            # running values BEFORE any feed mutation — Pine's data feed
            # always delivers the close; ours must not lose it to a missing
            # bar, and it must see the old day's feeds.
            self._confirmed_close_tick(self._last_min_ts)

        if self._last_min_ts is not None and ts.date() != self._last_min_ts.date():
            self._roll_htf(ts.date())

        self._accumulate_session_h1(ts, o, h, l, c)
        self._accumulate_day(ts, h, l, c)

        if self._cur_start is None or window_start != self._cur_start:
            self._cur_start = window_start
            self._cur = [o, h, l, c]
            self._bar_index += 1
        else:
            assert self._cur is not None
            self._cur[1] = max(self._cur[1], h)
            self._cur[2] = min(self._cur[2], l)
            self._cur[3] = c
        # Fresh entry cycle: the first armed minute opens a post-signal view
        # of the candle it falls in; later minutes of that SAME candle extend
        # it. A new candle simply reads its own full OHLC again.
        if self._cycle_pending:
            self._cycle_pending = False
            self._fresh_candle = window_start
            self._fresh = [o, h, l, c]
        elif self._fresh is not None and self._fresh_candle == window_start:
            self._fresh[1] = max(self._fresh[1], h)
            self._fresh[2] = min(self._fresh[2], l)
            self._fresh[3] = c
        self._em5_time = self._cur_start
        self._last_min_ts = ts
        self._minute_h = h

        if ts.minute % tf == tf - 1:
            # The window's final minute closes the candle on this very tick.
            # (A 15m window that the session cuts short — 15:30–15:39 — never
            # sees its final minute; its confirmed close arrives through the
            # deferred path above when the next window's first bar comes in,
            # exactly like a missing final minute of a 5m candle.)
            self._confirmed_close_tick(ts)
        else:
            self._tick(ts, new5m=False)
        if self._record_history:
            self._record_state(ts.isoformat())

    # ------------------------------------------------------------- the tick

    def _tick(self, ts: datetime, new5m: bool) -> None:
        assert self._cur is not None and self._em5_time is not None
        # Inside the candle that straddles the OI signal, every rule reads the
        # post-signal part only (see start_entry_cycle). Its close is the same
        # as the full candle's, so levels and confirmed-close logic agree.
        view = (
            self._fresh
            if self._fresh is not None and self._fresh_candle == self._em5_time
            else self._cur
        )
        live_o, live_h, live_l, live_c = view
        em5_time = self._em5_time
        ts_iso = ts.isoformat()

        # ── LEVEL BUILD + FREEZE GATE (confirmed closes only) ──
        if not self.in_trade and (new5m or not self.levels):
            self.levels = self._build(fallback_close=live_c)
            if self._record_history:
                self._record_levels(ts_iso)
        if self._feeds_loaded() and self.levels:
            self.levels_ready = True

        p_entry = self.p.entry

        # ── STEP 1 — TRIGGER CHECK (confirmed green close) ──
        if (
            p_entry.enable_long
            and self.levels_ready
            and new5m
            and not self.in_trade
            and not self.trig
            and live_c > live_o
            and live_c > 0
            and self.levels
        ):
            z, scen = self._find_zone(live_c, live_o)
            if z is not None and scen > 0:
                self.trig = True
                self._trig_bar = self._bar_index
                self._trig_time = em5_time
                self._trig_scen = scen
                self._trig_zone = z

        # A trade that exited THIS candle locks out any new entry until the
        # candle ends — Pine's L1051-1062 forced-close block kills a
        # same-candle re-entry the moment it appears (before Step 3 ever
        # runs), so the state evolution is identical to never entering.
        exit_candle = self._exit_fired and self._exit_time == em5_time

        # ── STEP 2 — TEST CANDLE + LIVE ENTRY (every tick) ──
        if (
            self.trig
            and not self.in_trade
            and not exit_candle
            and self._trig_time is not None
            and self._bar_index > self._trig_bar
            and em5_time > self._trig_time
        ):
            z = self._trig_zone
            assert z is not None
            eff_low = live_l
            eff_high = live_h
            # Bars of the ENTRY timeframe (Pine counts chart bars), as
            # wall-clock seconds — see ump-pine-parity (L820).
            timeout_s = p_entry.trigger_timeout_bars * self._tf * 60
            if (em5_time - self._trig_time).total_seconds() > timeout_s:
                self.trig = False
                self._trig_scen = 0
                self._trig_zone = None
            else:
                scen = self._trig_scen
                if scen == 1:
                    if eff_low <= z.b and eff_high >= z.b:
                        self._enter(ts_iso, z.b, z, "S1A")
                    elif eff_low <= z.um and eff_low > z.b:
                        self._enter(ts_iso, z.um, z, "S1B")
                    elif eff_low <= z.zt and eff_low > z.um:
                        self._enter(ts_iso, z.zt, z, "S1C")
                elif scen == 2:
                    if eff_low <= z.b and eff_high >= z.b:
                        self._enter(ts_iso, z.b, z, "S2A")
                    elif eff_low > z.b and eff_low <= z.um:
                        self._enter(ts_iso, z.um, z, "S2B")
                elif scen == 3:
                    if eff_low <= z.b and eff_high >= z.b:
                        self._enter(ts_iso, z.b, z, "S3A")
                    elif eff_low <= z.um and eff_low > z.b:
                        self._enter(ts_iso, z.um, z, "S3B")
                    elif live_c > z.zt and live_o < z.zt:
                        self._enter(ts_iso, z.zt, z, "S3C")

        # ── RETEST re-assertion (ERROR-2/4 interplay — benign without
        #    rollback, kept for exactness of the exit-wins rule) ──
        if (
            self._retest_fired
            and self._retest_lock_time == em5_time
            and not self.in_trade
            and not (self._exit_fired and self._exit_time == em5_time)
        ):
            self.in_trade = True

        # ── RETEST ENTRY MODEL (intrabar, same candle) ──
        if (
            p_entry.enable_retest
            and self.levels_ready
            and not self.in_trade
            and not exit_candle
            and not self.trig
            and self.levels
        ):
            for lv in self.levels:
                if self.in_trade:
                    break
                if lv.price <= 0.0 or lv.type == 4:   # never on MEDIAN
                    continue
                z = self._zone_of(lv.price)
                # R1 — top→bottom→top retest of the BASE.
                if live_o > z.b and live_l <= z.b and live_c > z.b:
                    self._enter(ts_iso, z.um, z, "R1", retest=True, lock_time=em5_time)
                # R2 — retest of the UPPER MEDIAN, wick never reaching Base.
                elif live_o > z.um and live_l <= z.um and live_l > z.b and live_c > z.um:
                    self._enter(ts_iso, z.zt, z, "R2", retest=True, lock_time=em5_time)

        # ── STEP 3 — TRADE MANAGEMENT ──
        if not self.in_trade:
            return
        assert self.base_level is not None and self.zone is not None
        b = self.base_level
        msl = self.max_sl

        nb = self.nearest_above(b)
        q2 = (b + nb) / 2.0 if nb is not None else None
        q1 = (b + q2) / 2.0 if q2 is not None else None
        q3 = (q2 + nb) / 2.0 if q2 is not None and nb is not None else None
        is_retest = self.sub in ("R1", "R2")

        # ERROR-1 — during the entry candle the post-entry high IS the running
        # close (rollback re-seeds it every tick); afterwards it ratchets on
        # running highs.
        if self._entry_candle == em5_time:
            if (
                self.p.entry.trail_counts_entry_candle_high
                and self._entry_minute is not None
                and self._last_min_ts is not None
                and self._last_min_ts > self._entry_minute
                and self._minute_h is not None
            ):
                # User rule (2026-09-25): a LATER minute of the entry candle
                # counts its own high — the price really traded there after
                # the entry. Running max from the entry minute's close.
                base = self.post_high if self.post_high is not None else live_c
                self.post_high = max(base, self._minute_h, live_c)
            else:
                self.post_high = live_c
        elif self.post_high is None:
            self.post_high = live_c
        else:
            self.post_high = max(self.post_high, live_h)

        # Trail raise steps (ratchet up only) — run BEFORE any exit check.
        # MULTI-LEVEL JUMP (Pine vl72 "MULTI-LEVEL JUMP FIX"): the target rung
        # is computed from post_high in ONE pass, so a candle that crosses
        # several Q-levels walks through every rung it cleared and lands on
        # the highest one (Base→Q1→Q2, plus Q3 for Retest), with ONE event at
        # the final level. Thresholds and the ratchet-only rule are unchanged;
        # the old else-if chain could only advance one rung per candle.
        if nb is not None and q1 is not None and q2 is not None and q3 is not None:
            prev_trail = self.trail_sl
            new_trail = self.trail_sl
            if self.post_high >= q1 and (new_trail is None or new_trail < b - 0.001):
                new_trail = b
            if self.post_high >= q2 and (new_trail is None or new_trail < q1 - 0.001):
                new_trail = q1
            if self.post_high >= q3 and (new_trail is None or new_trail < q2 - 0.001):
                new_trail = q2
            if (
                is_retest
                and self.post_high >= nb
                and (new_trail is None or new_trail < q3 - 0.001)
            ):
                new_trail = q3
            # Apply only if it advanced (ratchet up only); one label per advance.
            if new_trail is not None and (prev_trail is None or new_trail > prev_trail + 0.001):
                self.trail_sl = new_trail
                self._trail_seed_candle = em5_time
                # Pine labels the FINAL rung: "TRAIL SET" only when the trail
                # lands on Base; every higher rung is "TRAIL ↑" — even when
                # the trail was unset and jumped straight to Q1+ (vl72
                # L1144-1156; the 2026-09-02 TV diff caught 27 such labels).
                if abs(new_trail - b) <= 0.001:
                    self._event(ts_iso, "TRAIL_SET", new_trail, f"TRAIL SET {new_trail:.2f}")
                else:
                    self._event(ts_iso, "TRAIL_RAISE", new_trail, f"TRAIL ↑ {new_trail:.2f}")
            # Retest re-anchor: the ladder keeps climbing into the next zone;
            # the frozen entry_base (P4) never moves.
            if is_retest and self.post_high >= nb and live_c > nb:
                self.base_level = nb

        # ERROR-3 — during the set/raise candle the post-trail low IS the
        # running close; lows accumulate only from the next candle on.
        if self.trail_sl is not None:
            if self._trail_seed_candle == em5_time:
                self.trail_low = live_c
            elif self.trail_low is None:
                self.trail_low = live_c
            else:
                self.trail_low = min(self.trail_low, live_l)

        # System B — v2 zone trailing (Body Closing only). Same rollback
        # equivalence: one arm/raise step per candle; the post-arm low is the
        # running close during the arming candle.
        if not is_retest and nb is not None:
            zp = self.p.visual.zone_width_pct
            sb_base = nb
            sb_bottom = sb_base * (1 - zp / 100)
            sb_mid = (sb_bottom + sb_base) / 2.0
            sb_open = self._sb_seed_candle != em5_time
            if sb_open and self.sb_trail_sl is None and self.post_high >= sb_bottom:
                self.sb_trail_sl = sb_bottom
                self.sb_stage = 1
                self._sb_seed_candle = em5_time
            elif (
                sb_open
                and self.sb_trail_sl is not None
                and self.sb_trail_sl < sb_mid - 0.001
                and self.post_high >= sb_mid
            ):
                self.sb_trail_sl = sb_mid
                self.sb_stage = 2
                self._sb_seed_candle = em5_time
            # Non-repainting labels: drawn once per stage on confirmed close.
            if new5m and self.sb_stage > self.sb_stage_labelled:
                if self.sb_stage == 1:
                    self._event(ts_iso, "SB_SET", sb_bottom, f"◈ ZONE SET {sb_bottom:.2f}")
                elif self.sb_stage == 2:
                    self._event(ts_iso, "SB_RAISE", sb_mid, f"◈ ZONE ↑ {sb_mid:.2f}")
                self.sb_stage_labelled = self.sb_stage

        if self.sb_trail_sl is not None:
            if self._sb_seed_candle == em5_time:
                self.sb_trail_low = live_c
            elif self.sb_trail_low is None:
                self.sb_trail_low = live_c
            else:
                self.sb_trail_low = min(self.sb_trail_low, live_l)

        # ── EXIT PRIORITY (first match wins; latch locks the candle) ──
        exit_locked = self._exit_fired and self._exit_time == em5_time
        base_for_sl = self.entry_base if (is_retest and self.entry_base is not None) else b

        if not exit_locked and msl is not None and live_l <= msl:
            self._exit(ts_iso, em5_time, msl, "MAX_SL", f"✗ MAX SL {msl:.2f}")
        elif (
            not exit_locked
            and self.trail_sl is not None
            and self.trail_low is not None
            and self.trail_low <= self.trail_sl
        ):
            self._exit(ts_iso, em5_time, self.trail_sl, "TRAIL_EXIT", f"✦ TRAIL EXIT {self.trail_sl:.2f}")
        elif (
            not exit_locked
            and not is_retest
            and self.sb_trail_sl is not None
            and self.sb_trail_low is not None
            and self.sb_trail_low <= self.sb_trail_sl
        ):
            self._exit(ts_iso, em5_time, self.sb_trail_sl, "SB_EXIT", f"◈ ZONE EXIT {self.sb_trail_sl:.2f}")
        elif not exit_locked and not is_retest and nb is not None and live_h >= nb:
            self._exit(ts_iso, em5_time, nb, "TARGET", f"◆ TARGET HIT {nb:.2f}")
        elif (
            not exit_locked
            and new5m
            and live_c < base_for_sl
            and (self.trail_sl is None or self.trail_sl <= base_for_sl)
        ):
            self._exit(
                ts_iso, em5_time, live_c, "BASE_SL",
                f"✗ SL HIT — close < Base {base_for_sl:.2f}",
            )

    # -------------------------------------------------------------- actions

    def _enter(
        self,
        ts_iso: str,
        price: float,
        z: _Zone,
        sub: str,
        *,
        retest: bool = False,
        lock_time: Optional[datetime] = None,
    ) -> None:
        # Per-kind switch (§2 2026-09-09). Refusing HERE — after the elif
        # ladder already chose ``sub`` from the candle geometry — means a
        # disabled S1A does NOT fall through to S1B on this tick: the tick
        # simply does not enter and the trigger stays armed (identical to the
        # entry guard declining). The Retest loop keeps scanning the next
        # level, where an enabled R2 may still fire.
        if not self.p.entry.scenario_enabled(sub):
            return
        if self.entry_guard is not None and not self.entry_guard():
            return
        self.in_trade = True
        self.trig = False
        self.entry_price = price
        self.base_level = z.b
        self.zone = z
        self.max_sl = price * (1 - self.p.entry.max_sl_pct / 100)
        self.trail_sl = None
        self.trail_low = None
        self.sub = sub
        self.post_high = None
        self._entry_candle = self._em5_time
        self._entry_minute = self._last_min_ts
        self._trail_seed_candle = None
        self._sb_seed_candle = None
        if retest:
            self.entry_base = z.b            # frozen original Base (v24 fix)
            self._retest_fired = True
            self._retest_lock_time = lock_time
        else:
            self.entry_base = None
        self.trades.append(
            UmpTrade(entry_ts=ts_iso, entry_price=price, sub_scenario=sub, base_level=z.b)
        )
        self._event(ts_iso, sub, price, f"▲ {sub} {price:.2f}")

    def _exit(
        self, ts_iso: str, em5_time: datetime, price: float, kind: str, text: str
    ) -> None:
        self.in_trade = False
        self._exit_fired = True
        self._exit_time = em5_time
        self.trail_sl = None
        self.trail_low = None
        self.sb_trail_sl = None
        self.sb_trail_low = None
        self.sb_stage = 0
        self.sb_stage_labelled = 0
        self.post_high = None
        self.max_sl = None
        self._retest_fired = False
        self.trig = False
        self._trig_scen = 0
        self._trig_zone = None
        self._entry_candle = None
        self._trail_seed_candle = None
        self._sb_seed_candle = None
        if self.trades and self.trades[-1].open:
            t = self.trades[-1]
            t.exit_ts = ts_iso
            t.exit_price = price
            t.exit_reason = kind
        self._event(ts_iso, kind, price, text)

    def _event(self, ts_iso: str, kind: str, price: float, text: str) -> None:
        self.events.append(UmpEvent(ts=ts_iso, kind=kind, price=price, text=text))

    # ------------------------------------------------------------- history

    def _record_levels(self, ts_iso: str) -> None:
        sig = [(lv.price, lv.type, lv.name) for lv in self.levels]
        if self.levels_history:
            last = self.levels_history[-1][1]
            if [(lv.price, lv.type, lv.name) for lv in last] == sig:
                return
        self.levels_history.append((ts_iso, list(self.levels)))

    def _record_state(self, ts_iso: str) -> None:
        row = (
            self.in_trade,
            self.sub if self.in_trade else "",
            self.entry_price if self.in_trade else None,
            self.base_level if self.in_trade else None,
            self.max_sl,
            self.trail_sl,
            self.sb_trail_sl,
            self.sb_stage,
            self.trig,
            self.levels_ready,
        )
        if row == self._last_state_row:
            return
        self._last_state_row = row
        self.state_history.append({
            "ts": ts_iso,
            "in_trade": row[0],
            "sub": row[1],
            "entry_price": row[2],
            "base_level": row[3],
            "max_sl": row[4],
            "trail_sl": row[5],
            "sb_trail_sl": row[6],
            "sb_stage": row[7],
            "trig": row[8],
            "levels_ready": row[9],
        })
