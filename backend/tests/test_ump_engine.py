"""Ultra Master Pro — state-machine scenario tests (Pine v24 trade logic).

Run:  cd backend && PYTHONPATH=. python tests/test_ump_engine.py

Levels are INJECTED via a provider (the entry engine only ever reads the
unified level list — Pine's Option A), so every scenario controls its own
geometry exactly. Default zone width 3% on a Base of 100:
ZT = 103, UM = 101.5, LM = 98.5, ZB = 97. With a second level at 130:
NB = 130 → Q2 = 115, Q1 = 107.5, Q3 = 122.5; System B: Bottom = 126.1,
Mid = 128.05. Max SL 10% → entry 100 stops at 90.

Covered: SC1/SC2/SC3 trigger classification and the strict open-below-Base
origin rule; all 8 Body Closing sub-scenarios' fill levels; the two-candle
guard; trigger timeout; Retest R1/R2 (origin guard, MEDIAN exclusion, frozen
entry base); ERROR-1 (pre-entry spike), ERROR-3 (entry-wick trail exit),
exit priorities P1→P2→P2b→P3→P4 with first-match semantics; System B arm /
raise / exit and its Body-Closing-only scope; Retest NB re-anchoring
(never a target exit); the level freeze during trades; the levelsReady gate;
and §5.3 direction-flip discard.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.algo.config_models import UmpParams
from app.algo.engines.ump.engine import UmpEngine
from app.algo.engines.ump.levels import LEVEL_TYPE_NAMES, Level

T0 = datetime(2026, 8, 14, 9, 15)


def approx(a: float, b: float, tol: float = 1e-9) -> bool:
    """Zone sub-levels derive from percentage math (e.g. 100 × 1.015), which
    is not exactly representable in binary floats — compare with tolerance."""
    return abs(a - b) < tol


def mk(levels: list[tuple[float, int]], **entry_over) -> UmpEngine:
    lvls = [Level(price=p, type=t, name=LEVEL_TYPE_NAMES[t]) for p, t in levels]
    params = UmpParams()
    for k, v in entry_over.items():
        setattr(params.entry, k, v)
    return UmpEngine(params, level_provider=lambda: list(lvls))


def feed(eng: UmpEngine, bars: list[tuple[int, float, float, float, float]]) -> None:
    """bars: (minute_offset_from_09:15, o, h, l, c)."""
    for off, o, h, l, c in bars:
        eng.process_minute(T0 + timedelta(minutes=off), o, h, l, c)


def sc1_trigger(close: float = 104.0) -> list[tuple[int, float, float, float, float]]:
    """A green 09:15–09:19 candle: open 99 (< Base 100), close > ZT."""
    return [
        (0, 99.0, 99.2, 98.9, 99.1),
        (1, 99.1, 99.3, 99.0, 99.2),
        (2, 99.2, 99.4, 99.1, 99.3),
        (3, 99.3, 99.5, 99.2, 99.4),
        (4, 99.4, max(close, 99.4) + 0.2, 99.3, close),
    ]


def trigger_of(eng: UmpEngine) -> int:
    return eng._trig_scen  # noqa: SLF001 — scenario introspection in tests


# ── trigger classification ────────────────────────────────────────────────

def test_sc1_trigger_classification():
    eng = mk([(100.0, 1), (130.0, 1)])
    feed(eng, sc1_trigger(104.0))
    assert eng.trig and trigger_of(eng) == 1
    assert eng.state == "WATCHING"


def test_sc2_trigger_classification():
    eng = mk([(100.0, 1), (130.0, 1)])
    feed(eng, sc1_trigger(101.0))    # close between B and UM
    assert eng.trig and trigger_of(eng) == 2


def test_sc3_trigger_classification():
    eng = mk([(100.0, 1), (130.0, 1)])
    feed(eng, sc1_trigger(102.0))    # close in [UM, ZT]
    assert eng.trig and trigger_of(eng) == 3


def test_trigger_requires_open_strictly_below_base():
    eng = mk([(100.0, 1), (130.0, 1)])
    feed(eng, [
        (0, 100.0, 100.4, 99.9, 100.2),
        (1, 100.2, 100.9, 100.1, 100.7),
        (2, 100.7, 101.6, 100.6, 101.4),
        (3, 101.4, 102.6, 101.3, 102.4),
        (4, 102.4, 104.2, 102.3, 104.0),   # green, close > ZT, but open == B
    ])
    assert not eng.trig, "origin rule: candle open must be strictly below Base"


def test_trigger_requires_green_confirmed_close():
    eng = mk([(100.0, 1), (130.0, 1)])
    feed(eng, [
        (0, 105.0, 105.2, 104.8, 105.0),
        (1, 105.0, 105.1, 104.5, 104.6),
        (2, 104.6, 104.7, 104.0, 104.1),
        (3, 104.1, 104.2, 103.8, 103.9),
        (4, 103.9, 104.0, 103.5, 103.6),   # red candle (o 105 → c 103.6)
    ])
    assert not eng.trig


# ── body-closing entries ──────────────────────────────────────────────────

def test_s1a_enters_at_base():
    eng = mk([(100.0, 1), (130.0, 1)])
    feed(eng, sc1_trigger())
    feed(eng, [(5, 103.0, 103.5, 99.5, 101.0)])   # wick through Base
    assert eng.in_trade and eng.sub == "S1A"
    assert eng.entry_price == 100.0
    assert eng.max_sl == 100.0 * 0.9


def test_s1b_enters_at_upper_median():
    eng = mk([(100.0, 1), (130.0, 1)])
    feed(eng, sc1_trigger())
    feed(eng, [(5, 103.0, 103.2, 100.7, 102.0)])  # low in (B, UM]
    assert eng.in_trade and eng.sub == "S1B" and approx(eng.entry_price, 101.5)


def test_s1c_enters_at_zone_top():
    eng = mk([(100.0, 1), (130.0, 1)])
    feed(eng, sc1_trigger())
    feed(eng, [(5, 104.0, 104.1, 102.0, 103.5)])  # low in (UM, ZT]
    assert eng.in_trade and eng.sub == "S1C" and eng.entry_price == 103.0


def test_s2a_s2b_from_sc2():
    eng = mk([(100.0, 1), (130.0, 1)])
    feed(eng, sc1_trigger(101.0))
    feed(eng, [(5, 101.0, 101.2, 99.8, 100.5)])
    assert eng.sub == "S2A" and eng.entry_price == 100.0

    eng2 = mk([(100.0, 1), (130.0, 1)])
    feed(eng2, sc1_trigger(101.0))
    feed(eng2, [(5, 101.4, 101.5, 100.8, 101.2)])
    assert eng2.sub == "S2B" and approx(eng2.entry_price, 101.5)


def test_s3a_s3b_s3c_from_sc3():
    eng = mk([(100.0, 1), (130.0, 1)])
    feed(eng, sc1_trigger(102.0))
    feed(eng, [(5, 102.0, 102.2, 99.9, 101.0)])
    assert eng.sub == "S3A" and eng.entry_price == 100.0

    eng2 = mk([(100.0, 1), (130.0, 1)])
    feed(eng2, sc1_trigger(102.0))
    feed(eng2, [(5, 102.0, 102.1, 101.0, 101.8)])
    assert eng2.sub == "S3B" and approx(eng2.entry_price, 101.5)

    eng3 = mk([(100.0, 1), (130.0, 1)])
    feed(eng3, sc1_trigger(102.0))
    feed(eng3, [(5, 102.5, 104.0, 102.4, 103.8)])  # breakout: open < ZT < close
    assert eng3.sub == "S3C" and eng3.entry_price == 103.0


def test_two_candle_guard_blocks_same_candle_entry():
    eng = mk([(100.0, 1), (130.0, 1)])
    # Trigger candle whose own final minute would satisfy S1A if the guard
    # were missing (low touches Base).
    feed(eng, [
        (0, 99.0, 99.2, 98.9, 99.1),
        (1, 99.1, 99.3, 99.0, 99.2),
        (2, 99.2, 99.4, 99.1, 99.3),
        (3, 99.3, 99.5, 99.2, 99.4),
        (4, 99.4, 104.2, 99.3, 104.0),   # candle low 98.9 ≤ B, high ≥ B
    ])
    assert eng.trig and not eng.in_trade, "entry must wait for a LATER 5m candle"


def test_trigger_timeout_expires():
    eng = mk([(100.0, 1), (130.0, 1)], trigger_timeout_bars=4)
    feed(eng, sc1_trigger())
    # Four full no-entry candles (prices parked above the zone, red closes).
    bars = []
    for k in range(4):
        base_min = 5 + k * 5
        for m in range(5):
            bars.append((base_min + m, 105.0, 105.1, 104.6, 104.8))
    feed(eng, bars)
    assert eng.trig, "diff == timeout is NOT yet expired (strict >)"
    feed(eng, [(25, 105.0, 105.1, 104.6, 104.8)])
    assert not eng.trig, "the 5th candle after the trigger clears it"
    # Probe bar starts a FRESH 5m window with its open below Base, so the
    # candle can neither S1A (trigger expired) nor double as an R1 retest.
    feed(eng, [(30, 99.6, 103.5, 99.5, 101.0)])
    assert not eng.in_trade, "no entry once the trigger expired"


# ── retest entries ────────────────────────────────────────────────────────

def test_r1_enters_at_upper_median_with_frozen_base():
    eng = mk([(100.0, 1), (130.0, 1)])
    feed(eng, [(0, 101.2, 101.3, 99.8, 100.6)])   # top→bottom→top on Base
    assert eng.in_trade and eng.sub == "R1"
    assert approx(eng.entry_price, 101.5)
    assert eng.entry_base == 100.0


def test_r2_enters_at_zone_top():
    eng = mk([(100.0, 1), (130.0, 1)])
    feed(eng, [(0, 102.0, 102.2, 101.2, 101.9)])  # dip to UM, never Base
    assert eng.in_trade and eng.sub == "R2" and eng.entry_price == 103.0


def test_retest_needs_open_above_level():
    eng = mk([(100.0, 1), (130.0, 1)])
    feed(eng, [(0, 99.8, 100.8, 99.6, 100.6)])    # open BELOW Base — no origin
    assert not eng.in_trade


def test_same_candle_exit_is_final_no_reassertion_reopen():
    """Pine L970 (ERROR-2/4 interplay): a retest entry re-asserts itself
    through its candle — UNLESS any exit fired on that same candle, in which
    case the exit wins and the trade stays closed. Pin the exit-wins rule."""
    eng = mk([(100.0, 1), (130.0, 1)])
    feed(eng, [(0, 101.2, 101.3, 99.8, 100.6)])   # R1 entry @ UM 101.5
    assert eng.in_trade and eng.sub == "R1"
    # Same 5m candle: crash through Max SL (101.5 × 0.90 = 91.35).
    feed(eng, [(1, 100.0, 100.1, 90.0, 92.0)])
    assert not eng.in_trade and eng.trades[-1].exit_reason == "MAX_SL"
    # Still the same candle: recovery tick. Entry conditions are false
    # (running close below Base) and the re-assertion path must NOT
    # resurrect the closed trade.
    feed(eng, [(2, 92.0, 100.9, 92.0, 99.5)])
    assert not eng.in_trade, "exit wins the candle — no re-assertion reopen"
    assert len(eng.trades) == 1 and eng.trades[0].exit_reason == "MAX_SL"


def test_retest_never_fires_on_median_levels():
    eng = mk([(100.0, 4), (130.0, 1)])            # Base level is MEDIAN
    feed(eng, [(0, 101.2, 101.3, 99.8, 100.6)])
    assert not eng.in_trade


def test_retest_disabled_by_switch():
    eng = mk([(100.0, 1), (130.0, 1)], enable_retest=False)
    feed(eng, [(0, 101.2, 101.3, 99.8, 100.6)])
    assert not eng.in_trade


# ── ERROR-1 / ERROR-3 disciplines ─────────────────────────────────────────

def test_error1_pre_entry_spike_never_arms_trail():
    eng = mk([(100.0, 1), (130.0, 1)])
    feed(eng, sc1_trigger())
    # Entry minute spiked to 112 (over Q1 = 107.5) BEFORE the entry; close 101.
    feed(eng, [(5, 103.0, 112.0, 99.5, 101.0)])
    assert eng.in_trade and eng.sub == "S1A"
    assert eng.post_high == 101.0, "post-entry high seeds from the running CLOSE"
    assert eng.trail_sl is None, "the pre-entry spike must not set the trail"
    # Later tick of the SAME candle: rollback semantics keep post_high at the
    # running close — another spike still cannot arm the ladder.
    feed(eng, [(6, 101.0, 112.5, 100.8, 101.2)])
    assert eng.post_high == 101.2 and eng.trail_sl is None
    # A genuine push in the NEXT candle arms it (highs now count).
    feed(eng, [(10, 101.2, 108.0, 101.0, 107.0)])
    assert eng.trail_sl == 100.0


def test_error3_entry_wick_cannot_trigger_trail_exit():
    eng = mk([(100.0, 1), (130.0, 1)])
    feed(eng, sc1_trigger())
    feed(eng, [(5, 103.0, 103.5, 99.5, 101.0)])   # S1A; wick to 99.5
    # Next candle arms the trail AND dips below it in the same candle: the
    # arming candle's low (99.0 < 100) must NOT exit — trail_low is the
    # running close for the whole set candle (rollback re-seeds it per tick).
    feed(eng, [(10, 101.0, 108.0, 99.0, 107.5)])
    assert eng.in_trade, "the arming wick must not satisfy the trail exit"
    assert eng.trail_sl == 100.0 and eng.trail_low == 107.5
    # A touch in the NEXT candle does exit.
    feed(eng, [(15, 107.0, 107.2, 99.8, 100.2)])
    assert not eng.in_trade
    assert eng.trades[-1].exit_reason == "TRAIL_EXIT"
    assert eng.trades[-1].exit_price == 100.0


# ── exit priorities ───────────────────────────────────────────────────────

def _enter_s1a(eng: UmpEngine) -> None:
    feed(eng, sc1_trigger())
    feed(eng, [(5, 103.0, 103.5, 99.5, 101.0)])
    assert eng.in_trade and eng.sub == "S1A"


def test_p1_max_sl_intrabar():
    eng = mk([(100.0, 1), (130.0, 1)])
    _enter_s1a(eng)
    feed(eng, [(6, 101.0, 101.2, 89.0, 91.0)])
    t = eng.trades[-1]
    assert t.exit_reason == "MAX_SL" and t.exit_price == 90.0


def test_p1_beats_trail_exit_in_the_same_tick():
    eng = mk([(100.0, 1), (130.0, 1)])
    _enter_s1a(eng)
    feed(eng, [(10, 101.0, 108.0, 100.9, 107.0)])  # next candle: trail armed at 100
    # A candle that satisfies BOTH the Max SL (low ≤ 90) and the trail
    # touch — the FIRST branch in the chain (P1) must win.
    feed(eng, [(15, 107.0, 107.1, 88.0, 92.0)])
    assert eng.trades[-1].exit_reason == "MAX_SL"


def test_trail_ladder_ratchets_q1_q2_q3():
    # Pushes live in separate candles — one rung each.
    eng = mk([(100.0, 1), (130.0, 1)])
    _enter_s1a(eng)
    feed(eng, [(10, 101.0, 108.0, 100.9, 107.9)])
    assert eng.trail_sl == 100.0                  # postHigh ≥ Q1 → trail = B
    feed(eng, [(15, 108.0, 116.0, 107.9, 115.5)])
    assert eng.trail_sl == 107.5                  # ≥ Q2 → trail = Q1
    feed(eng, [(20, 115.5, 123.0, 115.4, 122.8)])
    assert eng.trail_sl == 115.0                  # ≥ Q3 → trail = Q2


def test_trail_ladder_multi_level_jump_in_one_candle():
    # Pine vl72 MULTI-LEVEL JUMP FIX: a single candle whose high clears Q3
    # outright walks Base→Q1→Q2 in one pass and lands on Q2 (the highest
    # rung cleared), with ONE label; the next flat candle changes nothing.
    eng = mk([(100.0, 1), (130.0, 1)])
    _enter_s1a(eng)
    n_before = len(eng.events)
    feed(eng, [(10, 101.0, 124.0, 100.9, 123.5)])
    assert eng.trail_sl == 115.0, "walks every rung crossed this candle"
    trail_events = [e for e in eng.events[n_before:] if e.kind in ("TRAIL_SET", "TRAIL_RAISE")]
    # Pine labels the FINAL rung: landing above Base is "TRAIL ↑" even from
    # an unset trail (vl72 L1144-1156) — one event, kind TRAIL_RAISE.
    assert len(trail_events) == 1 and trail_events[0].kind == "TRAIL_RAISE"
    feed(eng, [(15, 123.5, 123.6, 123.0, 123.2)])
    assert eng.trail_sl == 115.0, "no advance without a higher post_high"


def test_trail_multi_jump_is_error3_safe():
    # The jump candle's own low sits below the new trail (115) but the
    # trail_low is re-seeded to the running close on the set candle, so the
    # wick that produced the jump can never satisfy the trail exit.
    eng = mk([(100.0, 1), (130.0, 1)])
    _enter_s1a(eng)
    feed(eng, [(10, 101.0, 124.0, 100.9, 123.5)])
    assert eng.in_trade and eng.trail_sl == 115.0 and eng.trail_low == 123.5


def test_p3_target_hit_for_body_closing():
    eng = mk([(100.0, 1), (130.0, 1)])
    _enter_s1a(eng)
    feed(eng, [(10, 101.0, 131.0, 100.9, 130.5)])
    t = eng.trades[-1]
    assert t.exit_reason == "TARGET" and t.exit_price == 130.0


def test_retest_reanchors_at_nb_instead_of_target():
    eng = mk([(100.0, 1), (130.0, 1)])
    feed(eng, [(0, 101.2, 101.3, 99.8, 100.6)])   # R1 @ UM, base 100
    feed(eng, [(1, 101.5, 131.0, 101.4, 130.6)])  # crosses NB with close > NB
    assert eng.in_trade, "Retest never exits at NB"
    assert eng.trail_sl == 122.5, "trail advanced to Q3 at the NB cross"
    assert eng.base_level == 130.0, "ladder re-anchored to NB"
    assert eng.entry_base == 100.0, "frozen entry base never moves (v24 fix)"


def test_p4_base_sl_confirmed_close_only():
    eng = mk([(100.0, 1), (130.0, 1)])
    _enter_s1a(eng)
    # An intrabar dip below Base does NOT exit (no confirmed close)…
    feed(eng, [(6, 100.5, 100.6, 98.5, 100.2)])
    assert eng.in_trade
    # …nor does a mid-candle tick of the next window…
    # …the next candle CLOSES below Base (its last minute) → BASE_SL.
    feed(eng, [
        (10, 100.1, 100.2, 99.6, 99.8),
        (11, 99.8, 99.9, 99.3, 99.5),
        (12, 99.5, 99.6, 99.0, 99.2),
        (13, 99.2, 99.3, 98.8, 99.0),
        (14, 99.0, 99.1, 98.6, 98.8),
    ])
    t = eng.trades[-1]
    assert t.exit_reason == "BASE_SL" and t.exit_price == 98.8


def test_p4_uses_frozen_entry_base_for_retest():
    # Retest into 100, ladder re-anchored to 130 — a close below the DRIFTED
    # base must NOT exit; only a close below the ORIGINAL 100 may.
    eng = mk([(100.0, 1), (130.0, 1)])
    feed(eng, [(0, 101.2, 101.3, 99.8, 100.6)])
    feed(eng, [(1, 101.5, 131.0, 101.4, 130.6)])
    assert eng.base_level == 130.0
    # Candle closing at 125 (< drifted base 130 but > entry_base 100 and
    # > trail 122.5 → no trail touch: keep lows above it).
    feed(eng, [
        (5, 130.0, 130.1, 127.0, 128.0),
        (6, 128.0, 128.1, 126.0, 126.5),
        (7, 126.5, 126.6, 125.0, 125.5),
        (8, 125.5, 125.6, 124.8, 125.0),
        (9, 125.0, 125.1, 124.6, 125.0),
    ])
    assert eng.in_trade, "close below the drifted base must not fire P4"


def test_system_b_arms_raises_and_exits():
    # Each System B step in its own candle (one arm/raise per candle), with
    # closes above the fresh trail so the arming candle cannot self-exit.
    eng = mk([(100.0, 1), (130.0, 1)])
    _enter_s1a(eng)
    # Arm: post-entry high over SB_Bottom (126.1) without reaching NB (130).
    feed(eng, [(10, 101.0, 126.5, 100.9, 126.3)])
    assert eng.sb_stage == 1 and approx(eng.sb_trail_sl, 130.0 * 0.97)
    # Raise over SB_Mid (128.05), still below NB.
    feed(eng, [(15, 126.3, 128.5, 126.2, 128.2)])
    assert eng.sb_stage == 2 and approx(eng.sb_trail_sl, 128.05)
    # Hold above the Mid trail one candle, then touch it → P2b exit at the SB
    # level (System A's trail sits at Q2 = 115, so P2 stays silent).
    feed(eng, [(20, 128.2, 128.4, 128.1, 128.3)])
    assert eng.in_trade
    feed(eng, [(25, 128.3, 128.35, 127.8, 127.9)])
    t = eng.trades[-1]
    assert t.exit_reason == "SB_EXIT"
    assert approx(t.exit_price, 128.05)


def test_system_b_never_runs_for_retest():
    eng = mk([(100.0, 1), (130.0, 1)])
    feed(eng, [(0, 101.2, 101.3, 99.8, 100.6)])   # R1
    feed(eng, [(1, 101.5, 127.0, 101.4, 126.5)])  # over SB_Bottom
    assert eng.sb_stage == 0 and eng.sb_trail_sl is None


# ── freeze gate / levelsReady / direction discard ─────────────────────────

def test_levels_freeze_during_trade():
    lvls = [
        Level(100.0, 1, "1H-STRUCT"),
        Level(130.0, 1, "1H-STRUCT"),
    ]
    eng = UmpEngine(UmpParams(), level_provider=lambda: list(lvls))
    feed(eng, sc1_trigger())
    feed(eng, [(5, 103.0, 103.5, 99.5, 101.0)])
    assert eng.in_trade
    # Mutate the provider's world mid-trade — the frozen set must keep NB=130.
    lvls.clear()
    lvls.append(Level(50.0, 1, "1H-STRUCT"))
    feed(eng, [(10, 101.0, 131.0, 100.9, 130.5)])
    assert eng.trades[-1].exit_reason == "TARGET" and eng.trades[-1].exit_price == 130.0
    # After the exit the next confirmed close rebuilds from the new world.
    feed(eng, [
        (10, 60.0, 60.1, 59.6, 59.8),
        (11, 59.8, 59.9, 59.3, 59.5),
        (12, 59.5, 59.6, 59.0, 59.2),
        (13, 59.2, 59.3, 58.8, 59.0),
        (14, 59.0, 59.1, 58.6, 58.8),
    ])
    assert [l.price for l in eng.levels] == [50.0]


def test_levels_ready_gate_blocks_entries():
    eng = UmpEngine(UmpParams(), level_provider=lambda: [])
    feed(eng, [(0, 101.2, 101.3, 99.8, 100.6)])
    assert not eng.levels_ready and not eng.in_trade


def test_direction_flip_discards_watching_state():
    eng = mk([(100.0, 1), (130.0, 1)])
    feed(eng, sc1_trigger())
    assert eng.state == "WATCHING"
    eng.reset_direction_state()
    assert eng.state == "IDLE" and not eng.trig
    # Open kept BELOW Base so the bar cannot double as an R1 retest.
    feed(eng, [(5, 99.6, 103.5, 99.5, 101.0)])
    assert not eng.in_trade, "discarded trigger must not produce an entry"


def test_no_nb_means_no_trail_no_target():
    eng = mk([(100.0, 1)])                        # single level, no NB
    _enter_s1a(eng)
    feed(eng, [(10, 101.0, 140.0, 100.9, 139.0)])
    assert eng.in_trade and eng.trail_sl is None, "no ladder without an NB"


# ── 2026-08-18 Pine parity fixes (equal-close, missing minute, exit-candle
#    lockout, intraday 1H accumulation, nearest_below) ─────────────────────

R1_BAR = (5, 101.2, 101.3, 99.8, 100.6)   # open>B, wick to/below B, close>B


def test_equal_close_suppresses_new_5m_close():
    """Pine L131-134: a 5m candle closing at EXACTLY the previous confirmed
    close is not a new close — Step 1 must skip that candle."""
    red_104 = [
        (0, 105.0, 105.2, 103.8, 104.0),
        (1, 104.0, 104.3, 103.9, 104.0),
        (2, 104.0, 104.2, 103.9, 104.0),
        (3, 104.0, 104.1, 103.9, 104.0),
        (4, 104.0, 104.1, 103.9, 104.0),   # candle 1 confirmed close = 104.0
    ]
    green_close = lambda c: [  # noqa: E731
        (5, 99.0, 99.2, 98.9, 99.1),
        (6, 99.1, 99.5, 99.0, 99.4),
        (7, 99.4, 101.0, 99.3, 100.8),
        (8, 100.8, 103.0, 100.7, 102.8),
        (9, 102.8, max(c, 102.8) + 0.2, 102.7, c),
    ]
    eng = mk([(100.0, 1), (130.0, 1)])
    feed(eng, red_104 + green_close(104.0))   # same close as candle 1
    assert not eng.trig, "equal confirmed close ⇒ new5mClose false ⇒ no Step-1"

    eng2 = mk([(100.0, 1), (130.0, 1)])
    feed(eng2, red_104 + green_close(104.1))  # differs by a tick
    assert eng2.trig, "a changed close is a real confirmed close"


def test_missing_final_minute_still_confirms_the_close():
    """A feed gap on the candle's :x4 minute must not erase its confirmed
    close — it fires on the next window's first bar (Pine's data feed always
    delivers the close)."""
    eng = mk([(100.0, 1), (130.0, 1)])
    feed(eng, [
        (0, 99.0, 99.2, 98.9, 99.1),
        (1, 99.1, 100.5, 99.0, 100.3),
        (2, 100.3, 102.5, 100.2, 102.3),
        (3, 102.3, 104.2, 102.2, 104.0),   # running close 104 — but :x4 missing
        (5, 104.0, 104.1, 103.9, 104.0),   # next candle's first bar
    ])
    assert eng.trig and trigger_of(eng) == 1, (
        "the 09:15 candle's confirmed close (SC1) must fire via the deferred "
        "close on the 09:20 candle's first bar"
    )


def test_no_reentry_inside_the_exit_candle():
    """Pine L1051-1062: once an exit fires, any same-candle re-entry is
    killed before it can be managed — the port blocks the entry outright."""
    eng = mk([(100.0, 1), (130.0, 1)])
    feed(eng, [R1_BAR])                        # R1 entry @ UM 101.5
    assert eng.in_trade and eng.sub == "R1"
    feed(eng, [(6, 100.0, 100.1, 90.0, 92.0)])  # P1 MAX SL (91.35) breach
    assert not eng.in_trade
    assert eng.trades[-1].exit_reason == "MAX_SL"
    feed(eng, [(7, 101.2, 101.3, 99.8, 100.6)])  # would be a fresh R1...
    assert not eng.in_trade, "no new entry inside the exit's 5m candle"
    assert len(eng.trades) == 1
    feed(eng, [(10, 101.2, 101.3, 99.8, 100.6)])  # next candle — allowed
    assert eng.in_trade and len(eng.trades) == 2


def test_intraday_session_hour_joins_the_h1_feed():
    """Pine's h1_struct grows ALL session: each completed session-anchored
    hour (09:15–10:15, …) must append to the engine's 1H feed."""
    eng = mk([(100.0, 1), (130.0, 1)])
    bars = [(i, 100.0 + i * 0.01, 100.2 + i * 0.01, 99.9 + i * 0.01, 100.1 + i * 0.01)
            for i in range(0, 60)]             # 09:15 .. 10:14 — one full hour
    feed(eng, bars)
    assert len(eng.feeds.h1_candles) == 0, "hour not complete yet"
    feed(eng, [(60, 101.0, 101.2, 100.9, 101.1)])   # 10:15 → new bucket
    assert len(eng.feeds.h1_candles) == 1, "completed 09:15–10:15 hour appended"
    h1 = eng.feeds.h1_candles[-1]
    assert approx(h1.o, 100.0) and approx(h1.c, 100.1 + 59 * 0.01, 1e-6)


