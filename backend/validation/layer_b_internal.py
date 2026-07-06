"""Layer B — internal consistency: independent recompute of the API math.

Recomputes /api/oi-change (several timeframes + one explicit window) and
/api/oi-timeseries straight from option_oi_snapshots using a *different* SQL
formulation (ROW_NUMBER window functions / epoch-floor bucketing instead of
DISTINCT ON / time_bucket), then diffs against the live API responses.
Same database -> any non-exact difference is an engine bug.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime, timedelta, timezone

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.core.time_utils import IST, market_open_today, parse_timeframe

from . import common
from .common import Finding, SourceSnapshot

TIMEFRAMES = ["5m", "15m", "1h", "full_day"]
SUBMINUTE = ["15s", "30s"]

_NOW_SQL = text(
    """
    SELECT strike, option_type, oi, ltp, ts FROM (
        SELECT strike, option_type, oi, ltp, ts,
               ROW_NUMBER() OVER (PARTITION BY strike, option_type ORDER BY ts DESC) rn
        FROM option_oi_snapshots
        WHERE symbol = :symbol AND expiry = :expiry
    ) t WHERE rn = 1
    """
)

_THEN_BEFORE_SQL = text(
    """
    SELECT strike, option_type, oi, ltp, ts FROM (
        SELECT strike, option_type, oi, ltp, ts,
               ROW_NUMBER() OVER (PARTITION BY strike, option_type ORDER BY ts DESC) rn
        FROM option_oi_snapshots
        WHERE symbol = :symbol AND expiry = :expiry AND ts <= :cutoff
    ) t WHERE rn = 1
    """
)

_THEN_AFTER_SQL = text(
    """
    SELECT strike, option_type, oi, ltp, ts FROM (
        SELECT strike, option_type, oi, ltp, ts,
               ROW_NUMBER() OVER (PARTITION BY strike, option_type ORDER BY ts ASC) rn
        FROM option_oi_snapshots
        WHERE symbol = :symbol AND expiry = :expiry AND ts >= :cutoff
    ) t WHERE rn = 1
    """
)

_BOUNDS_SQL = text(
    "SELECT MIN(ts) AS min_ts, MAX(ts) AS max_ts FROM option_oi_snapshots "
    "WHERE symbol = :symbol AND expiry = :expiry"
)

# Independent 1-minute bucketing: epoch-floor instead of time_bucket_gapfill,
# ROW_NUMBER instead of last(), and the carry-forward (locf) is applied in
# Python rather than SQL — a genuinely different mechanism computing the same
# semantics: per bucket, each contract contributes its last-known OI.
_SPARSE_TS_SQL = text(
    """
    SELECT bucket, strike, option_type, oi FROM (
        SELECT to_timestamp(floor(extract(epoch FROM ts) / 60) * 60) AT TIME ZONE 'UTC' AS bucket,
               strike, option_type, oi,
               ROW_NUMBER() OVER (
                   PARTITION BY floor(extract(epoch FROM ts) / 60), strike, option_type
                   ORDER BY ts DESC
               ) rn
        FROM option_oi_snapshots
        WHERE symbol = :symbol AND expiry = :expiry
          AND strike BETWEEN :strike_min AND :strike_max
          AND ts >= :from_ts AND ts <= :to_ts
    ) t WHERE rn = 1
    ORDER BY bucket
    """
)


def _locf_totals(sparse_rows, bucket_keys: list[str]) -> dict[str, tuple[int, int]]:
    """Forward-fill each contract's last-known OI across the given buckets, sum per bucket."""
    per_contract: dict[tuple[int, str], list[tuple[str, int]]] = {}
    for r in sparse_rows:
        key_ist = r["bucket"].replace(tzinfo=timezone.utc).astimezone(IST).isoformat()
        per_contract.setdefault((int(r["strike"]), r["option_type"]), []).append((key_ist, int(r["oi"])))
    totals: dict[str, tuple[int, int]] = {}
    carried: dict[tuple[int, str], int] = {}
    pos: dict[tuple[int, str], int] = {k: 0 for k in per_contract}
    for bk in bucket_keys:
        ce = pe = 0
        for contract, series in per_contract.items():
            i = pos[contract]
            while i < len(series) and series[i][0] <= bk:
                carried[contract] = series[i][1]
                i += 1
            pos[contract] = i
            if contract in carried:
                if contract[1] == "CE":
                    ce += carried[contract]
                else:
                    pe += carried[contract]
        totals[bk] = (ce, pe)
    return totals


