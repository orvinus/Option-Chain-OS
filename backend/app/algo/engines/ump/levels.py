"""Ultra Master Pro — the institutional level pipeline (Pine v24 lines
156–395, transcribed structure-for-structure).

Four level types governed by three laws plus injection:

- **1H-STRUCT** (type 1) — two-candle 1H reversals (green-then-red or
  red-then-green, bodies matching within ``body_match_pts``); level = the
  FIRST candle's close.
- **DISCOVERY** (type 2) — a staircase of weekly pivot-matrix resistances
  above the structural maximum, each ≥ Expansion% above the last. Since the
  2026-09-10 fix the staircase also walks the R4x/R5x extension rungs, so it
  can reach one step higher than R5 (see ``DISCOVERY_IDX``).
- **BRIDGE** (type 3) — daily pivot-matrix values (39-value pool from the
  previous three daily candles) filling downside room below the structural
  floor and internal voids wider than the Bridge law %.
- **MEDIAN** (type 4) — recursive midpoints injected into any remaining gap
  wider than the Median law %, only when both halves respect the Expansion
  spacing.

Everything returns exactly what would be PLOTTED — the entry engine reads the
same list, so an entry can only ever fire on a drawn level (Pine's "Option A"
unified ``outP``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ...config_models import UmpParams

LEVEL_TYPE_NAMES: dict[int, str] = {
    1: "1H-STRUCT",
    2: "DISCOVERY",
    3: "BRIDGE",
    4: "MEDIAN",
}


@dataclass(frozen=True)
class Candle:
    o: float
    h: float
    l: float
    c: float


@dataclass(frozen=True)
class Level:
    price: float
    type: int          # 1..4 per LEVEL_TYPE_NAMES
    name: str


def detect_h1_reversals(
    h1_candles: list[Candle], body_match_pts: float, price_floor: float
) -> list[float]:
    """Pine's 1H structural reversal stream, accumulated across the feed.

    At each 1H bar the script looks at the two most recently CLOSED candles
    (offsets [2] and [1]): a green-then-red or red-then-green pair whose
    open[1] matches close[2] within ``body_match_pts``, with close[2] above
    the price floor, emits close[2] as a structural level. Consecutive
    duplicate emissions collapse and exact values are de-duplicated across
    the whole session (the ``h1_struct`` array's duplicate scan)."""
    out: list[float] = []
    prev_emitted: Optional[float] = None
    for i in range(2, len(h1_candles) + 1):
        c2 = h1_candles[i - 2]
        c1 = h1_candles[i - 1]
        reversal = (
            (c2.c > c2.o and c1.c < c1.o and c1.c < c2.c)
            or (c2.c < c2.o and c1.c > c1.o and c1.c > c2.c)
        )
        val: Optional[float] = None
        if reversal and abs(c1.o - c2.c) <= body_match_pts and c2.c > price_floor:
            val = c2.c
        if val is not None and val != prev_emitted:
            if val not in out:
                out.append(val)
        prev_emitted = val
    return out


def pivot_matrix(h: float, l: float, c: float) -> list[float]:
    """Pine ``f_matrix`` — 13 pivots: P, R1..R5, S1..S5, R4x, R5x (in that
    order; the index positions are load-bearing, see ``DISCOVERY_IDX``).

    R4x/R5x are the extension rungs added by the 2026-09-10 DISCOVERY fix
    (Pine vl74). Two facts about them are worth stating once, here:

    - ``r4x = 2*r3 - r2 = h + 3*(p - l)`` is **algebraically identical to r5**.
      It can therefore never be admitted as a discovery rung — the staircase
      tests it immediately after r5, against a ratchet r5 has just raised —
      and inside the bridge pool it is an exact duplicate, which the
      Expansion pass discards on a zero gap. It is kept because the script
      computes it, and a faithful port must produce the same pool.
    - ``r5x = 2*r4x - r3 = h + 4*(p - l)`` is the genuinely new rung: one
      ``(p - l)`` step above R5.
    """
    p = (h + l + c) / 3
    r1 = 2 * p - l
    r2 = p + (h - l)
    r3 = h + 2 * (p - l)
    r4 = p + 1.5 * (h - l)
    r5 = h + 3 * (p - l)
    s1 = 2 * p - h
    s2 = p - (h - l)
    s3 = l - 2 * (h - p)
    s4 = 2 * p - r3
    s5 = 2 * p - r4
    r4x = r3 + (r3 - r2)
    r5x = r4x + (r4x - r3)
    return [p, r1, r2, r3, r4, r5, s1, s2, s3, s4, s5, r4x, r5x]


# Pine ``_dIdx`` — the weekly-matrix members the discovery staircase walks,
# in the script's literal order: R1, R2, R3, R4, R5, R4x, R5x. The extension
# rungs are visited AFTER R5, so the ratchet has already climbed when they
# are tested (which is why R4x, being equal to R5, is inert).
DISCOVERY_IDX: tuple[int, ...] = (1, 2, 3, 4, 5, 11, 12)


def _sort_asc(prices: list[float], types: list[int]) -> tuple[list[float], list[int]]:
    """Pine ``f_sortAsc`` — exchange sort over the two parallel arrays,
    swapping only on strictly-greater.

    Ported literally rather than using ``sorted(zip(prices, types))`` because
    the two are NOT equivalent once two levels share a price: Pine's pass is
    unstable and never looks at the type, while ``sorted`` breaks the tie on
    the type integer. Since the Expansion pass keeps the FIRST of an equal
    pair, that tie decides which TYPE survives — and type drives Retest
    eligibility (MEDIAN is excluded) and the chart colour.

    Equal prices are unreachable today: every insertion path enforces
    ≥ Expansion% separation, so the R4x/R5 duplicates in the bridge pool die
    long before the sorter. This keeps the port faithful anyway rather than
    resting on that proof — the 2026-09-10 DISCOVERY fix is itself evidence
    that the law set moves. n is well under a hundred, so the O(n²) is free.
    """
    p = list(prices)
    t = list(types)
    for i in range(len(p) - 1):
        for j in range(i + 1, len(p)):
            if p[i] > p[j]:
                p[i], p[j] = p[j], p[i]
                t[i], t[j] = t[j], t[i]
    return p, t


def build_levels(
    h1_struct: list[float],
    daily: list[tuple[float, float, float]],     # up to 3 × (H, L, C), most recent first
    weekly: Optional[tuple[float, float, float]],  # previous week (H, L, C)
    params: UmpParams,
    fallback_close: float,
) -> list[Level]:
    """Pine ``f_buildLevels`` — the complete pipeline, ascending result.

    ``fallback_close`` stands in for ``close`` when no structural level exists
    (the discovery staircase seeds from the structural max, else the chart's
    current close)."""
    expansion = params.institutional.expansion_law_pct
    bridge_law = params.institutional.bridge_law_pct
    median_law = params.institutional.median_law_pct
    floor = params.institutional.price_floor_inr
    scan_depth = params.structural.scan_depth_bars

    final_p: list[float] = []
    final_t: list[int] = []

    # 1. Structural levels — NEWEST first, each kept only when ≥ Expansion%
    #    away from every already-kept structural level.
    total = len(h1_struct)
    take = min(total, scan_depth)
    for i in range(total - 1, total - take - 1, -1):
        pv = h1_struct[i]
        if pv <= floor:
            continue
        conflict = False
        for existing in final_p:
            lv = min(pv, existing)
            if lv > 0 and abs(pv - existing) / lv * 100 < expansion:
                conflict = True
                break
        if not conflict:
            final_p.append(pv)
            final_t.append(1)

    # 2. Bridge pool — 39 pivots from the previous three daily candles (3 × 13
    #    since the R4x/R5x extension rungs joined the matrix; Pine's pool loop
    #    spreads the WHOLE matrix, so the three R5x values become real bridge
    #    candidates and the three R4x duplicates fall out at the Expansion pass).
    b_pool: list[float] = []
    for (dh, dl, dc) in daily[:3]:
        if dh > dl and dh > 0:
            b_pool.extend(v for v in pivot_matrix(dh, dl, dc) if v > 0)
    b_pool.sort()

    # 3. Weekly discovery staircase above the structural max — the matrix
    #    members in ``DISCOVERY_IDX`` (R1..R5 then the R4x/R5x extensions),
    #    each step ≥ Expansion% above the previous rung.
    if weekly is not None:
        wh, wl, wc = weekly
        if wh > wl and wh > 0:
            w_matrix = pivot_matrix(wh, wl, wc)
            s_max = max(final_p) if final_p else fallback_close
            u_base = s_max
            for i in DISCOVERY_IDX:
                cv = w_matrix[i]
                if cv > u_base * (1 + expansion / 100):
                    final_p.append(cv)
                    final_t.append(2)
                    u_base = cv

    final_p, final_t = _sort_asc(final_p, final_t)

    r_p: list[float] = []
    r_t: list[int] = []

    # 4. Downside bridges below the structural/discovery floor — pool values
    #    DESCENDING, each accepted when the room above it is ≥ Expansion%.
    if final_p:
        d_floor = min(final_p)
        for pvt in reversed(b_pool):
            if pvt < d_floor and pvt > floor:
                if d_floor > 0 and (d_floor - pvt) / pvt * 100 >= expansion:
                    r_p.append(pvt)
                    r_t.append(3)
                    d_floor = pvt

    # 5. Internal ladder + scavenger bridges: any adjacent gap wider than the
    #    Bridge law % scavenges pool pivots (each ≥ Expansion% above the lower
    #    anchor and below 98% of the upper), max 10 per gap.
    for i in range(len(final_p)):
        r_p.append(final_p[i])
        r_t.append(final_t[i])
        if i < len(final_p) - 1:
            gap_l = final_p[i]
            gap_h = final_p[i + 1]
            l_base = gap_l
            safety = 0
            while (
                gap_l > 0
                and l_base > 0
                and (gap_h - l_base) / l_base * 100 > bridge_law
                and safety < 10
            ):
                safety += 1
                found: Optional[float] = None
                for pvt in b_pool:
                    if pvt > l_base * (1 + expansion / 100) and pvt < gap_h * 0.98:
                        found = pvt
                        break
                if found is None:
                    break
                r_p.append(found)
                r_t.append(3)
                l_base = found

    r_p, r_t = _sort_asc(r_p, r_t)

    # 6. Global Expansion (20%) spacing enforcement, ascending.
    f_p: list[float] = []
    f_t: list[int] = []
    last_kept = -1.0
    for p, t in zip(r_p, r_t):
        if last_kept <= 0.0 or (last_kept > 0 and (p - last_kept) / last_kept * 100 >= expansion):
            f_p.append(p)
            f_t.append(t)
            last_kept = p
    r_p, r_t = f_p, f_t

    # 7. Recursive median injection: any gap wider than the Median law % gets
    #    its midpoint, provided BOTH halves still respect Expansion spacing
    #    and the midpoint clears the price floor. Repeats until stable
    #    (bounded at 20 sweeps like the reference).
    for _ in range(20):
        injected = False
        if len(r_p) >= 2:
            n_p: list[float] = []
            n_t: list[int] = []
            for i in range(len(r_p)):
                n_p.append(r_p[i])
                n_t.append(r_t[i])
                if i < len(r_p) - 1:
                    lo = r_p[i]
                    hi = r_p[i + 1]
                    if lo > 0 and (hi - lo) / lo * 100 > median_law:
                        med = (lo + hi) / 2.0
                        ok_lo = (med - lo) / lo * 100 >= expansion
                        ok_hi = (hi - med) / med * 100 >= expansion
                        if med > floor and ok_lo and ok_hi:
                            n_p.append(med)
                            n_t.append(4)
                            injected = True
            r_p, r_t = n_p, n_t
        if not injected:
            break

    return [Level(price=p, type=t, name=LEVEL_TYPE_NAMES[t]) for p, t in zip(r_p, r_t)]