def test_nearest_below():
    eng = mk([(100.0, 1), (130.0, 1)])
    feed(eng, [(0, 99.0, 99.2, 98.9, 99.1)])   # first bar builds the level set
    assert eng.nearest_below(120.0) == 100.0
    assert eng.nearest_below(99.0) is None
    assert eng.nearest_above(120.0) == 130.0


# ── multi-day continuity (TV chart parity: expiry-to-expiry replay) ──────

D_MON = datetime(2026, 8, 10, 9, 15)   # Mon .. Fri = ISO week 33
D_TUE = datetime(2026, 8, 11, 9, 15)
D_WED = datetime(2026, 8, 12, 9, 15)
D_THU = datetime(2026, 8, 13, 9, 15)
D_FRI = datetime(2026, 8, 14, 9, 15)
D_MON2 = datetime(2026, 8, 17, 9, 15)  # ISO week 34


def feed_day(eng: UmpEngine, day_open: datetime,
             bars: list[tuple[int, float, float, float, float]]) -> None:
    """bars: (minute_offset_from_09:15_of_that_day, o, h, l, c)."""
    for off, o, h, l, c in bars:
        eng.process_minute(day_open + timedelta(minutes=off), o, h, l, c)


def _tiny_session(px: float) -> list[tuple[int, float, float, float, float]]:
    """Two 5m candles; day close == px, day high px+1, day low px-1."""
    return [(0, px, px + 1.0, px - 1.0, px + 0.2), (5, px + 0.2, px + 0.5, px - 0.5, px)]


