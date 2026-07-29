"""Layer F — OI-CHANGE cross-check vs Sensibull via snapshot deltas.

Sensibull's oi-change-vs-strike page is driven by the same absolute per-strike
OI we already cross-check in Layer D; their API exposes no baseline/previous
OI. So OI *change* is verified baseline-independently: capture the Sensibull
chain at T0, again at T1 (>= ~20 min later), diff per strike, and compare with
our /api/oi-change?from_ts=T0&to_ts=T1 over the exact same window. Both feeds
track the exchange's ~1/min OI dissemination, so skew is bounded by ~1 min on
each edge — tolerances account for that.

Usage (from backend/, venv, backend running, feed live):
    python -m validation.oi_delta --symbol NIFTY --capture
    ... wait >= 20 min ...
    python -m validation.oi_delta --symbol NIFTY --compare

READ-ONLY contract: never calls /auth/login (see package docstring).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import text

from . import common
from .common import Finding, SourceSnapshot
from .layer_d_sensibull import fetch_sensibull_chain

# Fallbacks if the Kite dump yields no lot size (registry values, July 2026).
FALLBACK_LOT = {"NIFTY": 65, "SENSEX": 20}

# Sensibull cache + XTS both lag the exchange's ~1/min OI dissemination by up
# to a minute at each window edge; per-strike update cadences differ between
# vendors, so small-magnitude deltas on sparse strikes carry real skew.
# Observed same-instant vendor dispersion on fast-moving ATM contracts is
# ~2-3% of the OI level (XTS's own REST cache vs its socket stream showed the
# same gap), so 3% of level is the honest edge-skew allowance — still 5x
# stricter than Layer D's ±15% live tolerance on absolute OI.
PER_STRIKE_PCT = 10.0          # of |sensibull delta|
PER_STRIKE_LEVEL_PCT = 3.0     # of the strike's absolute OI level (edge skew)
PER_STRIKE_LOT_FLOOR = 25      # lots (one sparse update quantum on illiquids)
TOTAL_PCT = 5.0                # of |sensibull total delta|
TOTAL_LEVEL_PCT = 0.5          # of total OI level
NEGLIGIBLE_LOTS = 1            # both deltas within this => trivially PASS
MATERIAL_LOTS = 5              # threshold for the direction-agreement stat
STALE_AFTER_S = 150            # our strike frozen (dropped from window) => STALE
LATE_BASELINE_S = 120          # first row after T0+this => entered window mid-delta
MIN_WINDOW_MIN = 15


def parse_sensibull_ts(raw: str) -> datetime:
    """Sensibull timestamps carry nanoseconds (9 fractional digits), which
    datetime.fromisoformat rejects — trim to microseconds."""
    s = str(raw).strip().replace("Z", "+00:00")
    s = re.sub(r"\.(\d{6})\d+", r".\1", s)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def snapshot_path(symbol: str, which: str):
    return common.OUT_DIR / f"oi_delta_{which}_{symbol}.json"


def chain_to_jsonable(chain: dict[tuple[int, str], dict]) -> dict[str, int]:
    """{(strike, CE|PE): {oi,...}} -> {"24700|CE": oi} (only entries with OI)."""
    out = {}
    for (strike, otype), rec in chain.items():
        oi = rec.get("oi")
        if oi is not None:
            out[f"{strike}|{otype}"] = int(oi)
    return out


def jsonable_to_chain(obj: dict[str, int]) -> dict[tuple[int, str], int]:
    out = {}
    for key, oi in obj.items():
        strike_s, otype = key.split("|")
        out[(int(strike_s), otype)] = int(oi)
    return out


def our_ts_bounds_per_contract(
    symbol: str, expiry: str
) -> dict[tuple[int, str], tuple[datetime, datetime]]:
    """(first_ts, last_ts) per (strike, type) — detects frozen/dropped strikes
    and strikes that entered the subscription window mid-delta."""
    eng = common.sync_engine()
    try:
        with eng.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT strike, option_type, MIN(ts) AS first_ts, MAX(ts) AS last_ts
                    FROM option_oi_snapshots
                    WHERE symbol = :symbol AND expiry = :expiry
                    GROUP BY strike, option_type
                    """
                ),
                {"symbol": symbol, "expiry": expiry},
            ).mappings().all()
    finally:
        eng.dispose()
    out: dict[tuple[int, str], tuple[datetime, datetime]] = {}
    for r in rows:
        first, last = r["first_ts"], r["last_ts"]
        if getattr(first, "tzinfo", None) is None:
            first = first.replace(tzinfo=timezone.utc)
        if getattr(last, "tzinfo", None) is None:
            last = last.replace(tzinfo=timezone.utc)
        out[(int(r["strike"]), str(r["option_type"]))] = (first, last)
    return out


