"""Ultra Master Pro — level pipeline tests (Pine v24 f_buildLevels laws).

Run:  cd backend && PYTHONPATH=. python tests/test_ump_levels.py

Every expectation below is hand-derived from the Pine source semantics:
1H two-candle reversal detection, the 11-value pivot matrix, newest-first
structural spacing, the weekly discovery staircase, downside bridges,
void-filling internal bridges, the global 20% Expansion pass and recursive
Median injection.
"""
from __future__ import annotations

from app.algo.config_models import UmpParams
from app.algo.engines.ump.levels import (
    DISCOVERY_IDX,
    _sort_asc,
    Candle,
    build_levels,
    detect_h1_reversals,
    pivot_matrix,
)


def _params(**inst) -> UmpParams:
    p = UmpParams()
    for k, v in inst.items():
        setattr(p.institutional, k, v)
    return p


# ── 1H structural reversal detection ──────────────────────────────────────

def test_h1_green_then_red_reversal_emits_first_close():
    candles = [
        Candle(100, 111, 99, 110),   # green
        Candle(110.5, 112, 104, 105),  # red, closes below 110, open matches 110 within 2.2
        Candle(105, 106, 103, 104),  # red — must NOT form a second (red→green) pair
    ]
    assert detect_h1_reversals(candles, body_match_pts=2.2, price_floor=50) == [110.0]


def test_h1_red_then_green_reversal():
    candles = [
        Candle(110, 111, 99, 100),     # red
        Candle(100.5, 108, 100, 107),  # green, closes above 100, open matches 100 within 2.2
        Candle(107, 108, 106, 107.5),
    ]
    assert detect_h1_reversals(candles, 2.2, 50) == [100.0]


def test_h1_body_match_tolerance_gates():
    candles = [
        Candle(100, 111, 99, 110),
        Candle(115, 116, 104, 105),   # open 115 vs close 110 → gap 5 > 2.2
        Candle(105, 106, 103, 104),   # red — no follow-on reversal pair
    ]
    assert detect_h1_reversals(candles, 2.2, 50) == []
    assert detect_h1_reversals(candles, 6.0, 50) == [110.0], "wider tolerance admits it"


def test_h1_price_floor_gates():
    candles = [
        Candle(30, 41, 29, 40),
        Candle(40.5, 42, 34, 35),
        Candle(35, 36, 33, 34),      # red — no follow-on reversal pair
    ]
    assert detect_h1_reversals(candles, 2.2, price_floor=50) == []
    assert detect_h1_reversals(candles, 2.2, price_floor=20) == [40.0]


def test_h1_exact_duplicates_deduplicated():
    pattern = [
        Candle(100, 111, 99, 110),
        Candle(110.5, 112, 104, 105),
    ]
    candles = pattern + [Candle(105, 106, 103, 104)] + pattern
    assert detect_h1_reversals(candles, 2.2, 50) == [110.0]


# ── pivot matrix ──────────────────────────────────────────────────────────

def test_pivot_matrix_known_values():
    # h=110, l=90, c=100 → P=100; R1..R5 = 110,120,130,130,140;
    # S1..S5 = 90,80,70,70,70. Extensions (2026-09-10 DISCOVERY fix):
    #   R4x = R3 + (R3 − R2) = 130 + (130 − 120) = 140
    #   R5x = R4x + (R4x − R3) = 140 + (140 − 130) = 150
    m = pivot_matrix(110, 90, 100)
    assert m == [100, 110, 120, 130, 130, 140, 90, 80, 70, 70, 70, 140, 150]


def test_r4x_is_algebraically_r5():
    """R4x = 2·R3 − R2 = h + 3(p − l) = R5, for every candle. It is kept only
    because the script computes it; it can never win a discovery rung."""
    for (h, l, c) in [(110, 90, 100), (52, 48, 50), (150, 140, 145),
                      (24310.5, 23980.25, 24122.8), (100, 10, 100)]:
        m = pivot_matrix(h, l, c)
        assert m[11] == m[5], (h, l, c, m[11], m[5])
        # R5x is one more (p − l) step above R5.
        assert abs(m[12] - (m[5] + ((h + l + c) / 3 - l))) < 1e-9