def test_day_rollover_folds_daily_newest_first_cap3():
    eng = mk([(100.0, 1), (130.0, 1)])
    for day_open, px in [(D_MON, 100.0), (D_TUE, 101.0), (D_WED, 102.0), (D_THU, 103.0)]:
        feed_day(eng, day_open, _tiny_session(px))
    assert [d[2] for d in eng.feeds.daily] == [102.0, 101.0, 100.0], "after Thu's first bar: Wed, Tue, Mon"
    feed_day(eng, D_FRI, [(0, 104.0, 104.2, 103.8, 104.1)])
    assert [d[2] for d in eng.feeds.daily] == [103.0, 102.0, 101.0], "cap 3 — Monday dropped"
    assert eng.feeds.daily[0] == (104.0, 102.0, 103.0), "Thursday's (H, L, C)"


def test_week_rollover_sets_weekly_at_iso_boundary():
    eng = mk([(100.0, 1), (130.0, 1)])
    for day_open, px in [(D_WED, 102.0), (D_THU, 103.0), (D_FRI, 104.0)]:
        feed_day(eng, day_open, _tiny_session(px))
    assert eng.feeds.weekly is None, "running week not complete"
    feed_day(eng, D_MON2, [(0, 105.0, 105.2, 104.8, 105.1)])
    # Completed week 33 = Wed..Fri fold: H = 104+1, L = 102-1, C = Fri close.
    assert eng.feeds.weekly == (105.0, 101.0, 104.0)
    assert [d[2] for d in eng.feeds.daily] == [104.0, 103.0, 102.0]


