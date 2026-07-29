"""Layer E — official exchange cross-check.

NIFTY: drives the backend's own verify endpoints (which fetch NSE India's
option-chain JSON server-side with a browser-like session) and normalises
their per-strike results into finding records.
SENSEX: best-effort probe of BSE India's API (heavy anti-bot — an expected
block is recorded as a limitation, not a failure).
"""
from __future__ import annotations

import argparse
import asyncio

import httpx

from . import common
from .common import Finding, SourceSnapshot

BSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com",
}


async def run(symbol: str, round_id: int = 1) -> list[Finding]:
    findings: list[Finding] = []
    snapshots: list[SourceSnapshot] = []

    async with httpx.AsyncClient() as client:
        if symbol.upper() == "NIFTY":
            # ---- spot vs NSE/Yahoo (existing endpoint, ±30 pt tolerance) ----
            try:
                spot_chk = await common.api_get(client, "/api/verify/nifty-cross-check")
                p = common.save_json(f"raw_nse_spot_check_round{round_id}.json", spot_chk)
                snapshots.append(SourceSnapshot(
                    source="/api/verify/nifty-cross-check",
                    fetched_at_utc=common.now_utc().isoformat(), ok=True, payload_path=str(p)))
                findings.append(Finding(
                    layer="layer_e", metric="spot_vs_public", key="NIFTY",
                    ours=spot_chk.get("our_spot"),
                    theirs=spot_chk.get("reference_last"),
                    diff=spot_chk.get("diff_points"),
                    tol=f"±{spot_chk.get('tolerance_points', 30)} pts",
                    verdict="PASS" if spot_chk.get("aligned") else (
                        "SOURCE_UNAVAILABLE" if spot_chk.get("reference_last") is None else "FAIL"),
                    note=f"reference source: {spot_chk.get('reference_source')}"))
            except Exception as e:
                findings.append(Finding(
                    layer="layer_e", metric="spot_vs_public", key="NIFTY",
                    ours=None, theirs=None, diff=None, tol="±30 pts",
                    verdict="SOURCE_UNAVAILABLE", note=repr(e)))

            # ---- per-strike OI/LTP vs NSE option chain ----
            try:
                chk = await common.api_get(client, "/api/verify/option-chain", {"symbol": symbol})
                p = common.save_json(f"raw_nse_option_chain_check_round{round_id}.json", chk)
                snapshots.append(SourceSnapshot(
                    source="/api/verify/option-chain",
                    fetched_at_utc=common.now_utc().isoformat(),
                    ok=chk.get("nse_fetch_error") is None, payload_path=str(p),
                    error=chk.get("nse_fetch_error")))
                if chk.get("nse_fetch_error"):
                    findings.append(Finding(
                        layer="layer_e", metric="oi_vs_nse", key=symbol,
                        ours=None, theirs=None, diff=None, tol="±10%",
                        verdict="SOURCE_UNAVAILABLE", note=chk["nse_fetch_error"]))
                else:
                    summ = chk.get("summary", {})
                    for row in chk.get("rows", []):
                        strike = row["strike"]
                        for side in ("ce", "pe"):
                            if row.get(f"{side}_oi_aligned") is not None:
                                findings.append(Finding(
                                    layer="layer_e", metric="oi_vs_nse",
                                    key=f"{strike}{side.upper()}",
                                    ours=row.get(f"{side}_oi_ours"),
                                    theirs=row.get(f"{side}_oi_nse"),
                                    diff=f"{row.get(f'{side}_oi_diff_pct')}%",
                                    tol=f"±{chk.get('oi_tolerance_pct', 10)}%",
                                    verdict="PASS" if row[f"{side}_oi_aligned"] else "FAIL",
                                    note=""))
                            if row.get(f"{side}_ltp_aligned") is not None:
                                findings.append(Finding(
                                    layer="layer_e", metric="ltp_vs_nse",
                                    key=f"{strike}{side.upper()}",
                                    ours=row.get(f"{side}_ltp_ours"),
                                    theirs=row.get(f"{side}_ltp_nse"),
                                    diff=row.get(f"{side}_ltp_diff"),
                                    tol=f"±{chk.get('ltp_tolerance_pts', 5)}pt or ±{chk.get('ltp_tolerance_pct', 10)}%",
                                    verdict="PASS" if row[f"{side}_ltp_aligned"] else "FAIL",
                                    note=""))
                    findings.append(Finding(
                        layer="layer_e", metric="nse_coverage", key=symbol,
                        ours=f"missing_in_ours={summ.get('missing_in_ours')}",
                        theirs=f"missing_in_nse={summ.get('missing_in_nse')}",
                        diff=None, tol="n/a", verdict="INFO",
                        note=f"NSE covers the full chain; we subscribe ATM±11 -> "
                             f"{summ.get('total_strikes')} strikes union, source={chk.get('nse_source')}"))
            except Exception as e:
                findings.append(Finding(
                    layer="layer_e", metric="oi_vs_nse", key=symbol,
                    ours=None, theirs=None, diff=None, tol="±10%",
                    verdict="SOURCE_UNAVAILABLE", note=repr(e)))

        elif symbol.upper() == "SENSEX":
            # ---- best-effort BSE probe (expected to be blocked) ----
            url = "https://api.bseindia.com/BseIndiaAPI/api/ddlExpiry_IV/w"
            try:
                async with httpx.AsyncClient(headers=BSE_HEADERS, timeout=20.0) as bse:
                    r = await bse.get(url, params={"ProductType": "IO", "scrip_cd": "1"})
                ok = r.status_code == 200
                findings.append(Finding(
                    layer="layer_e", metric="bse_probe", key="SENSEX",
                    ours=None, theirs=f"HTTP {r.status_code}", diff=None, tol="n/a",
                    verdict="INFO" if ok else "SOURCE_UNAVAILABLE",
                    note="BSE India API probe — SENSEX has no NSE equivalent; "
                         "documented limitation when blocked"))
            except Exception as e:
                findings.append(Finding(
                    layer="layer_e", metric="bse_probe", key="SENSEX",
                    ours=None, theirs=None, diff=None, tol="n/a",
                    verdict="SOURCE_UNAVAILABLE", note=repr(e)))

    common.write_layer_output(f"layer_e_round{round_id}_{symbol}", findings, snapshots,
                              {"symbol": symbol})
    return findings


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--round", type=int, default=1)
    args = ap.parse_args()
    asyncio.run(run(args.symbol.upper(), args.round))