def test_discovery_index_order_matches_pine():
    # Pine `_dIdx = array.from(1, 2, 3, 4, 5, 11, 12)` — R1..R5 then the
    # extensions, in this order (the ratchet has climbed before R4x/R5x).
    assert DISCOVERY_IDX == (1, 2, 3, 4, 5, 11, 12)


# ── full pipeline (hand-derived end-to-end expectation) ───────────────────

def _full_pipeline_levels() -> list:
    # Structural stream: 100, 118, 130 (oldest→newest). Newest-first spacing:
    # 130 kept; 118 within 10.2% of 130 → dropped; 100 at 30% → kept.
    h1 = [100.0, 118.0, 130.0]
    # One daily candle (52,48,50) → P=50, R1..R5 = 52,54,56,56,58,
    # S1..S5 = 48,46,44,44,44, R4x = 56+(56−54) = 58, R5x = 58+(58−56) = 60.
    # Pool per candle = 13 values → 39 across the three identical candles:
    # [44×9, 46×3, 48×3, 50×3, 52×3, 54×3, 56×6, 58×6, 60×3].
    daily = [(52.0, 48.0, 50.0), (52.0, 48.0, 50.0), (52.0, 48.0, 50.0)]
    # Weekly (150,140,145) → R1..R5 = 150,155,160,160,165; R4x = 165 (= R5),
    # R5x = 170. Staircase above the structural max 130 needs > 130×1.2 = 156
    # → only 160 enters; 160×1.2 = 192 then blocks 160, 165, R4x 165 and
    # R5x 170, so the extension rungs change nothing here.
    weekly = (150.0, 140.0, 145.0)
    return build_levels(h1, daily, weekly, _params(), fallback_close=120.0)


def test_full_pipeline_hand_derived():
    lv = _full_pipeline_levels()
    got = [(l.price, l.name) for l in lv]
    # Downside bridges below floor 100, walking the pool descending with 20%
    # spacing and the ₹50 price floor. The R5x extension put 60 into the pool,
    # so 60 is now the first candidate: (100−60)/60 = 66.7% ≥ 20 → accepted
    # (before the fix the highest pool value was 58, which took this slot).
    # From 60 the rest are too close — 58 is 3.4%, 56 is 7.1%, 54 is 11.1%,
    # 52 is 15.4% — 50 is not > the ₹50 floor, and 48/46/44 are below it.
    # Ascending spacing then keeps everything, and the 50% Median law injects
    # (60+100)/2 = 80 into the 66.7% void (halves 33.3% and 25%, both ≥ 20%).
    # No other gap exceeds 50%.
    assert got == [
        (60.0, "BRIDGE"),
        (80.0, "MEDIAN"),
        (100.0, "1H-STRUCT"),
        (130.0, "1H-STRUCT"),
        (160.0, "DISCOVERY"),
    ], got


def test_bridge_pool_carries_thirteen_pivots_per_daily_candle():
    """Pine's pool loop spreads the WHOLE matrix, so it grew 33 → 39 with the
    extension rungs. The duplicate R4x values must not survive as levels."""
    daily = [(52.0, 48.0, 50.0), (52.0, 48.0, 50.0), (52.0, 48.0, 50.0)]
    pool = []
    for (dh, dl, dc) in daily:
        pool.extend(v for v in pivot_matrix(dh, dl, dc) if v > 0)
    assert len(pool) == 39
    assert pool.count(58.0) == 6, "R5 and its duplicate R4x, once per candle"
    assert 60.0 in pool, "R5x is a new bridge candidate"
    # The Expansion pass is the de-duplicator: a 0% gap fails ">= 20".
    lv = _full_pipeline_levels()
    prices = [l.price for l in lv]
    assert len(prices) == len(set(prices)), prices