def test_gate_opens_mid_replay_from_empty_feeds():
    eng = UmpEngine(UmpParams())          # REAL level build, empty feeds

    def oc_session(o: float, c: float) -> list[tuple[int, float, float, float, float]]:
        mid = (o + c) / 2
        hi, lo = max(o, c), min(o, c)
        return [(0, o, hi + 0.3, lo - 0.3, mid), (5, mid, hi + 0.1, lo - 0.1, c)]

    # Alternating green/red day-candles produce 1H structural reversals, so
    # the REAL level pipeline has anchors once the feeds exist.
    sessions = [(D_MON, 100.0, 101.0), (D_TUE, 100.9, 99.9), (D_WED, 100.0, 102.0),
                (D_THU, 101.9, 100.5), (D_FRI, 101.0, 103.0)]
    for day_open, o, c in sessions:
        feed_day(eng, day_open, oc_session(o, c))
        assert not eng.levels_ready, "weekly feed still missing — gate closed"
    feed_day(eng, D_MON2, [(0, 103.0, 103.2, 102.8, 103.1)])
    assert eng.feeds.loaded and eng.levels_ready, (
        "3 dailies + completed ISO week + 1H present — the gate opens mid-replay"
    )


def test_deferred_close_fires_under_prior_day_feeds():
    snaps: list[int] = []
    box: dict = {}
    lvl = Level(price=100.0, type=1, name=LEVEL_TYPE_NAMES[1])

    def provider():
        snaps.append(len(box["e"].feeds.daily))
        return [lvl]

    eng = UmpEngine(UmpParams(), level_provider=provider)
    box["e"] = eng
    # Day 1: one full candle, then a candle whose FINAL minute never arrives.
    feed_day(eng, D_MON, [(0, 99.0, 99.4, 98.8, 99.2), (1, 99.2, 99.5, 99.0, 99.3),
                          (2, 99.3, 99.6, 99.1, 99.4), (3, 99.4, 99.7, 99.2, 99.5),
                          (4, 99.5, 99.8, 99.3, 99.6),
                          (370, 99.6, 99.9, 99.4, 99.7), (371, 99.7, 100.0, 99.5, 99.8)])
    assert all(s == 0 for s in snaps)
    # Day 2's first bar: the deferred close of the 15:25 candle must rebuild
    # levels BEFORE Monday folds into feeds.daily.
    feed_day(eng, D_TUE, [(0, 99.8, 100.1, 99.6, 99.9)])
    assert snaps[-1] == 0, "deferred close evaluated under the PRIOR day's feeds"
    feed_day(eng, D_TUE, [(1, 99.9, 100.2, 99.7, 100.0), (2, 100.0, 100.3, 99.8, 100.1),
                          (3, 100.1, 100.4, 99.9, 100.2), (4, 100.2, 100.5, 100.0, 100.3)])
    assert snaps[-1] == 1, "the next confirmed close sees the folded Monday"


