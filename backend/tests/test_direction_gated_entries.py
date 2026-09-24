"""Direction-gated Ultra Master Pro entries — the required sequence:

    Multi-TF + OI Change + Ratio -> CALL/PUT -> activate UMP -> R1/R2/S* -> entry
    -> no duplicate

The engine factory here does what live (``warmup_ump_engine_full_life``) and the
backtest (``BacktestDeps.build_engine``) both do: replay every CLOSED minute up
to the pass with entries DISARMED, then re-arm. Minutes come from a tape keyed
by time, like the premium store. Levels are injected (base 100, next 130; the
seeded Monday Z1 zone width is 2 % -> UM 101, ZT 102), as in the orchestrator
suite.

Each scenario first proves the pre-direction setup is REAL (an always-armed
engine enters on it), then shows the gated pipeline ignores it and takes the
genuinely new setup that forms after the direction.

Run:  cd backend && PYTHONPATH=. python tests/test_direction_gated_entries.py
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from app.algo.engines.ump.engine import UmpEngine
from app.algo.engines.ump.levels import LEVEL_TYPE_NAMES, Level
from app.algo.orchestrator import MinuteBar, ZoneOrchestrator, disarm_entries, rearm_entries
from tests.test_algo_orchestrator import Fake, run

D = datetime(2026, 8, 17)                  # Monday; seeded Z1 = 09:20-10:30, band 75-125


def t(hm: str) -> datetime:
    h, m = map(int, hm.split(":"))
    return D.replace(hour=h, minute=m)


def bar(hm: str, o: float, h: float, l: float, c: float) -> MinuteBar:
    return MinuteBar(ts=t(hm), o=o, h=h, l=l, c=c)


class TapeWorld(Fake):
    """Fake providers + a time-indexed minute tape + a real disarmed warm-up."""

    def __init__(self, tape: list[MinuteBar]) -> None:
        super().__init__()
        self.tape = {b.ts: b for b in tape}
        self.now: datetime = t("09:20")
        self.engines_built = 0

    def levels(self) -> list[Level]:
        return [Level(price=p, type=k, name=LEVEL_TYPE_NAMES[k]) for p, k in self.engine_levels]

    def deps(self):
        base = super().deps()

        async def build_engine(contract, zone_cfg, entries_live):
            self.engines_built += 1
            params = zone_cfg.ump.model_copy(deep=True)
            disarm_entries(params)
            lv = self.levels()
            eng = UmpEngine(params, level_provider=lambda: list(lv))
            for ts in sorted(self.tape):
                if ts + timedelta(minutes=1) <= self.now:     # closed minutes only
                    b = self.tape[ts]
                    eng.process_minute(b.ts, b.o, b.h, b.l, b.c)
            rearm_entries(params, zone_cfg.ump)
            return eng

        async def latest_minute(contract, now):
            return self.tape.get(now - timedelta(minutes=1))

        return replace(base, build_engine=build_engine, latest_minute=latest_minute)

    def step(self, orch: ZoneOrchestrator, hm: str, reading: str) -> None:
        self.now = t(hm)
        self.readings = {"oi_change": reading, "multi_tf": reading, "ratio": reading}
        run(orch, self.now)


def always_armed_entries(world: TapeWorld, upto: str) -> list[tuple[str, str]]:
    """What an engine that never waited for a direction would have entered."""
    eng = UmpEngine(world.config.days["monday"].zones["Z1"].ump,
                    level_provider=lambda: world.levels())
    out = []
    for ts in sorted(world.tape):
        if ts > t(upto):
            break
        b = world.tape[ts]
        n = len(eng.trades)
        eng.process_minute(b.ts, b.o, b.h, b.l, b.c)
        if len(eng.trades) > n:
            tr = eng.trades[-1]
            out.append((tr.entry_ts[11:16], tr.sub_scenario))
            break
    return out


def run_day(world: TapeWorld, direction_from: str, until: str) -> tuple[ZoneOrchestrator, dict[str, str]]:
    """One pass per minute; NO_TRADE before ``direction_from``, CALL after.
    Returns the pass time at which each ledger row was inserted."""
    orch = ZoneOrchestrator(world.deps())
    entered_at: dict[str, str] = {}
    cur = t("09:21")
    while cur <= t(until):
        hm = cur.strftime("%H:%M")
        n = len(world.inserted)
        world.step(orch, hm, "CALL" if cur >= t(direction_from) else "NO_TRADE")
        if len(world.inserted) > n:
            row = world.inserted[-1]
            entered_at[f"#{len(world.inserted)}"] = f"{hm} {row.get('sub_scenario')}"
        cur += timedelta(minutes=1)
    return orch, entered_at


# ── the pre-direction candles used by the retest scenarios ─────────────────
FLAT = [bar(f"09:{m:02d}", 105.0, 105.2, 104.8, 105.0) for m in range(15, 25)]


def test_r1_formed_before_direction_is_ignored_new_r1_after_is_taken() -> None:
    tape = FLAT + [
        # 09:25-09:29: open above Base, wick through it, close back above = R1
        bar("09:25", 102.0, 102.2, 101.8, 102.0),
        bar("09:26", 102.0, 102.1, 101.5, 101.9),
        bar("09:27", 101.9, 102.0, 100.9, 100.95),
        bar("09:28", 100.95, 101.0, 99.8, 100.4),
        bar("09:29", 100.4, 100.9, 100.3, 100.8),
        # direction appears at the 09:30 pass; new candle 09:30-09:34
        bar("09:30", 100.9, 101.0, 100.7, 100.9),     # no retest yet (low above Base)
        bar("09:31", 100.9, 100.9, 99.9, 100.5),      # NEW R1: wick to 99.9, close 100.5
        bar("09:32", 100.5, 100.8, 100.4, 100.6),
    ]
    world = TapeWorld(tape)
    assert always_armed_entries(world, "09:29") == [("09:28", "R1")], \
        "scenario check: the pre-direction candle really is an R1"

    orch, entered = run_day(world, direction_from="09:30", until="09:32")
    assert entered == {"#1": "09:32 R1"}, entered
    assert orch.position is not None and orch.position.engine.trades[-1].entry_ts.startswith("2026-08-17T09:31")


def test_r2_formed_before_direction_is_ignored_new_r2_after_is_taken() -> None:
    tape = FLAT + [
        # 09:25-09:29: open above UM (101), wick into UM but not Base, close above UM = R2
        bar("09:25", 102.5, 102.6, 102.3, 102.4),
        bar("09:26", 102.4, 102.5, 100.6, 101.9),
        bar("09:27", 101.9, 102.0, 101.7, 101.8),
        bar("09:28", 101.8, 102.0, 101.7, 101.9),
        bar("09:29", 101.9, 102.1, 101.8, 102.0),
        bar("09:30", 102.2, 102.4, 102.0, 102.3),     # direction pass; no retest
        bar("09:31", 102.3, 102.3, 100.7, 101.8),     # NEW R2
        bar("09:32", 101.8, 102.0, 101.7, 101.9),
    ]
    world = TapeWorld(tape)
    assert always_armed_entries(world, "09:29") == [("09:26", "R2")]

    orch, entered = run_day(world, direction_from="09:30", until="09:32")
    assert entered == {"#1": "09:32 R2"}, entered


def test_body_closing_trigger_before_direction_is_not_carried_forward() -> None:
    tape = [bar(f"09:{m:02d}", 99.0, 99.2, 98.8, 99.0) for m in range(15, 20)] + [
        # 09:20-09:24 green trigger: open below Base, close between Base and UM (SC2)
        bar("09:20", 99.0, 99.6, 98.9, 99.5),
        bar("09:21", 99.5, 100.2, 99.4, 100.1),
        bar("09:22", 100.1, 100.5, 100.0, 100.4),
        bar("09:23", 100.4, 100.8, 100.3, 100.5),
        bar("09:24", 100.5, 100.8, 100.4, 100.6),
        # 09:25-09:29 would be its test candle (touches Base)
        bar("09:25", 100.6, 101.1, 99.9, 100.2),
        bar("09:26", 100.2, 100.4, 100.1, 100.3),
        bar("09:27", 100.3, 100.4, 100.2, 100.3),
        bar("09:28", 100.3, 100.4, 100.2, 100.3),
        bar("09:29", 100.3, 100.3, 99.4, 99.5),
        # direction at 09:30; a NEW trigger candle 09:30-09:34 (open 99.4 < Base, close 100.6)
        bar("09:30", 99.4, 99.9, 99.3, 99.8),
        bar("09:31", 99.8, 100.4, 99.7, 100.3),
        bar("09:32", 100.3, 100.7, 100.2, 100.6),
        bar("09:33", 100.6, 100.8, 100.5, 100.5),
        bar("09:34", 100.5, 100.8, 100.4, 100.6),
        # 09:35 test candle touches Base -> S2A @ 100
        bar("09:35", 100.6, 101.0, 99.9, 100.4),
        bar("09:36", 100.4, 100.6, 100.3, 100.5),
    ]
    world = TapeWorld(tape)
    assert always_armed_entries(world, "09:29") == [("09:25", "S2A")], \
        "scenario check: the pre-direction trigger + test candle really is S2A"

    orch, entered = run_day(world, direction_from="09:30", until="09:36")
    assert entered == {"#1": "09:36 S2A"}, entered
    assert orch.position is not None
    assert orch.position.engine.trades[-1].entry_ts.startswith("2026-08-17T09:35")


def test_no_direction_means_no_engine_and_no_entry_all_day() -> None:
    tape = FLAT + [
        bar("09:25", 102.0, 102.2, 101.8, 102.0),
        bar("09:28", 101.2, 101.3, 99.8, 100.4),
        bar("09:31", 100.9, 100.9, 99.9, 100.5),
    ]
    world = TapeWorld(tape)
    orch, entered = run_day(world, direction_from="23:59", until="09:40")
    assert entered == {}
    assert world.engines_built == 0, "no Call/Put -> the UMP engine is never even built"
    assert orch.hunt is None


def test_no_reentry_inside_the_exit_candle_even_from_a_rebuilt_hunt() -> None:
    """Pine: after an exit, no entry of any kind until that candle ends. The
    hunt engine built after the exit is a NEW engine, so the orchestrator
    hands it the lock. Levels 100 / 103 / 130 -> the S2A trade targets 103."""
    tape = FLAT + [bar(f"09:{m:02d}", 99.0, 99.2, 98.8, 99.0) for m in range(25, 30)] + [
        # 09:30-09:34 trigger candle (open 99.4 < Base, close 100.6: SC2)
        bar("09:30", 99.4, 99.9, 99.3, 99.8),
        bar("09:31", 99.8, 100.4, 99.7, 100.3),
        bar("09:32", 100.3, 100.7, 100.2, 100.6),
        bar("09:33", 100.6, 100.8, 100.5, 100.5),
        bar("09:34", 100.5, 100.8, 100.4, 100.6),
        # 09:35-09:39: S2A @100, TARGET @103, then R1 geometry in the SAME candle
        bar("09:35", 100.6, 100.8, 99.9, 100.4),     # S2A (pass 09:36)
        bar("09:36", 100.4, 103.2, 100.3, 101.5),    # TARGET 103 (pass 09:37) -> exit
        bar("09:37", 101.5, 101.6, 100.7, 100.8),    # open 100.6 > B, low 99.9, close 100.8: R1 shape
        bar("09:38", 100.8, 100.9, 100.7, 100.8),
        bar("09:39", 100.8, 100.9, 100.6, 100.7),
        # 09:40: a NEW candle with a NEW R1 -> allowed
        bar("09:40", 100.9, 101.0, 99.8, 100.6),
        bar("09:41", 100.6, 100.8, 100.5, 100.7),
    ]
    world = TapeWorld(tape)
    world.engine_levels = [(100.0, 1), (103.0, 1), (130.0, 1)]
    orch, entered = run_day(world, direction_from="09:30", until="09:41")
    assert list(entered.values()) == ["09:36 S2A", "09:41 R1"], entered
    assert [c["exit_reason"] for c in world.closed_trades] == ["TARGET"], world.closed_trades
    assert orch.position is not None
    assert orch.position.engine.trades[-1].entry_ts.startswith("2026-08-17T09:40")


def test_one_setup_one_entry() -> None:
    """A fired entry consumes its setup: while the trade is open the engine is
    never asked for another entry, and the position engine itself is disarmed."""
    tape = FLAT + [bar(f"09:{m:02d}", 105.0, 105.2, 104.8, 105.0) for m in range(25, 30)] + [
        bar("09:30", 100.9, 101.0, 100.7, 100.9),
        bar("09:31", 100.9, 100.9, 99.9, 100.5),     # R1
        bar("09:32", 100.5, 100.7, 99.9, 100.6),     # same R1 shape keeps showing
        bar("09:33", 100.6, 100.7, 99.9, 100.6),
        bar("09:34", 100.6, 100.7, 100.4, 100.6),
    ]
    world = TapeWorld(tape)
    orch, entered = run_day(world, direction_from="09:30", until="09:34")
    assert list(entered.values()) == ["09:32 R1"], entered
    assert world.entry_calls == 1


def _run_all() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run_all()
    print("\nall direction-gated entry tests passed")


# ── fresh OI-integrated entry cycle (2026-09-25) ───────────────────────────
# The signal arrives MID-candle: the 09:30-09:34 candle already holds an R1
# wick through the Base in its pre-signal minutes (09:30-09:31).
MID = FLAT + [
    bar("09:25", 102.0, 102.2, 101.8, 102.0),
    bar("09:26", 102.0, 102.1, 101.6, 101.9),
    bar("09:27", 101.9, 102.0, 101.4, 101.5),
    bar("09:28", 101.5, 101.6, 101.2, 101.3),
    bar("09:29", 101.3, 101.4, 101.1, 101.2),
    bar("09:30", 101.0, 101.1, 100.9, 101.0),     # candle opens above Base (100)
    bar("09:31", 101.0, 101.0, 99.8, 100.4),      # PRE-signal wick through the Base
    bar("09:32", 100.6, 100.9, 100.5, 100.8),     # first armed minute: no wick
    bar("09:33", 100.8, 100.9, 99.9, 100.5),      # NEW post-signal R1 wick
    bar("09:34", 100.5, 100.7, 100.4, 100.6),
]


def test_pre_signal_minutes_of_the_straddling_candle_cannot_fire_an_entry() -> None:
    world = TapeWorld(MID)
    orch, entered = run_day(world, direction_from="09:32", until="09:34")
    assert entered == {"#1": "09:34 R1"}, entered
    assert orch.position.engine.trades[-1].entry_ts.startswith("2026-08-17T09:33"),         "only the post-signal wick (09:33) may trigger"


def test_without_the_fresh_cycle_the_pre_signal_wick_would_have_fired() -> None:
    """Proves the scenario exercises the leak: with the fresh view disabled the
    SAME tape enters one minute earlier, on the pre-signal wick."""
    world = TapeWorld(MID)
    orig = UmpEngine.start_entry_cycle
    UmpEngine.start_entry_cycle = lambda self: None
    try:
        _, entered = run_day(world, direction_from="09:32", until="09:34")
    finally:
        UmpEngine.start_entry_cycle = orig
    assert entered == {"#1": "09:33 R1"}, entered


def test_switch_off_calculates_no_entries_at_all() -> None:
    world = TapeWorld(MID)
    world.config.days["monday"].zones["Z1"].oi_fresh_entries = False
    orch, entered = run_day(world, direction_from="09:32", until="09:34")
    assert entered == {} and world.engines_built == 0
    assert any("fresh OI-integrated entry calculation OFF" in b for b in orch.status.gate_blocks)


def test_every_direction_change_starts_a_fresh_cycle() -> None:
    world = TapeWorld(FLAT + [bar(f"09:{m:02d}", 105.0, 105.2, 104.8, 105.0) for m in range(25, 40)])
    orch = ZoneOrchestrator(world.deps())
    cycles = []
    orig = UmpEngine.start_entry_cycle

    def spy(self):
        cycles.append(world.now.strftime("%H:%M"))
        orig(self)

    UmpEngine.start_entry_cycle = spy
    try:
        for hm, rd in (("09:30", "CALL"), ("09:31", "CALL"), ("09:32", "PUT"),
                       ("09:33", "PUT"), ("09:34", "CALL")):
            world.step(orch, hm, rd)
    finally:
        UmpEngine.start_entry_cycle = orig
    assert cycles == ["09:30", "09:32", "09:34"], cycles    # CALL, PUT, CALL — each fresh
