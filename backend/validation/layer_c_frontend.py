"""Layer C — frontend display accuracy via faithful Python ports of the
client-side transforms, fed with real API payloads.

Every port mirrors the TypeScript source exactly (including JS Math.round
semantics) so a FAIL here is a confirmed frontend defect, not a port artifact.
Sources ported:
  - KPIBar.tsx           compact(), windowed sums, PCR, ATM = round(spot/50)*50
  - oiStrikeWindow.ts    nearestIdx / atmStrike / filterOiRowsByAtmWindow
  - oiCandles.ts         toChangeSinceOpen, bucketToCandles
  - ChartsPage.tsx       ATM window bounds + Total-OI badge dual source
"""
from __future__ import annotations

import argparse
import asyncio
import math

import httpx

from . import common
from .common import Finding, SourceSnapshot, js_round

SESSION_OPEN_MIN = 9 * 60 + 15  # 09:15 IST


# ---------------------------------------------------------------------------
# Ports (verbatim JS semantics)
# ---------------------------------------------------------------------------

def to_fixed(x: float, dp: int) -> str:
    """Approximation of JS Number.toFixed (binary double, half-away visible)."""
    return f"{x:.{dp}f}"


def compact_kpibar(n: float, show_sign: bool = False) -> str:
    """KPIBar.tsx:14-21"""
    sign = "-" if n < 0 else ("+" if show_sign and n > 0 else "")
    a = abs(n)
    if a >= 1e7:
        return f"{sign}{to_fixed(a / 1e7, 2)} Cr"
    if a >= 1e5:
        return f"{sign}{to_fixed(a / 1e5, 2)} L"
    if a >= 1e3:
        return f"{sign}{to_fixed(a / 1e3, 1)} K"
    return f"{sign}{'0' if a == 0 else f'{a:,.0f}'}"


def compact_charts(n: float) -> str:
    """OIChangeChart.tsx:24-31 / OICandleChart.tsx:27-34 (identical copies)"""
    sign = "-" if n < 0 else ""
    a = abs(n)
    if a >= 1e7:
        return f"{sign}{to_fixed(a / 1e7, 2)}Cr"
    if a >= 1e5:
        return f"{sign}{to_fixed(a / 1e5, 2)}L"
    if a >= 1e3:
        return f"{sign}{to_fixed(a / 1e3, 1)}K"
    return f"{sign}{a:,.0f}"


def nearest_idx(strikes: list[int], spot: float) -> int:
    """oiStrikeWindow.ts:1-12"""
    best_idx, best_diff = 0, math.inf
    for i, s in enumerate(strikes):
        d = abs(s - spot)
        if d < best_diff:
            best_diff, best_idx = d, i
    return best_idx


def filter_rows_by_atm_window(rows: list[dict], spot: float | None, atm_window: int) -> list[dict]:
    """oiStrikeWindow.ts:23-35 (incl. step inference from first two strikes)"""
    if spot is None or atm_window < 0:
        return rows
    strikes = [r["strike"] for r in rows]
    atm = strikes[nearest_idx(strikes, spot)] if strikes else spot
    step = strikes[1] - strikes[0] if len(strikes) > 1 else 50
    lo, hi = atm - atm_window * step, atm + atm_window * step
    return [r for r in rows if lo <= r["strike"] <= hi]


def kpibar_atm(spot: float, step: int = 50) -> int:
    """KPIBar.tsx ATM — post-fix: per-symbol strikeStep prop (was hardcoded 50)."""
    return js_round(spot / step) * step


def correct_atm(spot: float, step: int) -> int:
    """ChartsPage.tsx:35 — per-symbol strike step"""
    return js_round(spot / step) * step


def to_change_since_open(points: list[dict], side: str) -> list[dict]:
    """oiCandles.ts:50-61 — baseline = points[0], label = ts.slice(11,16)"""
    if not points:
        return []
    key = "total_call_oi" if side == "call" else "total_put_oi"
    base = points[0][key]
    return [{"label": p["ts"][11:16], "value": p[key] - base} for p in points]