def _tzaware(ts) -> datetime:
    if getattr(ts, "tzinfo", None) is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


async def _recompute_oi_change(conn, symbol: str, expiry: str, timeframe: str):
    """Mirror OIChangeEngine._compute semantics with independent SQL."""
    bounds = (await conn.execute(_BOUNDS_SQL, {"symbol": symbol, "expiry": expiry})).mappings().first()
    if not bounds or bounds["max_ts"] is None:
        return None
    anchor = _tzaware(bounds["max_ts"])
    earliest = _tzaware(bounds["min_ts"])

    delta = parse_timeframe(timeframe)
    if delta == "full_day":
        cutoff = market_open_today(anchor.astimezone(IST)).astimezone(timezone.utc)
        then_sql = _THEN_AFTER_SQL
    else:
        cutoff = anchor - delta
        then_sql = _THEN_BEFORE_SQL
        if cutoff < earliest:  # baseline clamp (oi_change.py:356-372)
            cutoff = earliest
            then_sql = _THEN_AFTER_SQL

    params = {"symbol": symbol, "expiry": expiry}
    now_rows = (await conn.execute(_NOW_SQL, params)).mappings().all()
    then_rows = (await conn.execute(then_sql, {**params, "cutoff": cutoff})).mappings().all()

    # Mirror the engine's session floor (oi_change.py): the now-side only keeps
    # strikes with data in the anchor day's session — applied in Python here to
    # stay an independent mechanism.
    session_floor = market_open_today(anchor.astimezone(IST)).astimezone(timezone.utc)
    now_rows = [r for r in now_rows if _tzaware(r["ts"]) >= session_floor]

    now_map = {(int(r["strike"]), r["option_type"]): r for r in now_rows}
    then_map = {(int(r["strike"]), r["option_type"]): r for r in then_rows}
    strikes = sorted({k[0] for k in now_map})

    rows = {}
    total_ce = total_pe = 0
    for s in strikes:
        ce_n, pe_n = now_map.get((s, "CE")), now_map.get((s, "PE"))
        ce_t, pe_t = then_map.get((s, "CE")), then_map.get((s, "PE"))
        ce_chg = (int(ce_n["oi"]) if ce_n else 0) - (int(ce_t["oi"]) if ce_t else 0)
        pe_chg = (int(pe_n["oi"]) if pe_n else 0) - (int(pe_t["oi"]) if pe_t else 0)
        total_ce += ce_chg
        total_pe += pe_chg
        rows[s] = {
            "call_oi": int(ce_n["oi"]) if ce_n else 0,
            "put_oi": int(pe_n["oi"]) if pe_n else 0,
            "call_oi_change": ce_chg,
            "put_oi_change": pe_chg,
            "call_ltp": float(ce_n["ltp"]) if ce_n and ce_n["ltp"] is not None else None,
            "put_ltp": float(pe_n["ltp"]) if pe_n and pe_n["ltp"] is not None else None,
        }
    return {"anchor": anchor, "rows": rows, "total_ce": total_ce, "total_pe": total_pe,
            "cutoff": cutoff, "clamped": then_sql is _THEN_AFTER_SQL and timeframe != "full_day"}