def test_trigger_expires_over_the_overnight_gap():
    # Retest disabled: after the timeout cancels the trigger, the SAME tick's
    # later retest block could legitimately enter (Pine has the identical
    # ordering) — this test isolates the timeout semantics alone.
    eng = mk([(100.0, 1), (130.0, 1)], enable_retest=False)
    # Trigger candle 15:20–15:24 (offsets 365–369; :24 = confirmed close).
    feed_day(eng, D_MON, [(365, 99.0, 99.4, 98.8, 99.2), (366, 99.2, 99.6, 99.0, 99.4),
                          (367, 99.4, 99.8, 99.2, 99.6), (368, 99.6, 100.5, 99.4, 100.2),
                          (369, 100.2, 104.4, 100.0, 104.0)])
    assert eng.trig, "SC1 trigger armed on Monday's last confirmed close"
    # Tuesday touches the Base — but the wall-clock timeout (Pine L820-821:
    # em5_time diff in ms, despite the '5m Bars' label) expired overnight.
    feed_day(eng, D_TUE, [(0, 103.0, 103.5, 99.5, 101.0)])
    assert not eng.in_trade and not eng.trig, "trigger dies at the overnight gap — TV-exact"


def test_trade_carries_overnight_with_trail_state():
    eng = mk([(100.0, 1), (130.0, 1)])
    feed_day(eng, D_MON, sc1_trigger())
    feed_day(eng, D_MON, [(5, 103.0, 103.5, 99.5, 101.0)])   # S1A @ 100
    assert eng.in_trade and eng.sub == "S1A"
    feed_day(eng, D_TUE, [(0, 101.0, 101.5, 100.5, 101.2)])
    assert eng.in_trade, "no End-Exit inside the engine — the position rides the gap"
    feed_day(eng, D_TUE, [(1, 89.5, 89.8, 88.0, 89.0)])      # Max SL 90 intrabar
    assert not eng.in_trade
    assert eng.trades[-1].exit_reason == "MAX_SL"
    assert eng.trades[-1].entry_ts.startswith("2026-08-10")
    assert eng.trades[-1].exit_ts is not None and eng.trades[-1].exit_ts.startswith("2026-08-11")