async def capture(symbol: str) -> None:
    async with httpx.AsyncClient() as api:
        expiry = await common.active_expiry(api, symbol)
    if not expiry:
        raise SystemExit(f"No active expiry for {symbol} — is the feed live?")
    meta, chain, _ = await fetch_sensibull_chain(symbol, expiry)
    snap = {
        "symbol": symbol,
        "expiry": expiry,
        "captured_at_utc": common.now_utc().isoformat(),
        "sensibull_last_updated_at": meta.get("last_updated_at"),
        "underlying_price": meta.get("underlying_price"),
        "lot_size": meta.get("lot_size"),
        "chain": chain_to_jsonable(chain),
    }
    path = common.save_json(snapshot_path(symbol, "t0").name, snap)
    print(f"[oi_delta] captured T0 for {symbol} {expiry}: {len(snap['chain'])} contracts "
          f"@ {snap['sensibull_last_updated_at']} -> {path}")


async def compare(symbol: str) -> list[Finding]:
    t0_path = snapshot_path(symbol, "t0")
    if not t0_path.exists():
        raise SystemExit(f"No T0 snapshot at {t0_path} — run --capture first.")
    with open(t0_path, encoding="utf-8") as f:
        t0 = json.load(f)

    findings: list[Finding] = []
    snapshots: list[SourceSnapshot] = []
    expiry = t0["expiry"]

    meta, chain_now, _ = await fetch_sensibull_chain(symbol, expiry)
    sb_t0 = jsonable_to_chain(t0["chain"])
    sb_t1 = {k: int(v["oi"]) for k, v in chain_now.items() if v.get("oi") is not None}
    common.save_json(snapshot_path(symbol, "t1").name, {
        "symbol": symbol, "expiry": expiry,
        "captured_at_utc": common.now_utc().isoformat(),
        "sensibull_last_updated_at": meta.get("last_updated_at"),
        "underlying_price": meta.get("underlying_price"),
        "chain": chain_to_jsonable(chain_now),
    })

    ts0 = parse_sensibull_ts(t0["sensibull_last_updated_at"])
    ts1 = parse_sensibull_ts(meta.get("last_updated_at"))
    window_min = (ts1 - ts0).total_seconds() / 60.0
    lot = int(t0.get("lot_size") or meta.get("lot_size") or FALLBACK_LOT.get(symbol, 1))

    snapshots.append(SourceSnapshot(
        source=f"sensibull delta window {symbol}", fetched_at_utc=common.now_utc().isoformat(),
        ok=True, extra={"t0": ts0.isoformat(), "t1": ts1.isoformat(),
                        "window_min": round(window_min, 1), "lot_size": lot}))

    if window_min < MIN_WINDOW_MIN:
        findings.append(Finding(
            layer="layer_f", metric="window_length", key=symbol,
            ours=f"{window_min:.1f} min", theirs=f">= {MIN_WINDOW_MIN} min", diff=None,
            tol="advisory", verdict="INFO",
            note="Short window: real deltas may not dwarf edge skew; rerun later for confidence."))

    # Ours over the exact same window.
    async with httpx.AsyncClient() as api:
        ours = await common.api_get(api, "/api/oi-change", {
            "symbol": symbol, "expiry": expiry,
            "from_ts": ts0.isoformat(), "to_ts": ts1.isoformat()})
    common.save_json(f"raw_ours_oi_delta_{symbol}.json", ours)

    ts_bounds = our_ts_bounds_per_contract(symbol, expiry)
    stale_cutoff = ts1 - timedelta(seconds=STALE_AFTER_S)
    late_baseline_cutoff = ts0 + timedelta(seconds=LATE_BASELINE_S)

    our_ce_d = our_pe_d = sb_ce_d = sb_pe_d = 0
    compared = skipped_missing = late_baseline = negligible = 0
    material_total = material_agree = 0
    for row in ours.get("rows", []):
        strike = int(row["strike"])
        for otype, field in (("CE", "call_oi_change"), ("PE", "put_oi_change")):
            key = (strike, otype)
            if key not in sb_t0 or key not in sb_t1:
                skipped_missing += 1
                continue
            d_sb = sb_t1[key] - sb_t0[key]
            d_ours = int(row[field])
            bounds = ts_bounds.get(key)
            if bounds is not None and bounds[0] > late_baseline_cutoff:
                # Strike entered our subscription window mid-delta: our change is
                # "since its first row", Sensibull's is over the full window —
                # not comparable, report informationally and keep out of totals.
                late_baseline += 1
                findings.append(Finding(
                    layer="layer_f", metric="oi_change_vs_sensibull", key=f"{strike}{otype}",
                    ours=d_ours, theirs=d_sb, diff=None, tol="n/a", verdict="INFO",
                    ts_theirs=ts1.isoformat(),
                    note=f"no pre-window baseline (first row {bounds[0].isoformat()}) — "
                         "strike entered subscription window mid-delta"))
                continue
            compared += 1
            if otype == "CE":
                our_ce_d += d_ours
                sb_ce_d += d_sb
            else:
                our_pe_d += d_ours
                sb_pe_d += d_sb
            if abs(d_sb) >= MATERIAL_LOTS * lot:
                material_total += 1
                if (d_ours > 0) == (d_sb > 0) and d_ours != 0:
                    material_agree += 1
            if abs(d_sb) <= NEGLIGIBLE_LOTS * lot and abs(d_ours) <= NEGLIGIBLE_LOTS * lot:
                negligible += 1
                continue
            level = max(abs(sb_t1[key]), abs(sb_t0[key]), 1)
            tol = max(PER_STRIKE_PCT / 100.0 * abs(d_sb),
                      PER_STRIKE_LEVEL_PCT / 100.0 * level,
                      PER_STRIKE_LOT_FLOOR * lot)
            diff = abs(d_ours - d_sb)
            if diff <= tol:
                verdict = "PASS"
            elif bounds is not None and bounds[1] < stale_cutoff:
                verdict = "STALE"
            else:
                verdict = "FAIL"
            findings.append(Finding(
                layer="layer_f", metric="oi_change_vs_sensibull", key=f"{strike}{otype}",
                ours=d_ours, theirs=d_sb, diff=diff,
                tol=f"max({PER_STRIKE_PCT}%|Δ|, {PER_STRIKE_LEVEL_PCT}% level, {PER_STRIKE_LOT_FLOOR} lots)",
                verdict=verdict, ts_ours=ours.get("asof"), ts_theirs=ts1.isoformat(),
                note=("strike frozen in our window (dropped from subscription)"
                      if verdict == "STALE" else "")))

    if negligible:
        findings.append(Finding(
            layer="layer_f", metric="oi_change_negligible", key=symbol,
            ours=negligible, theirs=None, diff=None, tol=f"|Δ| <= {NEGLIGIBLE_LOTS} lot both sides",
            verdict="PASS", note="contracts where neither side saw a material change"))
    if skipped_missing or late_baseline:
        findings.append(Finding(
            layer="layer_f", metric="oi_change_coverage", key=symbol,
            ours=compared, theirs=compared + skipped_missing + late_baseline,
            diff=skipped_missing + late_baseline,
            tol="n/a", verdict="INFO",
            note=f"excluded: {skipped_missing} absent from a Sensibull snapshot, "
                 f"{late_baseline} entered our window mid-delta (no baseline)"))

    for name, o, t, side_total in (
            ("total_call_oi_change", our_ce_d, sb_ce_d, sum(v for (s, ot), v in sb_t1.items() if ot == "CE")),
            ("total_put_oi_change", our_pe_d, sb_pe_d, sum(v for (s, ot), v in sb_t1.items() if ot == "PE"))):
        tol = max(TOTAL_PCT / 100.0 * abs(t), TOTAL_LEVEL_PCT / 100.0 * max(side_total, 1))
        diff = abs(o - t)
        findings.append(Finding(
            layer="layer_f", metric=f"{name}_vs_sensibull", key=f"{symbol} ({compared} contracts)",
            ours=o, theirs=t, diff=diff,
            tol=f"max({TOTAL_PCT}%|Δtotal|, {TOTAL_LEVEL_PCT}% of side OI)",
            verdict="PASS" if diff <= tol else "FAIL",
            note=f"window {window_min:.1f} min ({ts0.isoformat()} -> {ts1.isoformat()})"))

    if material_total:
        findings.append(Finding(
            layer="layer_f", metric="oi_change_direction_agreement", key=symbol,
            ours=f"{material_agree}/{material_total}", theirs="all agree",
            diff=material_total - material_agree, tol=f"|Δsb| >= {MATERIAL_LOTS} lots",
            verdict="INFO",
            note="sign agreement on materially-moving strikes (context stat, not pass/fail)"))

    common.write_layer_output(f"layer_f_oi_delta_{symbol}", findings, snapshots, {
        "symbol": symbol, "expiry": expiry, "window_min": round(window_min, 1),
        "t0": ts0.isoformat(), "t1": ts1.isoformat(), "lot_size": lot,
        "compared": compared})
    return findings


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="NIFTY")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--capture", action="store_true", help="save the T0 Sensibull snapshot")
    mode.add_argument("--compare", action="store_true", help="fetch T1, diff, compare vs /api/oi-change")
    args = ap.parse_args()
    sym = args.symbol.upper()
    if args.capture:
        asyncio.run(capture(sym))
    elif args.compare:
        asyncio.run(compare(sym))
    else:
        # auto: capture if no T0 yet, else compare
        if snapshot_path(sym, "t0").exists():
            asyncio.run(compare(sym))
        else:
            asyncio.run(capture(sym))
