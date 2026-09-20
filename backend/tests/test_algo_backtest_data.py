"""Backtest day-frame semantics — the SQL-parity layer.

Run:  cd backend && PYTHONPATH=. python tests/test_algo_backtest_data.py

These pin the traps that would silently corrupt a backtest:
- window-scoped first-seen seeding (a late-arriving strike retroactively
  rewrites EARLIER buckets' absolute totals for later cursors — MQAE's PCR
  input is NOT prefix-stable),
- locf carry + real_end clamp,
- closed-candle discipline at the cursor,
- spot carry from the previous session (pre-open ATM),
- the deterministic strike picker (closed minutes only, recency window,
  lowest-strike tie-break).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.algo.backtest.data import (
    MINUTES_PER_DAY,
    DayFrame,
    ExpiryMap,
    build_day_frame,
    minute_bar,
    oi_change_pair_at,
    pick_strike_in_band,
    ratio_pair_at,
    resolve_basket,
    totals_points,
)

D = date(2026, 3, 3)                     # a Tuesday
OPEN_UTC = datetime(2026, 3, 3, 3, 45, tzinfo=timezone.utc)   # 09:15 IST


def bucket(i: int) -> datetime:
    return OPEN_UTC + timedelta(minutes=i)


def row(strike, ot, i, oi=None, ltp=None):
    r = {"strike": strike, "option_type": ot, "bucket": bucket(i),
         "oi": oi, "o": None, "h": None, "l": None, "c": None}
    if ltp is not None:
        r.update({"o": ltp, "h": ltp + 0.5, "l": ltp - 0.5, "c": ltp})
    return r


def mk_frame(chain, spot_rows=None, preopen=None, bounds=(22000, 23000), step=50):
    f = build_day_frame(
        trade_date=D, symbol="NIFTY", expiry=date(2026, 3, 3),
        open_utc=OPEN_UTC, chain=chain, spot_rows=spot_rows or [],
        preopen=preopen, bounds=bounds, strike_step=step,
    )
    assert f is not None
    return f


def ist(minute_offset: int) -> datetime:
    """IST-naive wall clock at 09:15 + offset — the evaluate_minute boundary."""
    return datetime(2026, 3, 3, 9, 15) + timedelta(minutes=minute_offset)


# ── ExpiryMap ────────────────────────────────────────────────────────────────

def test_expiry_map_day_boundaries():
    em = ExpiryMap(by_symbol={"NIFTY": [date(2026, 3, 3), date(2026, 3, 10)]})
    assert em.for_day("NIFTY", date(2026, 3, 3)) == date(2026, 3, 3), "expiry day maps to itself"
    assert em.for_day("NIFTY", date(2026, 3, 4)) == date(2026, 3, 10), "day after → next weekly"
    assert em.for_day("NIFTY", date(2026, 3, 11)) is None, "past the last stored expiry"
    assert em.for_day("SENSEX", date(2026, 3, 3)) is None, "unknown symbol"


# ── locf + first-seen + membership ──────────────────────────────────────────

def test_late_arriving_strike_rewrites_earlier_buckets():
    """The SQL's window-scoped first-seen: strike B (first tick at minute 3)
    is invisible at cursor 2, and at cursor 5 its first OI seeds buckets 0–2
    retroactively — absolute totals for EARLY buckets differ between cursors."""
    chain = [
        row(22500, "CE", 0, oi=1000), row(22500, "CE", 1, oi=1100),
        row(22500, "CE", 2, oi=1200), row(22500, "CE", 5, oi=1500),
        row(22500, "PE", 0, oi=2000), row(22500, "PE", 5, oi=2500),
        row(22600, "CE", 3, oi=700),  row(22600, "CE", 4, oi=800),
    ]
    f = mk_frame(chain)

    early = totals_points(f, 22000, 23000, 2)
    assert len(early) == 3
    assert [p.total_call_oi for p in early] == [1000, 1100, 1200], "B not yet a member"
    assert [p.total_put_oi for p in early] == [2000, 2000, 2000], "PE locf carry"

    late = totals_points(f, 22000, 23000, 5)
    assert len(late) == 6
    # B's first OI (700) seeds its leading buckets 0..2 (COALESCE(p.oi, f.oi0)).
    assert [p.total_call_oi for p in late] == [1700, 1800, 1900, 1900, 2000, 2300]
    assert late[0].pcr != early[0].pcr, "bucket 0's PCR changed retroactively — not prefix-stable"


def test_real_end_clamp_stops_at_last_tick():
    chain = [row(22500, "CE", 0, oi=100), row(22500, "CE", 4, oi=200),
             row(22500, "PE", 0, oi=100)]
    f = mk_frame(chain)
    pts = totals_points(f, 22000, 23000, 60)
    assert len(pts) == 5, f"series must stop at the last real tick (minute 4), got {len(pts)}"


def test_membership_requires_strike_in_range():
    chain = [row(22500, "CE", 0, oi=100), row(24000, "CE", 0, oi=999),
             row(22500, "PE", 0, oi=50)]
    f = mk_frame(chain, bounds=(22500, 24000))
    pts = totals_points(f, 22400, 22600, 0)
    assert pts[0].total_call_oi == 100, "out-of-range strike excluded"


# ── pair builders: closed-candle discipline ─────────────────────────────────

def test_closed_candle_discipline_at_cursor():
    chain = []
    for i in range(6):
        chain.append(row(22500, "CE", i, oi=1000 + i * 10))
        chain.append(row(22500, "PE", i, oi=2000 + i * 10))
    spot = [{"bucket": bucket(0), "spot": 22510.0}]
    f = mk_frame(chain, spot_rows=spot)

    pair = oi_change_pair_at(f, 10, ist(5))
    assert pair is not None
    # Cursor 09:20 → buckets 09:15..09:19 are closed; the 09:20 bucket (if any)
    # would still be forming. Here data ends at minute 5 = 09:20 → dropped.
    assert len(pair.timestamps) == 5, f"expected 5 closed buckets, got {len(pair.timestamps)}"
    assert pair.call_change_cr[0] == 0.0, "cumulative change starts at 0"

    rp = ratio_pair_at(f, 10, ist(5))
    assert rp is not None and len(rp.green_pcr) == 5


def test_forming_minute_never_joins_membership():
    """A strike whose FIRST tick lands in the evaluation minute (still
    forming) must not enter membership — its first-seen seed would rewrite
    earlier buckets with knowledge the live instant did not have."""
    chain = [
        row(22500, "CE", 0, oi=1000), row(22500, "CE", 5, oi=1200),
        row(22500, "PE", 0, oi=2000),
        row(22600, "CE", 5, oi=700),          # first tick at minute 5
    ]
    spot = [{"bucket": bucket(0), "spot": 22510.0}]
    f = mk_frame(chain, spot_rows=spot)

    # Evaluation at 09:20 (minute 5 is forming): 22600 invisible.
    pair = oi_change_pair_at(f, 10, ist(5))
    assert pair is not None
    assert pair.call_change_cr[0] == 0.0
    assert len(pair.timestamps) == 1, "only minute 0 has closed data (real_end clamp)"

    # One minute later (minute 5 closed): 22600 joins and seeds bucket 0.
    f2 = mk_frame(chain, spot_rows=spot)
    pair2 = oi_change_pair_at(f2, 10, ist(6))
    assert pair2 is not None
    assert len(pair2.timestamps) == 6, f"minutes 0..5 closed, got {len(pair2.timestamps)}"


# ── spot / basket ───────────────────────────────────────────────────────────

def test_preopen_spot_carries_previous_session():
    chain = [row(22500, "CE", 0, oi=100), row(22500, "PE", 0, oi=100)]
    f = mk_frame(chain, spot_rows=[], preopen=22480.0)
    basket = resolve_basket(f, 2, 0)
    assert basket is not None
    smin, smax, spot = basket
    assert spot == 22480.0, "pre-first-tick ATM must use the previous session's spot"
    assert (smin, smax) == (22400, 22600)


def test_spotless_degrades_to_full_chain():
    chain = [row(22500, "CE", 0, oi=100), row(22500, "PE", 0, oi=100)]
    f = mk_frame(chain, spot_rows=[], preopen=None, bounds=(21000, 24000))
    assert f.spotless is True
    basket = resolve_basket(f, 2, 0)
    assert basket == (21000, 24000, None), "no spot → full stored chain, exactly like live"


def test_negative_window_is_full_chain():
    chain = [row(22500, "CE", 0, oi=100)]
    f = mk_frame(chain, spot_rows=[{"bucket": bucket(0), "spot": 22500.0}],
                 bounds=(21000, 24000))
    assert resolve_basket(f, -1, 0) == (21000, 24000, None)


def test_spot_locf_across_day():
    chain = [row(22500, "CE", 0, oi=100)]
    spot = [{"bucket": bucket(0), "spot": 22500.0}, {"bucket": bucket(10), "spot": 22600.0}]
    f = mk_frame(chain, spot_rows=spot)
    assert f.spot[5] == 22500.0
    assert f.spot[10] == 22600.0
    assert f.spot[MINUTES_PER_DAY - 1] == 22600.0


# ── minute bars ─────────────────────────────────────────────────────────────

def test_minute_bar_silent_minute_is_none():
    chain = [row(22500, "CE", 0, oi=100, ltp=101.0), row(22500, "CE", 2, oi=110, ltp=103.0)]
    f = mk_frame(chain)
    assert minute_bar(f, 22500, "CE", ist(1)) is None, "no tick that minute → None (live parity)"
    bar = minute_bar(f, 22500, "CE", ist(2))
    assert bar is not None and bar[1] == 103.0 and bar[2] == 103.5 and bar[3] == 102.5
    assert minute_bar(f, 22500, "CE", datetime(2026, 3, 3, 8, 0)) is None, "outside session"
    assert minute_bar(f, 99999, "CE", ist(2)) is None, "unknown contract"


# ── strike picker ───────────────────────────────────────────────────────────

def test_pick_strike_band_and_tiebreak():
    chain = [
        row(22400, "CE", 8, oi=1, ltp=95.0),
        row(22500, "CE", 8, oi=1, ltp=105.0),
        row(22600, "CE", 8, oi=1, ltp=140.0),   # out of band
        row(22700, "CE", 8, oi=1, ltp=50.0),    # out of band
    ]
    f = mk_frame(chain)
    # Band 90–110, mid 100: 95 and 105 tie at distance 5 → LOWEST strike wins.
    picked = pick_strike_in_band(f, "CE", 90.0, 110.0, ist(9))
    assert picked == (22400, 95.0), f"tie must go to the lowest strike, got {picked}"


def test_pick_strike_uses_closed_minute_not_forming():
    chain = [
        row(22500, "CE", 5, oi=1, ltp=100.0),
        row(22500, "CE", 6, oi=1, ltp=300.0),   # the cursor's own (forming) minute
    ]
    f = mk_frame(chain)
    picked = pick_strike_in_band(f, "CE", 90.0, 110.0, ist(6))
    assert picked == (22500, 100.0), "the forming minute's close is lookahead — must use minute 5"


def test_pick_strike_recency_window():
    chain = [row(22500, "CE", 0, oi=1, ltp=100.0)]     # last tick at 09:15
    f = mk_frame(chain)
    picked = pick_strike_in_band(f, "CE", 90.0, 110.0, ist(30))
    assert picked is None, "a strike quiet for >15 minutes must not qualify (NOW()-15min parity)"
    picked2 = pick_strike_in_band(f, "CE", 90.0, 110.0, ist(10))
    assert picked2 == (22500, 100.0), "inside the window it qualifies via locf'd last close"


def test_pick_strike_band_edges_inclusive():
    chain = [row(22500, "CE", 3, oi=1, ltp=90.0), row(22600, "CE", 3, oi=1, ltp=110.0)]
    f = mk_frame(chain)
    assert pick_strike_in_band(f, "CE", 90.0, 110.0, ist(4)) is not None
    assert pick_strike_in_band(f, "CE", 91.0, 109.0, ist(4)) is None, "strictly outside band"


def test_pick_strikes_top_n_ordered_nearest_mid():
    from app.algo.backtest.data import pick_strikes_in_band

    chain = [
        row(22400, "CE", 8, oi=1, ltp=95.0),    # dist 5 (tie, lower strike)
        row(22500, "CE", 8, oi=1, ltp=105.0),   # dist 5
        row(22450, "CE", 8, oi=1, ltp=100.0),   # dist 0 → nearest
        row(22600, "CE", 8, oi=1, ltp=140.0),   # out of band
    ]
    f = mk_frame(chain)
    picked = pick_strikes_in_band(f, "CE", 90.0, 110.0, ist(9), 3)
    assert picked == [(22450, 100.0), (22400, 95.0), (22500, 105.0)], (
        f"nearest-mid first, ties → lowest strike, got {picked}"
    )
    # count trims from the tail; head element == the single-pick regression pin
    assert pick_strikes_in_band(f, "CE", 90.0, 110.0, ist(9), 2) == picked[:2]
    assert pick_strikes_in_band(f, "CE", 90.0, 110.0, ist(9), 1)[0] == \
        pick_strike_in_band(f, "CE", 90.0, 110.0, ist(9))


# ── brute-force cross-check of the whole pipeline ───────────────────────────

def test_totals_match_bruteforce_reference():
    """Independent per-cursor reference: for each member strike, value at
    bucket m = last non-null OI ≤ m, else its first-in-window OI."""
    import random
    rng = random.Random(7)
    chain = []
    truth: dict[tuple[int, str], dict[int, int]] = {}
    for strike in (22400, 22500, 22600):
        for ot in ("CE", "PE"):
            first = rng.randrange(0, 6)
            oi_val = rng.randrange(500, 5000)
            ticks = {}
            for m in range(first, 30):
                if rng.random() < 0.6:
                    oi_val += rng.randrange(-50, 60)
                    ticks[m] = oi_val
                    chain.append(row(strike, ot, m, oi=oi_val))
            truth[(strike, ot)] = ticks
    f = mk_frame(chain)

    for cursor in (0, 3, 7, 15, 29):
        pts = totals_points(f, 22000, 23000, cursor)
        members = {k: t for k, t in truth.items() if t and min(t) <= cursor}
        if not members:
            assert pts == []
            continue
        real_end = max(max(m for m in t if m <= cursor) if any(m <= cursor for m in t) else -1
                       for t in members.values())
        assert len(pts) == real_end + 1, f"cursor {cursor}: real_end clamp mismatch"
        for m in range(real_end + 1):
            ce = pe = 0
            for (strike, ot), ticks in members.items():
                past = [v for mm, v in sorted(ticks.items()) if mm <= m]
                v = past[-1] if past else ticks[min(ticks)]
                if ot == "CE":
                    ce += v
                else:
                    pe += v
            assert pts[m].total_call_oi == ce, f"cursor {cursor} bucket {m} CE"
            assert pts[m].total_put_oi == pe, f"cursor {cursor} bucket {m} PE"



def test_fast_pairs_match_pure_transforms():
    """The incremental fast-pair path must be byte-identical to feeding
    totals_points through the pure transforms (the parity seams), across
    random data and many cursors — including late joiners and quiet gaps."""
    import random

    from app.algo.backtest.data import _pairs_at
    from app.algo.series import oi_change_pair_from_points, ratio_pair_from_points
    from app.core.time_utils import ist_naive_to_utc

    rng = random.Random(21)
    chain = []
    for strike in (22400, 22500, 22600, 22700):
        for ot in ("CE", "PE"):
            first = rng.randrange(0, 12)
            oi_val = rng.randrange(500, 5000)
            for m in range(first, 60):
                if rng.random() < 0.55:
                    oi_val += rng.randrange(-50, 60)
                    chain.append(row(strike, ot, m, oi=oi_val, ltp=50.0 + m))
    spot = [{"bucket": bucket(0), "spot": 22510.0}, {"bucket": bucket(25), "spot": 22610.0}]

    for cursor in (1, 3, 9, 20, 33, 47, 60):
        for w in (2, -1):
            f_fast = mk_frame(chain, spot_rows=spot, bounds=(22400, 22700))
            f_ref = mk_frame(chain, spot_rows=spot, bounds=(22400, 22700))
            now = ist(cursor)
            oic_fast, rp_fast = _pairs_at(f_fast, w, now)

            last_idx = cursor - 1
            basket = resolve_basket(f_ref, w, last_idx) if last_idx >= 0 else None
            oic_ref = rp_ref = None
            if basket is not None:
                smin, smax, sp = basket
                pts = totals_points(f_ref, smin, smax, last_idx)
                if pts:
                    oic_ref = oi_change_pair_from_points(
                        pts, now_utc=ist_naive_to_utc(now),
                        strike_min=smin, strike_max=smax, spot=sp)
                    rp_ref = ratio_pair_from_points(
                        pts, now_utc=ist_naive_to_utc(now),
                        strike_min=smin, strike_max=smax, spot=sp)
            assert (oic_fast is None) == (oic_ref is None), f"c{cursor} w{w} oic presence"
            if oic_fast and oic_ref:
                assert oic_fast.timestamps == oic_ref.timestamps, f"c{cursor} w{w} ts"
                assert oic_fast.call_change_cr == oic_ref.call_change_cr, f"c{cursor} w{w} call"
                assert oic_fast.put_change_cr == oic_ref.put_change_cr, f"c{cursor} w{w} put"
                assert (oic_fast.strike_min, oic_fast.strike_max, oic_fast.spot) == (
                    oic_ref.strike_min, oic_ref.strike_max, oic_ref.spot)
            assert (rp_fast is None) == (rp_ref is None), f"c{cursor} w{w} rp presence"
            if rp_fast and rp_ref:
                assert rp_fast.timestamps == rp_ref.timestamps
                assert rp_fast.green_pcr == rp_ref.green_pcr, f"c{cursor} w{w} green"
                assert rp_fast.yellow_ratio == rp_ref.yellow_ratio, f"c{cursor} w{w} yellow"


def test_incremental_state_survives_basket_reuse():
    """Advancing the SAME frame through increasing cursors must equal fresh
    frames at each cursor (forward-only state correctness)."""
    chain = [row(22500, "CE", 0, oi=1000), row(22500, "PE", 0, oi=900),
             row(22600, "CE", 7, oi=700), row(22500, "CE", 12, oi=1100),
             row(22600, "PE", 15, oi=444), row(22500, "PE", 20, oi=950)]
    shared = mk_frame(chain)
    for cursor in (1, 5, 8, 13, 16, 21, 30):
        fresh = mk_frame(chain)
        a = totals_points(shared, 22000, 23000, cursor)
        b = totals_points(fresh, 22000, 23000, cursor)
        assert [(p.total_call_oi, p.total_put_oi) for p in a] ==                [(p.total_call_oi, p.total_put_oi) for p in b], f"cursor {cursor}"


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
    print("all backtest data tests passed")


if __name__ == "__main__":
    _run_all()