def test_equal_close_suppressed_across_the_gap():
    eng = mk([(100.0, 1), (130.0, 1)])
    # Monday's last candle: RED, confirmed close exactly 104.0 (no trigger).
    feed_day(eng, D_MON, [(0, 105.0, 105.2, 104.8, 105.0), (1, 105.0, 105.1, 104.4, 104.5),
                          (2, 104.5, 104.6, 104.2, 104.3), (3, 104.3, 104.4, 104.0, 104.1),
                          (4, 104.1, 104.2, 103.8, 104.0)])
    assert not eng.trig
    # Tuesday's first candle is a textbook SC1 green trigger — but its close
    # equals Monday's last confirmed close, so new5mClose is False (L131-134).
    feed_day(eng, D_TUE, [(0, 99.0, 99.2, 98.9, 99.1), (1, 99.1, 99.3, 99.0, 99.2),
                          (2, 99.2, 99.4, 99.1, 99.3), (3, 99.3, 99.5, 99.2, 99.4),
                          (4, 99.4, 104.2, 99.3, 104.0)])
    assert not eng.trig, "equal-close suppression spans the overnight gap"
    feed_day(eng, D_TUE, [(5, 99.4, 99.6, 99.2, 99.5), (6, 99.5, 99.7, 99.3, 99.6),
                          (7, 99.6, 99.8, 99.4, 99.7), (8, 99.7, 99.9, 99.5, 99.8),
                          (9, 99.8, 104.3, 99.6, 104.1)])
    assert eng.trig, "a genuinely new close re-arms — the geometry was always valid"