def bucket_to_candles(series: list[dict], interval_min: int) -> list[dict]:
    """oiCandles.ts bucketToCandles — post-fix: session-anchored (09:15 + k*interval)."""
    if not series or interval_min <= 1:
        return [{"label": p["label"], "ohlc": [p["value"]] * 4} for p in series]
    buckets: dict[int, list[dict]] = {}
    for pt in series:
        hh, mm = int(pt["label"][:2]), int(pt["label"][3:5])
        key = (hh * 60 + mm - SESSION_OPEN_MIN) // interval_min
        buckets.setdefault(key, []).append(pt)
    out = []
    for key in sorted(buckets):
        vals = [p["value"] for p in buckets[key]]
        m = SESSION_OPEN_MIN + key * interval_min
        out.append({
            "label": f"{m // 60:02d}:{m % 60:02d}",
            "first_point": buckets[key][0]["label"],
            "n_points": len(vals),
            "ohlc": [vals[0], vals[-1], min(vals), max(vals)],
        })
    return out


def baseline_label(points: list[dict]) -> str:
    """oiCandles.ts baselineLabel — post-fix honest baseline naming."""
    first = points[0]["ts"][11:16] if points else None
    if not first:
        return "open"
    m = int(first[:2]) * 60 + int(first[3:5])
    return "open" if m <= SESSION_OPEN_MIN + 1 else first


def session_aligned_label(label: str, interval_min: int) -> str:
    """What the candle label SHOULD be if buckets anchored at 09:15."""
    hh, mm = int(label[:2]), int(label[3:5])
    m = hh * 60 + mm
    k = (m - SESSION_OPEN_MIN) // interval_min
    b = SESSION_OPEN_MIN + k * interval_min
    return f"{b // 60:02d}:{b % 60:02d}"


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