async def run(symbol: str) -> list[Finding]:
    findings: list[Finding] = []
    snapshots: list[SourceSnapshot] = []
    engine = create_async_engine(settings.db_url, echo=False)

    try:
        async with httpx.AsyncClient() as client:
            expiry = await common.active_expiry(client, symbol)
            if expiry is None:
                findings.append(Finding(
                    layer="layer_b", metric="expiry", key=symbol, ours=None, theirs=None,
                    diff=None, tol="n/a", verdict="SOURCE_UNAVAILABLE", note="no expiries"))
                common.write_layer_output(f"layer_b_{symbol}", findings, snapshots)
                return findings

            # ---------- /api/oi-change per timeframe ----------
            async with engine.connect() as conn:
                for tf in TIMEFRAMES:
                    # Retry until our recompute anchor matches the API's anchor
                    # (a flush between the two reads moves MAX(ts)).
                    for _attempt in range(4):
                        mine = await _recompute_oi_change(conn, symbol, expiry, tf)
                        api = await common.api_get(
                            client, "/api/oi-change",
                            {"timeframe": tf, "expiry": expiry, "symbol": symbol})
                        if mine is None:
                            break
                        api_asof = api.get("asof")
                        mine_asof = mine["anchor"].astimezone(IST).isoformat(timespec="seconds")
                        # API 'asof' is second-precision IST of the anchor
                        if api_asof and api_asof[:19] == mine_asof[:19]:
                            break
                    if mine is None:
                        findings.append(Finding(
                            layer="layer_b", metric="oi_change", key=tf, ours=None, theirs=None,
                            diff=None, tol="n/a", verdict="SOURCE_UNAVAILABLE", note="no DB rows"))
                        continue

                    common.save_json(f"fixture_oi_change_{tf}_{symbol}.json", api, common.FIXTURE_DIR)
                    api_rows = {int(r["strike"]): r for r in api.get("rows", [])}
                    mismatches = 0
                    checked = 0
                    for s, m in mine["rows"].items():
                        a = api_rows.get(s)
                        if a is None:
                            mismatches += 1
                            findings.append(Finding(
                                layer="layer_b", metric=f"oi_change[{tf}]", key=f"{s}",
                                ours="present", theirs="missing", diff=None, tol="exact",
                                verdict="FAIL", note="strike missing from API response"))
                            continue
                        for fld in ("call_oi", "put_oi", "call_oi_change", "put_oi_change"):
                            checked += 1
                            if int(a[fld]) != int(m[fld]):
                                mismatches += 1
                                findings.append(Finding(
                                    layer="layer_b", metric=f"oi_change[{tf}].{fld}", key=f"{s}",
                                    ours=m[fld], theirs=a[fld], diff=int(a[fld]) - int(m[fld]),
                                    tol="exact", verdict="FAIL",
                                    note=f"recompute vs API (anchor={mine['anchor'].isoformat()})"))
                    extra_api = set(api_rows) - set(mine["rows"])
                    for s in extra_api:
                        mismatches += 1
                        findings.append(Finding(
                            layer="layer_b", metric=f"oi_change[{tf}]", key=f"{s}",
                            ours="missing", theirs="present", diff=None, tol="exact",
                            verdict="FAIL", note="strike in API but not in recompute"))

                    for fld, mval in (("total_call_oi_change", mine["total_ce"]),
                                      ("total_put_oi_change", mine["total_pe"])):
                        checked += 1
                        if int(api[fld]) != int(mval):
                            mismatches += 1
                            findings.append(Finding(
                                layer="layer_b", metric=f"oi_change[{tf}].{fld}", key="totals",
                                ours=mval, theirs=api[fld], diff=int(api[fld]) - int(mval),
                                tol="exact", verdict="FAIL", note=""))
                    if mismatches == 0:
                        findings.append(Finding(
                            layer="layer_b", metric=f"oi_change[{tf}]", key="all",
                            ours=f"{len(mine['rows'])} strikes / {checked} values",
                            theirs="identical", diff=0, tol="exact", verdict="PASS",
                            ts_ours=mine["anchor"].astimezone(IST).isoformat(),
                            note=("baseline clamped to earliest snapshot" if mine["clamped"] else "")))

                # ---------- explicit from_ts/to_ts window ----------
                bounds = (await conn.execute(_BOUNDS_SQL, {"symbol": symbol, "expiry": expiry})).mappings().first()
                if bounds and bounds["max_ts"] is not None:
                    hi = _tzaware(bounds["max_ts"])
                    lo = _tzaware(bounds["min_ts"])
                    mid = lo + (hi - lo) / 2
                    from_ist = mid.astimezone(IST).isoformat(timespec="seconds")
                    to_ist = hi.astimezone(IST).isoformat(timespec="seconds")
                    api = await common.api_get(client, "/api/oi-change", {
                        "from_ts": from_ist, "to_ts": to_ist, "expiry": expiry, "symbol": symbol})
                    common.save_json(f"fixture_oi_change_range_{symbol}.json", api, common.FIXTURE_DIR)

                    # recompute: snapshot(to) - snapshot(from), AT_OR_BEFORE both sides
                    p = {"symbol": symbol, "expiry": expiry}
                    now_rows = (await conn.execute(
                        _THEN_BEFORE_SQL, {**p, "cutoff": datetime.fromisoformat(to_ist)})).mappings().all()
                    then_rows = (await conn.execute(
                        _THEN_BEFORE_SQL, {**p, "cutoff": datetime.fromisoformat(from_ist)})).mappings().all()
                    # Mirror the engine's session floor on the now side (open of
                    # the window's upper-bound day).
                    range_floor = market_open_today(
                        datetime.fromisoformat(to_ist).astimezone(IST)).astimezone(timezone.utc)
                    now_rows = [r for r in now_rows if _tzaware(r["ts"]) >= range_floor]
                    now_map = {(int(r["strike"]), r["option_type"]): int(r["oi"]) for r in now_rows}
                    then_map = {(int(r["strike"]), r["option_type"]): int(r["oi"]) for r in then_rows}
                    # Mirror the engine's range baseline clamp: strikes first seen
                    # after from_ts fall back to their earliest stored snapshot.
                    missing_then = [k for k in now_map if k not in then_map]
                    if missing_then:
                        after_rows = (await conn.execute(
                            _THEN_AFTER_SQL, {**p, "cutoff": datetime.fromisoformat(from_ist)})).mappings().all()
                        after_map = {(int(r["strike"]), r["option_type"]): int(r["oi"]) for r in after_rows}
                        for k in missing_then:
                            if k in after_map:
                                then_map[k] = after_map[k]
                    api_rows = {int(r["strike"]): r for r in api.get("rows", [])}
                    mismatches = 0
                    for s in sorted({k[0] for k in now_map}):
                        a = api_rows.get(s)
                        if a is None:
                            mismatches += 1
                            continue
                        ce = now_map.get((s, "CE"), 0) - then_map.get((s, "CE"), 0)
                        pe = now_map.get((s, "PE"), 0) - then_map.get((s, "PE"), 0)
                        if int(a["call_oi_change"]) != ce or int(a["put_oi_change"]) != pe:
                            mismatches += 1
                            findings.append(Finding(
                                layer="layer_b", metric="oi_change[range]", key=f"{s}",
                                ours=(ce, pe), theirs=(a["call_oi_change"], a["put_oi_change"]),
                                diff=None, tol="exact", verdict="FAIL",
                                note=f"window {from_ist}..{to_ist}"))
                    findings.append(Finding(
                        layer="layer_b", metric="oi_change[range]", key="all",
                        ours=f"{len(now_map) // 2} strikes", theirs="identical" if mismatches == 0 else f"{mismatches} mismatches",
                        diff=mismatches, tol="exact",
                        verdict="PASS" if mismatches == 0 else "FAIL",
                        note=f"window {from_ist}..{to_ist}"))

                # ---------- /api/oi-timeseries (1m) ----------
                health = await common.api_get(client, "/api/health")
                spot = health.get("latest_spot")
                step = 100 if symbol.upper() == "SENSEX" else 50
                atm = common.js_round(spot / step) * step if spot else None
                if atm:
                    smin, smax = atm - 10 * step, atm + 10 * step
                    for _attempt in range(4):
                        api = await common.api_get(client, "/api/oi-timeseries", {
                            "symbol": symbol, "expiry": expiry,
                            "strike_min": smin, "strike_max": smax, "bucket": "1m"})
                        b = (await conn.execute(_BOUNDS_SQL, {"symbol": symbol, "expiry": expiry})).mappings().first()
                        anchor = _tzaware(b["max_ts"])
                        from_utc = market_open_today(anchor.astimezone(IST)).astimezone(timezone.utc)
                        sparse = (await conn.execute(_SPARSE_TS_SQL, {
                            "symbol": symbol, "expiry": expiry,
                            "strike_min": smin, "strike_max": smax,
                            "from_ts": from_utc, "to_ts": anchor})).mappings().all()
                        api_pts = {p["ts"]: (int(p["total_call_oi"]), int(p["total_put_oi"]))
                                   for p in api.get("points", [])}
                        anchor_key = anchor.astimezone(IST).isoformat()[:16]
                        if api_pts and max(api_pts)[:16] == anchor_key[:16]:
                            break
                    common.save_json(f"fixture_oi_timeseries_{symbol}.json", api, common.FIXTURE_DIR)

                    bucket_keys = sorted(api_pts)
                    # continuity: buckets must be a gapless 1-minute ladder
                    gaps = []
                    for i in range(1, len(bucket_keys)):
                        prev = datetime.fromisoformat(bucket_keys[i - 1])
                        cur = datetime.fromisoformat(bucket_keys[i])
                        if (cur - prev) != timedelta(minutes=1):
                            gaps.append((bucket_keys[i - 1], bucket_keys[i]))
                    findings.append(Finding(
                        layer="layer_b", metric="oi_timeseries[1m].continuity", key=f"{len(bucket_keys)} buckets",
                        ours=f"{len(gaps)} gaps", theirs="0 gaps", diff=gaps[:3] or None,
                        tol="gapless after first data", verdict="PASS" if not gaps else "FAIL",
                        note="carry-forward (locf) must emit every minute after data start"))

                    mine_pts = _locf_totals(sparse, bucket_keys)
                    mismatches = [k for k in bucket_keys if api_pts.get(k) != mine_pts.get(k)]
                    for k in sorted(mismatches)[:20]:
                        findings.append(Finding(
                            layer="layer_b", metric="oi_timeseries[1m]", key=k,
                            ours=mine_pts.get(k), theirs=api_pts.get(k), diff=None,
                            tol="exact", verdict="FAIL", note=f"strikes {smin}-{smax}"))
                    if not mismatches:
                        findings.append(Finding(
                            layer="layer_b", metric="oi_timeseries[1m]", key="all",
                            ours=f"{len(bucket_keys)} buckets (python locf)", theirs="identical", diff=0,
                            tol="exact", verdict="PASS", note=f"strikes {smin}-{smax}"))

                # ---------- PCR / KPI totals from latest snapshot ----------
                def _floored_totals(rows):
                    # Mirror the API's session floor (open of the latest data day).
                    if not rows:
                        return 0, 0
                    hi = max(_tzaware(r["ts"]) for r in rows)
                    fl = market_open_today(hi.astimezone(IST)).astimezone(timezone.utc)
                    live = [r for r in rows if _tzaware(r["ts"]) >= fl]
                    return (sum(int(r["oi"]) for r in live if r["option_type"] == "CE"),
                            sum(int(r["oi"]) for r in live if r["option_type"] == "PE"))

                now_rows = (await conn.execute(_NOW_SQL, {"symbol": symbol, "expiry": expiry})).mappings().all()
                ce_tot, pe_tot = _floored_totals(now_rows)
                oc = await common.api_get(client, "/api/option-chain", {"symbol": symbol, "expiry": expiry})
                common.save_json(f"fixture_option_chain_{symbol}.json", oc, common.FIXTURE_DIR)
                api_ce = sum(int(r["call_oi"]) for r in oc.get("rows", []))
                api_pe = sum(int(r["put_oi"]) for r in oc.get("rows", []))
                # note: a flush between the two reads can move one side; tolerate one retry
                if (api_ce, api_pe) != (ce_tot, pe_tot):
                    now_rows = (await conn.execute(_NOW_SQL, {"symbol": symbol, "expiry": expiry})).mappings().all()
                    ce_tot, pe_tot = _floored_totals(now_rows)
                verdict = "PASS" if (api_ce, api_pe) == (ce_tot, pe_tot) else "FAIL"
                findings.append(Finding(
                    layer="layer_b", metric="pcr_totals", key=symbol,
                    ours={"ce": ce_tot, "pe": pe_tot, "pcr": round(pe_tot / ce_tot, 4) if ce_tot else None},
                    theirs={"ce": api_ce, "pe": api_pe, "pcr": round(api_pe / api_ce, 4) if api_ce else None},
                    diff=None, tol="exact (1 retry for flush race)", verdict=verdict, note=""))

                # ---------- sub-minute timeframes: informational only ----------
                for tf in SUBMINUTE:
                    api = await common.api_get(client, "/api/oi-change", {
                        "timeframe": tf, "expiry": expiry, "symbol": symbol})
                    findings.append(Finding(
                        layer="layer_b", metric=f"oi_change[{tf}]", key="hold-last",
                        ours=None,
                        theirs={"total_ce": api["total_call_oi_change"], "total_pe": api["total_put_oi_change"]},
                        diff=None, tol="n/a", verdict="SKIP",
                        note="sub-minute uses hold-last heuristic over ~1/min OI feed — "
                             "ill-defined against raw windows by design (oi_change.py:396-443)"))
    finally:
        await engine.dispose()

    common.write_layer_output(f"layer_b_{symbol}", findings, snapshots, {"symbol": symbol, "expiry": expiry})
    return findings


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="NIFTY")
    args = ap.parse_args()
    asyncio.run(run(args.symbol.upper()))