def test_seeded_feeds_keep_growing():
    from app.algo.engines.ump.engine import UmpFeeds
    from app.algo.engines.ump.levels import Candle
    eng = mk([(100.0, 1), (130.0, 1)])
    eng.set_feeds(UmpFeeds(
        h1_candles=[Candle(o=1.0, h=2.0, l=0.5, c=1.5)],
        daily=[(110.0, 90.0, 100.0), (111.0, 91.0, 101.0), (112.0, 92.0, 102.0)],
        weekly=(120.0, 80.0, 105.0),
    ))
    feed_day(eng, D_FRI, _tiny_session(104.0))
    feed_day(eng, D_MON2, [(0, 105.0, 105.3, 104.7, 105.1)])
    assert eng.feeds.daily == [(105.0, 103.0, 104.0), (110.0, 90.0, 100.0), (111.0, 91.0, 101.0)], (
        "seeded dailies rotate: Friday in front, oldest seed dropped"
    )
    assert eng.feeds.weekly == (105.0, 103.0, 104.0), "ISO boundary replaced the seeded week"
    assert len(eng.feeds.h1_candles) >= 2, "Friday's (partial) hour appended to the seed"


def test_sparse_day_still_folds_and_buckets():
    eng = mk([(100.0, 1), (130.0, 1)])
    feed_day(eng, D_MON, [(89, 50.0, 51.0, 49.5, 50.5),
                          (168, 50.5, 52.0, 50.0, 51.0),
                          (374, 51.0, 51.5, 48.0, 49.0)])
    feed_day(eng, D_TUE, [(0, 49.0, 49.5, 48.5, 49.2)])
    assert eng.feeds.daily[0] == (52.0, 48.0, 49.0), "a 3-minute day still folds a daily"
    assert eng._bar_index == 3, "each sparse bar opened its own 5m window"  # noqa: SLF001