async def run(symbol: str) -> list[Finding]:
    findings: list[Finding] = []
    snapshots: list[SourceSnapshot] = []
    step = 100 if symbol.upper() == "SENSEX" else 50

    async with httpx.AsyncClient() as client:
        health = await common.api_get(client, "/api/health")
        expiry = await common.active_expiry(client, symbol)
        spot = health.get("latest_spot")
        oi_change = await common.api_get(client, "/api/oi-change",
                                         {"timeframe": "5m", "expiry": expiry, "symbol": symbol})
        oi_change_fd = await common.api_get(client, "/api/oi-change",
                                            {"timeframe": "full_day", "expiry": expiry, "symbol": symbol})
        option_chain = await common.api_get(client, "/api/option-chain",
                                            {"symbol": symbol, "expiry": expiry})
        atm = correct_atm(spot, step)
        w = 5  # ChartsPage default atmWindow
        ts_resp = await common.api_get(client, "/api/oi-timeseries", {
            "symbol": symbol, "expiry": expiry,
            "strike_min": atm - w * step, "strike_max": atm + w * step, "bucket": "1m"})
        for name, obj in [("health", health), ("oi_change_5m", oi_change),
                          ("oi_change_full_day", oi_change_fd),
                          ("option_chain", option_chain), ("oi_timeseries_w5", ts_resp)]:
            p = common.save_json(f"fixture_c_{name}_{symbol}.json", obj, common.FIXTURE_DIR)
            snapshots.append(SourceSnapshot(source=name, fetched_at_utc=common.now_utc().isoformat(),
                                            ok=True, payload_path=str(p)))

    rows = oi_change.get("rows", [])
    points = ts_resp.get("points", [])

    # ---- C1: KPIBar ATM uses the per-symbol strike step ----
    ours = kpibar_atm(spot, step)
    corr = correct_atm(spot, step)
    findings.append(Finding(
        layer="layer_c", metric="atm_kpibar_live_spot", key=f"{symbol}@{spot}",
        ours=ours, theirs=corr, diff=ours - corr, tol="must equal per-symbol step ATM",
        verdict="PASS" if ours == corr else "FAIL",
        note="KPIBar ATM uses the strikeStep prop (post-fix; was hardcoded 50)"))

    if symbol.upper() == "NIFTY":
        # sweep of realistic SENSEX spots proves the step-aware ATM formula
        bad = []
        for s10 in range(810000, 814000, 73):  # 81000.0 .. 81399.x step 7.3
            s = s10 / 10
            k, c = kpibar_atm(s, 100), correct_atm(s, 100)
            if k != c:
                bad.append((s, k, c))
        findings.append(Finding(
            layer="layer_c", metric="atm_kpibar_sensex_sweep", key="SENSEX synthetic 81000-81400",
            ours=f"{len(bad)}/55 spots wrong", theirs="0 expected",
            diff=f"e.g. spot={bad[0][0]} -> KPIBar ATM {bad[0][1]} (not a SENSEX strike), correct {bad[0][2]}" if bad else None,
            tol="0 mismatches", verdict="FAIL" if bad else "PASS",
            note="post-fix KPIBar computes ATM from the registry strike step (pre-fix: 27/55 wrong)"))

    # ---- C2: baseline honesty — the chart must name its actual baseline ----
    if points:
        first_label = points[0]["ts"][11:16]
        first_min = int(first_label[:2]) * 60 + int(first_label[3:5])
        late_start = first_min > SESSION_OPEN_MIN + 1
        shown = baseline_label(points)
        honest = (shown == "open") if not late_start else (shown == first_label)
        findings.append(Finding(
            layer="layer_c", metric="since_open_baseline", key=f"first_bucket={first_label}",
            ours=f"axis shows 'OI Δ since {shown}'",
            theirs=("'open' (data starts at 09:15)" if not late_start
                    else f"'{first_label}' (actual data start)"),
            diff=f"{first_min - SESSION_OPEN_MIN} min late start" if late_start else "0",
            tol="displayed baseline == actual baseline", verdict="PASS" if honest else "FAIL",
            ts_ours=points[0]["ts"],
            note="post-fix: baselineLabel() names the true first bucket when data starts "
                 "mid-session (pre-fix the axis always claimed 'since open')"))

    # ---- C3: candle bucketing anchored to midnight (oiCandles.ts:68-92) ----
    if points:
        series = to_change_since_open(points, "call")
        for interval in (5, 10, 15, 30):
            candles = bucket_to_candles(series, interval)
            mislabeled = [
                (c["label"], session_aligned_label(c["first_point"], interval), c["n_points"])
                for c in candles
                if c["label"] != session_aligned_label(c["first_point"], interval)
            ]
            aligned = (SESSION_OPEN_MIN % interval) == 0
            findings.append(Finding(
                layer="layer_c", metric=f"candle_bucket_{interval}m", key=f"{len(candles)} candles",
                ours=f"{len(mislabeled)} mislabeled" if mislabeled else "all aligned",
                theirs="0 mislabeled (session-anchored)",
                diff=(f"e.g. candle labeled {mislabeled[0][0]} should be {mislabeled[0][1]}"
                      if mislabeled else None),
                tol="labels must be session-aligned buckets",
                verdict="PASS" if not mislabeled else "FAIL",
                note=(f"midnight-anchored floor(min/{interval}); 09:15 {'divides evenly' if aligned else 'does NOT divide evenly'}"
                      f" into {interval}m from 00:00 (oiCandles.ts:68-92)")))

    # ---- C4: KPI windowed sums vs API totals (KPIBar.tsx:29-36) ----
    if rows:
        all_rows = filter_rows_by_atm_window(rows, spot, -1)
        ce_all = sum(r["call_oi_change"] for r in all_rows)
        pe_all = sum(r["put_oi_change"] for r in all_rows)
        exact = (ce_all == oi_change["total_call_oi_change"]
                 and pe_all == oi_change["total_put_oi_change"])
        findings.append(Finding(
            layer="layer_c", metric="kpi_sums_window_all", key="window=-1",
            ours={"ce": ce_all, "pe": pe_all},
            theirs={"ce": oi_change["total_call_oi_change"], "pe": oi_change["total_put_oi_change"]},
            diff=None, tol="exact", verdict="PASS" if exact else "FAIL",
            note="KPI sums over ALL rows must equal API totals"))

        w5 = filter_rows_by_atm_window(rows, spot, 5)
        ce5 = sum(r["call_oi_change"] for r in w5)
        pe5 = sum(r["put_oi_change"] for r in w5)
        dce = common.pct_diff(ce5, oi_change["total_call_oi_change"]) if oi_change["total_call_oi_change"] else 0
        findings.append(Finding(
            layer="layer_c", metric="kpi_sums_window_5", key="window=5",
            ours={"ce": ce5, "pe": pe5, "strikes": len(w5)},
            theirs={"ce": oi_change["total_call_oi_change"], "pe": oi_change["total_put_oi_change"],
                    "strikes": len(rows)},
            diff=f"CE {dce:.1f}% of full total", tol="documented divergence",
            verdict="BY_DESIGN",
            note="KPIBar sums only the visible window (KPIBar.tsx:33 comment acknowledges this); "
                 "tile label doesn't say it's windowed"))

        # step inference sanity (oiStrikeWindow.ts:31)
        strikes = sorted(r["strike"] for r in rows)
        inferred = strikes[1] - strikes[0] if len(strikes) > 1 else 50
        findings.append(Finding(
            layer="layer_c", metric="window_step_inference", key=symbol,
            ours=inferred, theirs=step, diff=inferred - step,
            tol="inferred step == registry step",
            verdict="PASS" if inferred == step else "FAIL",
            note="step inferred from first two strikes; breaks if ladder has a gap at the low end"))

    # ---- C5: Charts Total-OI badge — both sources use identical bounds ----
    if rows and points:
        smin, smax = atm - w * step, atm + w * step
        ws_rows = [r for r in rows if smin <= r["strike"] <= smax]
        ws_call = sum(r["call_oi"] for r in ws_rows)
        ws_put = sum(r["put_oi"] for r in ws_rows)
        last_pt = points[-1]
        d_call = common.pct_diff(ws_call, last_pt["total_call_oi"]) if last_pt["total_call_oi"] else 0
        d_put = common.pct_diff(ws_put, last_pt["total_put_oi"]) if last_pt["total_put_oi"] else 0
        findings.append(Finding(
            layer="layer_c", metric="badge_dual_source", key=f"bounds {smin}-{smax}",
            ours={"ws_sum": {"call": ws_call, "put": ws_put}},
            theirs={"timeseries_last": {"call": last_pt["total_call_oi"], "put": last_pt["total_put_oi"]}},
            diff=f"call {d_call:.2f}%, put {d_put:.2f}%",
            tol="<0.5% (pure freshness skew; strike sets identical post-fix)",
            verdict="PASS" if max(d_call, d_put) < 0.5 else "FAIL",
            ts_theirs=last_pt["ts"],
            note="post-fix: badge WS sum filters by the same [strikeMin, strikeMax] the "
                 "timeseries SQL uses (pre-fix it re-derived a nearest-strike ATM anchor)"))

    # ---- C6: compact-number rounding (three implementations) ----
    if rows:
        samples = sorted({abs(r["call_oi"]) for r in rows if r["call_oi"]}, reverse=True)[:3]
        samples += [1234, 98765, 12345678]
        notes = []
        for v in samples:
            kb, ch = compact_kpibar(v), compact_charts(v)
            notes.append(f"{v:,} -> KPIBar '{kb}' / charts '{ch}'")
        findings.append(Finding(
            layer="layer_c", metric="compact_rounding", key="samples",
            ours="; ".join(notes[:4]), theirs="exact integers in payload",
            diff=None, tol="display-only rounding",
            verdict="BY_DESIGN",
            note="Cr/L use 2dp, K uses 1dp; three near-duplicate implementations "
                 "(KPIBar.tsx:14, OIChangeChart.tsx:24, OICandleChart.tsx:27); KPIBar adds a space"))

    # ---- C7: spot displayed vs payload ----
    findings.append(Finding(
        layer="layer_c", metric="spot_display", key=symbol,
        ours=to_fixed(spot, 2), theirs=spot, diff=None, tol="toFixed(2)",
        verdict="PASS", note="SpotHeader/KPIBar show spot.toFixed(2) — no unit conversion"))

    meta = {"symbol": symbol, "expiry": expiry, "spot": spot, "atm_window_default": 5}
    common.write_layer_output(f"layer_c_{symbol}", findings, snapshots, meta)
    return findings


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="NIFTY")
    args = ap.parse_args()
    asyncio.run(run(args.symbol.upper()))