def test_discovery_admits_the_r5x_extension_rung():
    """A wide weekly candle (p − l > h/2) is the case the fix exists for:
    R5x = h + 4(p − l) clears R5 × 1.2 and becomes a sixth rung.

    weekly (100, 10, 100) → p = 70, (p − l) = 60:
      R1 130, R2 160, R3 220, R4 205, R5 280, R4x 280 (= R5), R5x 340.
    Ratchet from the structural max 100: 130 ✓ (>120), 160 ✓ (>156),
    220 ✓ (>192), 205 ✗ (≤264), 280 ✓ (>264), R4x 280 ✗ (≤336 — inert, as
    always), R5x 340 ✓ (>336). Before the fix the ladder stopped at 280.
    """
    lv = build_levels([100.0], [], (100.0, 10.0, 100.0), _params(), 100.0)
    disc = [l.price for l in lv if l.name == "DISCOVERY"]
    assert disc == [130.0, 160.0, 220.0, 280.0, 340.0], disc


def test_structural_spacing_is_newest_first():
    # Newest (130) wins the 20% contest against 118 — not the other way round.
    lv = build_levels([118.0, 130.0], [], None, _params(), fallback_close=120.0)
    prices = [l.price for l in lv if l.name == "1H-STRUCT"]
    assert prices == [130.0], prices


def test_discovery_needs_expansion_above_structural_max():
    # Weekly R-values that never clear max×1.2 produce no discovery levels.
    # (140,135,138) → R1..R5 = 140.33, 142.67, 145.33, 145.17, 148;
    # R4x = 148 (= R5), R5x = 150.67 — all below 130×1.2 = 156, so the
    # extension rungs leave this case unchanged.
    lv = build_levels([130.0], [], (140.0, 135.0, 138.0), _params(), 120.0)
    assert all(l.name != "DISCOVERY" for l in lv)


def test_median_requires_both_halves_spaced():
    # Gap 100→190 = 90% > 50% law → med 145: (145-100)/100 = 45% ≥ 20 and
    # (190-145)/145 = 31% ≥ 20 → injected. Second sweep: 100→145 (45%) < 50%
    # and 145→190 (31%) < 50% → stable.
    lv = build_levels([100.0, 190.0], [], None, _params(), 120.0)
    got = [(l.price, l.name) for l in lv]
    assert got == [(100.0, "1H-STRUCT"), (145.0, "MEDIAN"), (190.0, "1H-STRUCT")], got


def test_median_respects_price_floor():
    lv = build_levels(
        [60.0, 120.0], [], None, _params(price_floor_inr=95.0), 100.0
    )
    # 60 itself falls below the 95 floor at the structural stage; a lone 120
    # has no gap → no median.
    assert [(l.price, l.name) for l in lv] == [(120.0, "1H-STRUCT")]


def test_internal_bridge_scavenges_pool_pivot():
    # Structural 100 and 200 (100% gap > 50% bridge law). Daily (132,128,130)
    # → pool sorted [124,124,124,126,128,130,132,134,136,136,138]. The
    # scavenger takes the FIRST ascending pivot > 100×1.2 = 120 and
    # < 200×0.98 → 124 (an S-side pivot). Next anchor 124 needs > 148.8 —
    # none exists → stop. Spacing keeps [100,124,200]; the 124→200 gap
    # (61% > 50% median law) injects (124+200)/2 = 162 with both halves ≥ 20%.
    h1 = [100.0, 200.0]
    daily = [(132.0, 128.0, 130.0)]
    lv = build_levels(h1, daily, None, _params(), 150.0)
    got = [(l.price, l.name) for l in lv]
    assert got == [
        (100.0, "1H-STRUCT"),
        (124.0, "BRIDGE"),
        (162.0, "MEDIAN"),
        (200.0, "1H-STRUCT"),
    ], got


def test_scan_depth_limits_structural_history():
    p = _params()
    p.structural.scan_depth_bars = 1
    lv = build_levels([100.0, 300.0], [], None, p, 120.0)
    prices = [l.price for l in lv if l.name == "1H-STRUCT"]
    assert prices == [300.0], "only the newest structural level is scanned"


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
    print("all UMP level tests passed")


if __name__ == "__main__":
    _run_all()