def test_partial_final_hour_joins_h1_on_day_change():
    eng = mk([(100.0, 1), (130.0, 1)])
    bars = [(360 + i, 100.0 + i * 0.1, 100.2 + i * 0.1, 99.9 + i * 0.1, 100.1 + i * 0.1)
            for i in range(0, 15)]           # 15:15 .. 15:29 — the partial hour
    feed_day(eng, D_MON, bars)
    assert len(eng.feeds.h1_candles) == 0, "partial hour not flushed intraday"
    feed_day(eng, D_TUE, [(0, 101.5, 101.7, 101.4, 101.6)])
    assert len(eng.feeds.h1_candles) == 1, "the 15:15–15:30 short bar IS a completed 60m bar next morning"
    h1 = eng.feeds.h1_candles[-1]
    assert approx(h1.o, 100.0) and approx(h1.c, 100.1 + 14 * 0.1, 1e-6)


def test_rebuild_equals_continuous_feed():
    """The backtest's day-open-cache + deepcopy + today-tail path must be
    byte-equivalent to one continuous multi-day feed (the design-(b) caveat:
    rebuild ≡ continuous is what makes fresh-per-day + adoption faithful)."""
    import copy as _copy

    day1 = sc1_trigger() + [(5, 103.0, 103.5, 99.5, 101.0),   # S1A entry @ 100
                            (10, 101.0, 108.0, 100.8, 107.6),  # Q1 trail rung
                            (14, 107.6, 107.9, 107.0, 107.4)]
    day2 = [(0, 107.4, 107.8, 106.9, 107.2), (1, 107.2, 116.0, 107.0, 115.4),
            (4, 115.4, 115.8, 114.9, 115.2), (5, 115.2, 115.5, 114.5, 114.8)]

    cont = mk([(100.0, 1), (130.0, 1)])
    feed_day(cont, D_MON, day1)
    feed_day(cont, D_TUE, day2)

    base = mk([(100.0, 1), (130.0, 1)])
    feed_day(base, D_MON, day1)
    reb = _copy.deepcopy(base)
    feed_day(reb, D_TUE, day2)

    for attr in ("in_trade", "entry_price", "base_level", "trail_sl",
                 "sb_trail_sl", "sb_stage", "trig", "_bar_index",
                 "_prev_5m_close", "post_high", "trail_low"):
        assert getattr(cont, attr) == getattr(reb, attr), (
            f"{attr}: continuous={getattr(cont, attr)!r} rebuilt={getattr(reb, attr)!r}"
        )
    assert cont.feeds.daily == reb.feeds.daily
    assert cont.feeds.weekly == reb.feeds.weekly
    assert len(cont.events) == len(reb.events)
    assert len(cont.trades) == len(reb.trades)


def _run_all() -> None:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL  {name}: {e}")
    if failures:
        raise SystemExit(f"{failures} test(s) failed")
    print("all UMP engine tests passed")


if __name__ == "__main__":
    _run_all()


# ── trail inside the entry candle (user rule 2026-09-25) ────────────────────
# Base 100, NB 130 → Q1 107.5, Q2 115, Q3 122.5. The 09:20 candle enters S1A
# at the Base; a LATER minute of the same candle wicks above Q3 and closes
# between Q2 and Q3 (the SENSEX 73800 CE case from 24 Sep, in round numbers).

def test_entry_candle_wick_after_entry_moves_trail_to_q2_only_when_switched_on():
    for flag, want in ((False, 107.5), (True, 115.0)):
        eng = mk([(100.0, 1), (130.0, 1)], trail_counts_entry_candle_high=flag)
        feed(eng, sc1_trigger())
        feed(eng, [(5, 103.0, 103.5, 99.5, 101.0)])          # S1A at the Base
        assert eng.in_trade and eng.trades[-1].sub_scenario == "S1A"
        feed(eng, [(6, 101.0, 124.0, 117.0, 118.0)])         # later minute: wick > Q3
        assert approx(eng.trail_sl, want), (flag, eng.trail_sl)


def test_the_entry_minutes_own_high_never_counts():
    """Part of the entry minute's range may precede the entry touch — even
    with the switch ON its high must not advance the ladder."""
    eng = mk([(100.0, 1), (130.0, 1)], trail_counts_entry_candle_high=True)
    feed(eng, sc1_trigger())
    feed(eng, [(5, 103.0, 125.0, 99.5, 101.0)])              # entry minute spikes to 125
    assert eng.in_trade and eng.trail_sl is None


def test_after_the_entry_candle_the_ladder_is_unchanged_by_the_switch():
    """From the next candle on, running highs already count — both settings
    agree, so the switch only changes the entry candle."""
    out = []
    for flag in (False, True):
        eng = mk([(100.0, 1), (130.0, 1)], trail_counts_entry_candle_high=flag)
        feed(eng, sc1_trigger())
        feed(eng, [(5, 103.0, 103.5, 99.5, 101.0)])
        feed(eng, [(6, 101.0, 102.0, 100.5, 101.5), (7, 101.5, 102.0, 101.0, 101.8),
                   (8, 101.8, 102.0, 101.2, 101.6), (9, 101.6, 101.9, 101.3, 101.7)])
        feed(eng, [(10, 101.7, 124.0, 101.5, 118.0)])        # next candle wicks > Q3
        out.append(eng.trail_sl)
    assert approx(out[0], 115.0) and approx(out[1], 115.0), out
