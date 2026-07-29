"""Layer A — XTS REST truth vs DB ingestion vs /api/option-chain.

Fetches live quotes (1501 touchline + 1510 OI) for every token the feed
subscribed today, straight from the broker REST API (reusing the stored
session token), and compares them against the latest DB snapshot rows and
the backend's /api/option-chain response.
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
from datetime import timezone

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.core.time_utils import IST

from . import common
from .common import Finding, SourceSnapshot

# Live tolerances (market open). After-hours everything must match exactly.
OI_TOL_PCT_LIVE = 2.0
LTP_TOL_PCT_LIVE = 0.5
LTP_TOL_ABS_LIVE = 0.5
SPOT_TOL_PCT = 0.1
VOL_GROWTH_INFO_PCT = 5.0
STALE_AFTER_S = 90.0

_LATEST_PER_TOKEN_SQL = text(
    """
    SELECT DISTINCT ON (token) token, ts, strike, option_type, oi, ltp, volume, underlying
    FROM option_oi_snapshots
    WHERE symbol = :symbol AND expiry = :expiry
    ORDER BY token, ts DESC
    """
)


async def run(symbol: str, round_id: int = 1) -> list[Finding]:
    findings: list[Finding] = []
    snapshots: list[SourceSnapshot] = []
    state = common.market_state()

    token, uid, issued = common.load_xts_token()
    uni = common.todays_universe(symbol)
    if not uni:
        findings.append(Finding(
            layer="layer_a", metric="universe", key=symbol, ours=0, theirs=None,
            diff=None, tol="n/a", verdict="SOURCE_UNAVAILABLE",
            note="No rows in option_oi_snapshots for today — feed not ingesting?",
        ))
        common.write_layer_output(f"layer_a_round{round_id}_{symbol}", findings, snapshots)
        return findings

    expiry = uni[0].expiry
    seg = common.option_segment(symbol)
    instruments = [
        {"exchangeSegment": seg, "exchangeInstrumentID": int(u.token)} for u in uni
    ]
    by_token = {u.token: u for u in uni}

    spot_instr = None
    if symbol.upper() == "NIFTY":
        spot_instr = {"exchangeSegment": common.SEG_NSECM, "exchangeInstrumentID": 26000}

    engine = create_async_engine(settings.db_url, echo=False)
    try:
        async with httpx.AsyncClient() as client:
            if not await common.check_token(client, token):
                raise common.TokenInvalid(
                    "Stored XTS token failed the read check — aborting Layer A "
                    "(do NOT login; let the running backend self-heal)."
                )

            async def db_latest():
                async with engine.connect() as conn:
                    rows = (
                        await conn.execute(
                            _LATEST_PER_TOKEN_SQL, {"symbol": symbol, "expiry": expiry}
                        )
                    ).mappings().all()
                return {str(r["token"]): dict(r) for r in rows}

            t0 = common.now_utc()
            # DB snapshot FIRST, then broker REST: guarantees REST is the fresher
            # source, so monotone checks (volume) compare in a known direction.
            db_result = await db_latest()
            results = await asyncio.gather(
                common.fetch_xts_quotes(client, token, instruments, common.MSG_TOUCHLINE),
                common.fetch_xts_quotes(client, token, instruments, common.MSG_OPENINTEREST),
                common.fetch_xts_quotes(client, token, [spot_instr], common.MSG_TOUCHLINE)
                if spot_instr else asyncio.sleep(0, result={}),
                asyncio.sleep(0, result=db_result),
                common.api_get(client, "/api/option-chain", {"symbol": symbol, "expiry": expiry}),
                common.api_get(client, "/api/spot"),
                common.api_get(client, "/api/health"),
                return_exceptions=True,
            )
            t1 = common.now_utc()
            names = ["xts_1501", "xts_1510", "xts_spot", "db_latest", "api_option_chain", "api_spot", "api_health"]
            data: dict[str, object] = {}
            for name, res in zip(names, results):
                ok = not isinstance(res, BaseException)
                path = None
                if ok:
                    path = str(common.save_json(
                        f"raw_{name}_round{round_id}_{symbol}.json",
                        res if not isinstance(res, dict) or name not in ("xts_1501", "xts_1510", "xts_spot")
                        else {str(k): v for k, v in res.items()},
                    ))
                snapshots.append(SourceSnapshot(
                    source=name, fetched_at_utc=t1.isoformat(), ok=ok,
                    payload_path=path, error=None if ok else repr(res),
                ))
                data[name] = res if ok else None

            fetch_window_s = (t1 - t0).total_seconds()
    finally:
        await engine.dispose()

    q1501 = data["xts_1501"] or {}
    q1510 = data["xts_1510"] or {}
    qspot = data["xts_spot"] or {}
    db = data["db_latest"] or {}
    oc = data["api_option_chain"]
    api_spot = data["api_spot"]
    health = data["api_health"]

    oc_by_strike = {}
    if oc:
        for r in oc.get("rows", []):
            oc_by_strike[int(r["strike"])] = r

    oi_ratios: list[float] = []

    for tok_str, u in sorted(by_token.items(), key=lambda kv: (kv[1].strike, kv[1].option_type)):
        iid = int(tok_str)
        key = f"{u.strike}{u.option_type}"
        dbrow = db.get(tok_str)
        db_ts = dbrow["ts"] if dbrow else None
        if db_ts is not None and getattr(db_ts, "tzinfo", None) is None:
            db_ts = db_ts.replace(tzinfo=timezone.utc)
        db_age = (common.now_utc() - db_ts).total_seconds() if db_ts else None
        stale = state == "live" and db_age is not None and db_age > STALE_AFTER_S

        # ---- OI: REST 1510 vs DB ----
        rest_oi = common.quote_oi(q1510.get(iid, {})) if iid in q1510 else None
        db_oi = int(dbrow["oi"]) if dbrow else None
        if rest_oi is not None and db_oi is not None:
            if rest_oi > 0 and db_oi > 0:
                oi_ratios.append(rest_oi / db_oi)
            if rest_oi == 0 and db_oi > 0:
                verdict, note = "BY_DESIGN", "zero-suppression: feed drops 0-OI frames (ws_client.py)"
                d = None
            else:
                p = common.pct_diff(db_oi, rest_oi)
                tol = OI_TOL_PCT_LIVE if state == "live" else 0.0
                verdict = "PASS" if p <= tol else ("STALE" if stale else "FAIL")
                note = f"db_age={db_age:.0f}s" if db_age is not None else ""
                d = f"{p:.2f}%"
            findings.append(Finding(
                layer="layer_a", metric="oi_rest_vs_db", key=key,
                ours=db_oi, theirs=rest_oi, diff=d,
                tol=f"±{OI_TOL_PCT_LIVE}% live / exact closed", verdict=verdict,
                ts_ours=common.iso(db_ts), ts_theirs=None, note=note,
            ))

        # ---- OI: REST 1510 vs /api/option-chain ----
        ocr = oc_by_strike.get(u.strike)
        if rest_oi is not None and ocr is not None:
            api_oi = int(ocr["call_oi" if u.option_type == "CE" else "put_oi"])
            p = common.pct_diff(api_oi, rest_oi)
            tol = OI_TOL_PCT_LIVE if state == "live" else 0.0
            verdict = "PASS" if p <= tol else ("STALE" if stale else "FAIL")
            findings.append(Finding(
                layer="layer_a", metric="oi_rest_vs_api", key=key,
                ours=api_oi, theirs=rest_oi, diff=f"{p:.2f}%",
                tol=f"±{OI_TOL_PCT_LIVE}% live / exact closed", verdict=verdict,
                ts_ours=oc.get("asof") if oc else None, note="",
            ))

        # ---- LTP: REST 1501 vs DB ----
        if iid in q1501 and dbrow is not None:
            rest_ltp, rest_vol = common.quote_ltp_volume(q1501[iid])
            db_ltp = float(dbrow["ltp"]) if dbrow["ltp"] is not None else None
            if rest_ltp is not None and db_ltp is not None:
                diff_abs = abs(db_ltp - rest_ltp)
                diff_pct = common.pct_diff(db_ltp, rest_ltp)
                ok = diff_abs <= LTP_TOL_ABS_LIVE or diff_pct <= LTP_TOL_PCT_LIVE
                if state != "live":
                    ok = diff_abs < 0.005
                verdict = "PASS" if ok else ("STALE" if stale else "FAIL")
                findings.append(Finding(
                    layer="layer_a", metric="ltp_rest_vs_db", key=key,
                    ours=db_ltp, theirs=rest_ltp, diff=f"{diff_abs:.2f} ({diff_pct:.2f}%)",
                    tol=f"±{LTP_TOL_ABS_LIVE} or ±{LTP_TOL_PCT_LIVE}% live", verdict=verdict,
                    ts_ours=common.iso(db_ts),
                    note=f"db_age={db_age:.0f}s" if db_age is not None else "",
                ))
            # ---- Volume: REST vs DB (day-cumulative, so nearly monotone) ----
            # The broker's REST quote cache can lag its own live socket stream
            # by a second or two, so REST may trail the DB by a handful of
            # trades; only a material regression indicates an integrity issue.
            db_vol = int(dbrow["volume"]) if dbrow["volume"] is not None else None
            if rest_vol is not None and db_vol is not None:
                dev = common.pct_diff(rest_vol, max(db_vol, 1))
                if rest_vol < db_vol:
                    if dev <= 0.5:
                        verdict = "PASS"
                        note = (f"REST trails stream by {db_vol - rest_vol} ({dev:.3f}%) — "
                                "broker REST-cache lag, within tolerance")
                    else:
                        verdict = "FAIL"
                        note = f"volume regressed {dev:.2f}% (REST < DB) — integrity issue"
                else:
                    verdict = "INFO" if dev > VOL_GROWTH_INFO_PCT else "PASS"
                    note = f"growth since flush {dev:.2f}% (db_age={db_age:.0f}s)" if db_age is not None else ""
                findings.append(Finding(
                    layer="layer_a", metric="volume_rest_vs_db", key=key,
                    ours=db_vol, theirs=rest_vol, diff=rest_vol - db_vol,
                    tol="REST >= DB, or trail <= 0.5% (REST-cache lag)", verdict=verdict,
                    ts_ours=common.iso(db_ts), note=note,
                ))

    # ---- Unit sanity: consistent scale factor would indicate a units bug ----
    if oi_ratios:
        med = statistics.median(oi_ratios)
        findings.append(Finding(
            layer="layer_a", metric="oi_unit_sanity", key=symbol,
            ours=1.0, theirs=round(med, 4), diff=round(abs(med - 1.0), 4),
            tol="median REST/DB OI ratio ≈ 1 (0.98–1.02)",
            verdict="PASS" if 0.98 <= med <= 1.02 else "FAIL",
            note="a ratio near a lot-size multiple would mean contracts-vs-quantity units bug",
        ))

    # ---- Spot: XTS REST vs /api/spot vs /api/health ----
    if qspot and api_spot:
        rest_spot, _ = common.quote_ltp_volume(next(iter(qspot.values())))
        ours_spot = api_spot.get("spot")
        health_spot = (health or {}).get("latest_spot")
        if rest_spot is not None and ours_spot is not None:
            p = common.pct_diff(ours_spot, rest_spot)
            findings.append(Finding(
                layer="layer_a", metric="spot_rest_vs_api", key=symbol,
                ours=ours_spot, theirs=rest_spot, diff=f"{p:.3f}%",
                tol=f"±{SPOT_TOL_PCT}%", verdict="PASS" if p <= SPOT_TOL_PCT else "FAIL",
                ts_ours=api_spot.get("asof"), note=f"health.latest_spot={health_spot}",
            ))

    meta = {
        "symbol": symbol, "expiry": expiry, "round": round_id,
        "tokens": len(uni), "fetch_window_s": round(fetch_window_s, 2),
        "xts_user": uid, "token_issued_at": issued.isoformat(),
    }
    common.write_layer_output(f"layer_a_round{round_id}_{symbol}", findings, snapshots, meta)
    return findings


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--round", type=int, default=1)
    args = ap.parse_args()
    asyncio.run(run(args.symbol.upper(), args.round))