def test_sort_asc_matches_pine_tie_behaviour():
    """Pine's ``f_sortAsc`` swaps only on strictly-greater and never reads the
    type, so equal prices come out in an order Python's ``sorted`` does not
    reproduce (it tie-breaks on the type integer). The Expansion pass keeps
    the FIRST of an equal pair, so the tie decides which TYPE survives — and
    type drives Retest eligibility and the chart colour."""
    assert _sort_asc([2.0, 2.0, 1.0], [1, 2, 3]) == ([1.0, 2.0, 2.0], [3, 2, 1])
    assert sorted(zip([2.0, 2.0, 1.0], [1, 2, 3])) != [(1.0, 3), (2.0, 2), (2.0, 1)]
    # Distinct keys: identical to sorted(), which is every real level set.
    assert _sort_asc([5.0, 1.0, 3.0], [1, 2, 3]) == ([1.0, 3.0, 5.0], [2, 3, 1])


# ── level ladder → zone geometry → a real trade ───────────────────────────
# Every scenario in test_ump_engine.py INJECTS a level list, so nothing there
# covers the coupling this file owns: a ladder built by build_levels decides
# the zone sub-levels, the entry price, the Q-rungs and the target. That is
# exactly what the 2026-09-10 fix moves, so it gets an end-to-end test.

def test_real_ladder_drives_entry_zone_and_target():
    from datetime import datetime, timedelta

    from app.algo.engines.ump.engine import UmpEngine

    levels = _full_pipeline_levels()
    assert [l.price for l in levels] == [60.0, 80.0, 100.0, 130.0, 160.0]

    eng = UmpEngine(_params(), level_provider=lambda: list(levels))
    t0 = datetime(2026, 9, 10, 9, 15)

    def feed(bars):
        for off, o, h, l, c in bars:
            eng.process_minute(t0 + timedelta(minutes=off), o, h, l, c)

    # Trigger candle 09:15-09:19: opens 99 (below the 100 1H-STRUCT base) and
    # closes 104 — above ZT = 100 × 1.03 = 103 → SC1.
    feed([
        (0, 99.0, 99.2, 98.9, 99.1),
        (1, 99.1, 99.4, 99.0, 99.3),
        (2, 99.3, 100.6, 99.2, 100.4),
        (3, 100.4, 102.6, 100.3, 102.4),
        (4, 102.4, 104.2, 102.3, 104.0),
    ])
    assert eng.trig, "a real ladder must arm the SC1 trigger"

    # Test candle 09:20: wicks through the Base and recovers → S1A at 100.
    feed([(5, 103.0, 103.5, 99.5, 101.0)])
    assert eng.in_trade and eng.sub == "S1A"
    assert eng.entry_price == 100.0, "entry price IS the level"
    assert eng.base_level == 100.0
    assert eng.max_sl == 90.0, "10% hard cap under the entry"
    z = eng.zone
    assert z is not None
    # ±zonePct/100 and ±zonePct/200 around the base; compared with a
    # tolerance because 100 × (1 + 3.0/200) is 101.49999999999999 in binary
    # floating point — Pine computes the identical expression and lands on
    # the identical value, so the port must not "tidy" it.
    for got, want in zip((z.b, z.zt, z.um, z.lm, z.zb),
                         (100.0, 103.0, 101.5, 98.5, 97.0)):
        assert abs(got - want) < 1e-9, (got, want)
    # NB is the next ladder level above the base — 130, not the 80 MEDIAN
    # below it — so Q1/Q2/Q3 quarter the 100→130 room.
    assert eng.nearest_above(100.0) == 130.0
    assert eng.nearest_below(100.0) == 80.0

    # 09:25 runs to the next level: TARGET fires at NB.
    feed([(10, 101.0, 131.0, 100.9, 130.5)])
    assert not eng.in_trade
    t = eng.trades[-1]
    assert t.exit_reason == "TARGET" and t.exit_price == 130.0
    assert t.entry_price == 100.0 and t.base_level == 100.0
    # The P&L the order path would book is a pure function of the ladder.
    assert round(t.exit_price - t.entry_price, 2) == 30.0
