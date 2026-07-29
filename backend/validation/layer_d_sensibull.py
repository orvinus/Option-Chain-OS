"""Layer D — cross-reference vs Sensibull (Zerodha-backed web app).

Sensibull's web app is driven by public JSON endpoints on oxide.sensibull.com.
Exact paths drift, so this layer runs endpoint discovery first (--discover
saves every candidate's status + body head), then compares per-strike OI/LTP
and totals/PCR for whichever endpoint yields a parseable option chain.
Failures degrade to SOURCE_UNAVAILABLE — never a run failure.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

import httpx

from . import common
from .common import Finding, SourceSnapshot

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": "https://web.sensibull.com/",
    "Origin": "https://web.sensibull.com",
}

CANDIDATES = [
    "https://oxide.sensibull.com/v1/compute/cache/live_derivative_prices/{u}",
    "https://oxide.sensibull.com/v1/compute/cache/option_chain/{u}",
    "https://oxide.sensibull.com/v1/compute/cache/instrument_details/{u}",
    "https://oxide.sensibull.com/v1/compute/cache/live_prices/{u}",
    "https://oxide.sensibull.com/v1/instruments/{u}",
    "https://oxide.sensibull.com/v1/current_option_chain/{u}",
    "https://api.sensibull.com/v1/compute/cache/live_derivative_prices/{u}",
]

# Zerodha/Kite instrument tokens for the underlying indices — Sensibull's
# live_derivative_prices endpoint is keyed by these (discovered 2026-07-03:
# passing a name returns 400 "Cannot parse `NIFTY` to a `i32`").
UNDERLYING_TOKEN = {"NIFTY": 256265, "SENSEX": 265}
KITE_INSTRUMENTS = {"NIFTY": "https://api.kite.trade/instruments/NFO",
                    "SENSEX": "https://api.kite.trade/instruments/BFO"}
KITE_NAME = {"NIFTY": "NIFTY", "SENSEX": "SENSEX"}

# Live tolerances vs a third-party vendor with its own refresh cadence.
OI_TOL_PCT = {"live": 15.0, "closed": 5.0}
LTP_TOL = {"live": (3.0, 5.0), "closed": (0.05, 0.0)}  # (abs pts, pct)
TOTAL_TOL_PCT = {"live": 5.0, "closed": 2.0}
PCR_TOL = {"live": 0.05, "closed": 0.02}


async def discover(underlying: str) -> dict[str, Any]:
    """Probe all candidate endpoints; save statuses + parseable bodies."""
    results: dict[str, Any] = {}
    async with httpx.AsyncClient(headers=HEADERS, timeout=20.0, follow_redirects=True) as client:
        for tpl in CANDIDATES:
            url = tpl.format(u=underlying)
            entry: dict[str, Any] = {"url": url}
            try:
                r = await client.get(url)
                entry["status"] = r.status_code
                ct = r.headers.get("content-type", "")
                entry["content_type"] = ct
                if r.status_code == 200 and "json" in ct:
                    body = r.json()
                    entry["parsed"] = True
                    entry["body"] = body
                else:
                    entry["parsed"] = False
                    entry["body_head"] = r.text[:300]
            except Exception as e:
                entry["status"] = None
                entry["error"] = repr(e)
            results[url] = entry
    slim = {
        u: {k: v for k, v in e.items() if k != "body"} | (
            {"body_head": json.dumps(e.get("body"))[:600]} if e.get("parsed") else {})
        for u, e in results.items()
    }
    common.save_json(f"sensibull_discovery_{underlying}.json", slim)
    for u, e in results.items():
        if e.get("parsed"):
            common.save_json(
                f"raw_sensibull_{u.rsplit('/', 2)[-2]}_{underlying}.json", e["body"])
    return results


def extract_chain(body: Any, expiry: str) -> dict[tuple[int, str], dict]:
    """Best-effort extraction of {(strike, CE|PE): {oi, ltp}} for one expiry.

    Handles the known live_derivative_prices shape:
      data -> {expiry: {"options": {strike: {"CE": {...}, "PE": {...}}}}}
    plus a generic recursive fallback for records with strike/expiry/oi keys.
    """
    out: dict[tuple[int, str], dict] = {}

    def norm_num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def add(strike, otype, rec: dict):
        oi = rec.get("oi", rec.get("OI", rec.get("open_interest", rec.get("openInterest"))))
        ltp = rec.get("last_price", rec.get("ltp", rec.get("LTP", rec.get("lastPrice"))))
        oi_f, ltp_f = norm_num(oi), norm_num(ltp)
        if oi_f is None and ltp_f is None:
            return
        try:
            out[(int(float(strike)), otype)] = {
                "oi": int(oi_f) if oi_f is not None else None, "ltp": ltp_f}
        except (TypeError, ValueError):
            pass

    data = body.get("data") if isinstance(body, dict) else body
    # shape 1: {expiry: {"options": {strike: {"CE": {...}, "PE": {...}}}}}
    if isinstance(data, dict):
        per_exp = data.get(expiry)
        if isinstance(per_exp, dict):
            options = per_exp.get("options", per_exp)
            if isinstance(options, dict):
                for strike, sides in options.items():
                    if isinstance(sides, dict):
                        for otype in ("CE", "PE"):
                            rec = sides.get(otype)
                            if isinstance(rec, dict):
                                add(strike, otype, rec)
    if out:
        return out

    # shape 2: generic recursive walk for row-like dicts
    def walk(node: Any):
        if isinstance(node, dict):
            keys = {k.lower() for k in node.keys()}
            if "strike" in keys and ({"expiry", "expiry_date"} & keys):
                exp_val = str(node.get("expiry", node.get("expiry_date", "")))[:10]
                if exp_val == expiry:
                    otype = str(node.get("option_type", node.get("instrument_type", ""))).upper()
                    if otype in ("CE", "PE"):
                        add(node.get("strike"), otype, node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(body)
    return out


async def fetch_kite_token_map(symbol: str, expiry: str) -> tuple[dict[int, tuple[int, str]], dict[tuple[int, str], str], int | None]:
    """From Kite's public instruments dump:
    ({zerodha_token: (strike, CE|PE)}, {(strike, CE|PE): exchange_token}, lot_size).

    exchange_token is the NSE/BSE exchange token — the same ID space XTS uses
    for exchangeInstrumentID, enabling a direct token-identity cross-check.
    """
    url = KITE_INSTRUMENTS[symbol.upper()]
    name = KITE_NAME[symbol.upper()]
    tok_map: dict[int, tuple[int, str]] = {}
    exch_map: dict[tuple[int, str], str] = {}
    lot_size: int | None = None
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        r = await client.get(url)
        r.raise_for_status()
        lines = r.text.splitlines()
    if not lines:
        return tok_map, exch_map, lot_size
    header = [h.strip().strip('"') for h in lines[0].split(",")]
    idx = {k: header.index(k) for k in
           ("instrument_token", "exchange_token", "name", "expiry", "strike",
            "instrument_type", "lot_size")}
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) < len(header):
            continue
        if parts[idx["name"]].strip('"') != name or parts[idx["expiry"]] != expiry:
            continue
        itype = parts[idx["instrument_type"]]
        if itype not in ("CE", "PE"):
            continue
        try:
            strike = int(float(parts[idx["strike"]]))
            tok_map[int(parts[idx["instrument_token"]])] = (strike, itype)
            exch_map[(strike, itype)] = parts[idx["exchange_token"]]
            if lot_size is None:
                lot_size = int(parts[idx["lot_size"]])
        except (TypeError, ValueError):
            continue
    return tok_map, exch_map, lot_size


async def fetch_sensibull_chain(symbol: str, expiry: str):
    """(meta, {(strike, CE|PE): {oi, ltp, volume}}, exchange_token_map)."""
    u_token = UNDERLYING_TOKEN[symbol.upper()]
    async with httpx.AsyncClient(headers=HEADERS, timeout=30.0, follow_redirects=True) as client:
        r = await client.get(
            f"https://oxide.sensibull.com/v1/compute/cache/live_derivative_prices/{u_token}")
        r.raise_for_status()
        body = r.json()
    data = body.get("data") or {}
    per_exp = (data.get("per_expiry_data") or {}).get(expiry) or {}
    options = per_exp.get("options") or []
    token_map, exch_map, lot_size = await fetch_kite_token_map(symbol, expiry)
    chain: dict[tuple[int, str], dict] = {}
    unmapped = 0
    for rec in options:
        tok = rec.get("token")
        key = token_map.get(int(tok)) if tok is not None else None
        if key is None:
            unmapped += 1
            continue
        chain[key] = {"oi": rec.get("oi"), "ltp": rec.get("last_price"),
                      "volume": rec.get("volume")}
    meta = {
        "underlying_price": data.get("underlying_price"),
        "last_updated_at": data.get("last_updated_at"),
        "atm_strike": per_exp.get("atm_strike"),
        "options_in_feed": len(options),
        "mapped": len(chain),
        "unmapped": unmapped,
        "lot_size": lot_size,
    }
    common.save_json(f"raw_sensibull_chain_{symbol}.json",
                     {"meta": meta, "chain": {f"{k[0]}{k[1]}": v for k, v in chain.items()}})
    return meta, chain, exch_map


async def run(symbol: str, round_id: int = 1) -> list[Finding]:
    findings: list[Finding] = []
    snapshots: list[SourceSnapshot] = []
    state = common.market_state()
    underlying = symbol.upper()

    async with httpx.AsyncClient() as api:
        expiry = await common.active_expiry(api, symbol)
        ours = await common.api_get(api, "/api/option-chain", {"symbol": symbol, "expiry": expiry})
        our_spot = (await common.api_get(api, "/api/spot")).get("spot")
    common.save_json(f"raw_ours_for_sensibull_round{round_id}_{symbol}.json", ours)

    chain: dict[tuple[int, str], dict] = {}
    meta: dict = {}
    exch_map: dict[tuple[int, str], str] = {}
    source_url = f"live_derivative_prices/{UNDERLYING_TOKEN.get(underlying)}"
    try:
        meta, chain, exch_map = await fetch_sensibull_chain(symbol, expiry)
        snapshots.append(SourceSnapshot(
            source=source_url, fetched_at_utc=common.now_utc().isoformat(), ok=bool(chain),
            extra=meta))
    except Exception as e:
        snapshots.append(SourceSnapshot(
            source=source_url, fetched_at_utc=common.now_utc().isoformat(), ok=False,
            error=repr(e)))

    if not chain:
        findings.append(Finding(
            layer="layer_d", metric="sensibull_source", key=underlying,
            ours=None, theirs=None, diff=None, tol="n/a", verdict="SOURCE_UNAVAILABLE",
            note=f"Sensibull chain unavailable for {expiry} "
                 f"(meta={meta or 'fetch failed'}) — see snapshots"))
        common.write_layer_output(f"layer_d_round{round_id}_{symbol}", findings, snapshots,
                                  {"symbol": symbol, "expiry": expiry})
        return findings

    findings.append(Finding(
        layer="layer_d", metric="sensibull_source", key=underlying,
        ours=None, theirs=source_url, diff=None, tol="n/a", verdict="PASS",
        note=f"{len(chain)} contracts mapped via Kite token dump for {expiry} "
             f"(feed updated {meta.get('last_updated_at')})"))

    # ---- spot vs Sensibull underlying ----
    sb_spot = meta.get("underlying_price")
    if our_spot is not None and sb_spot is not None:
        p = common.pct_diff(our_spot, sb_spot)
        findings.append(Finding(
            layer="layer_d", metric="spot_vs_sensibull", key=underlying,
            ours=our_spot, theirs=sb_spot, diff=f"{p:.3f}%",
            tol="±0.1% (few-second skew)", verdict="PASS" if p <= 0.1 else "FAIL",
            ts_theirs=str(meta.get("last_updated_at")), note=""))

    # ---- token identity: XTS exchangeInstrumentID == NSE/BSE exchange_token ----
    if exch_map:
        uni = common.todays_universe(symbol)
        matched = sum(1 for u in uni if exch_map.get((u.strike, u.option_type)) == u.token)
        with_ref = sum(1 for u in uni if (u.strike, u.option_type) in exch_map)
        findings.append(Finding(
            layer="layer_d", metric="token_identity", key=symbol,
            ours=f"{matched}/{with_ref} XTS tokens == exchange_token",
            theirs="all matched", diff=with_ref - matched, tol="100%",
            verdict="PASS" if matched == with_ref and with_ref > 0 else "FAIL",
            note="XTS exchangeInstrumentID vs Kite exchange_token per (strike, type) — "
                 "proves we subscribe the exact NSE contracts Zerodha maps"))

    oi_tol = OI_TOL_PCT[state]
    ltp_abs, ltp_pct = LTP_TOL[state]

    our_ce_total = our_pe_total = sb_ce_total = sb_pe_total = 0
    checked = 0
    for r in ours.get("rows", []):
        strike = int(r["strike"])
        for otype, oi_field, ltp_field in (("CE", "call_oi", "call_ltp"), ("PE", "put_oi", "put_ltp")):
            sb = chain.get((strike, otype))
            if not sb:
                continue
            checked += 1
            our_oi = int(r[oi_field])
            sb_oi = sb.get("oi")
            if sb_oi is not None:
                if otype == "CE":
                    our_ce_total += our_oi
                    sb_ce_total += sb_oi
                else:
                    our_pe_total += our_oi
                    sb_pe_total += sb_oi
                p = common.pct_diff(our_oi, sb_oi)
                findings.append(Finding(
                    layer="layer_d", metric="oi_vs_sensibull", key=f"{strike}{otype}",
                    ours=our_oi, theirs=sb_oi, diff=f"{p:.2f}%", tol=f"±{oi_tol}% ({state})",
                    verdict="PASS" if p <= oi_tol else "FAIL",
                    ts_ours=ours.get("asof"), note=""))
            our_ltp = r.get(ltp_field)
            sb_ltp = sb.get("ltp")
            if our_ltp is not None and sb_ltp is not None:
                d = abs(float(our_ltp) - sb_ltp)
                ok = d <= ltp_abs or (ltp_pct and common.pct_diff(our_ltp, sb_ltp) <= ltp_pct)
                findings.append(Finding(
                    layer="layer_d", metric="ltp_vs_sensibull", key=f"{strike}{otype}",
                    ours=our_ltp, theirs=sb_ltp, diff=f"{d:.2f}",
                    tol=f"±{ltp_abs}pt or ±{ltp_pct}% ({state})",
                    verdict="PASS" if ok else "FAIL", note=""))

    if sb_ce_total and sb_pe_total:
        tol = TOTAL_TOL_PCT[state]
        for name, o, t in (("total_call_oi", our_ce_total, sb_ce_total),
                           ("total_put_oi", our_pe_total, sb_pe_total)):
            p = common.pct_diff(o, t)
            findings.append(Finding(
                layer="layer_d", metric=f"{name}_vs_sensibull", key=f"{symbol} (matched strikes)",
                ours=o, theirs=t, diff=f"{p:.2f}%", tol=f"±{tol}%",
                verdict="PASS" if p <= tol else "FAIL", note=f"{checked} contracts compared"))
        our_pcr = our_pe_total / our_ce_total if our_ce_total else None
        sb_pcr = sb_pe_total / sb_ce_total if sb_ce_total else None
        if our_pcr and sb_pcr:
            d = abs(our_pcr - sb_pcr)
            findings.append(Finding(
                layer="layer_d", metric="pcr_vs_sensibull", key=symbol,
                ours=round(our_pcr, 4), theirs=round(sb_pcr, 4), diff=round(d, 4),
                tol=f"±{PCR_TOL[state]}", verdict="PASS" if d <= PCR_TOL[state] else "FAIL",
                note="PCR over matched strike window"))

    common.write_layer_output(f"layer_d_round{round_id}_{symbol}", findings, snapshots,
                              {"symbol": symbol, "expiry": expiry, "source": source_url})
    return findings


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--round", type=int, default=1)
    ap.add_argument("--discover", action="store_true", help="discovery probe only")
    args = ap.parse_args()
    if args.discover:
        res = asyncio.run(discover(args.symbol.upper()))
        for u, e in res.items():
            print(f"{e.get('status')}  parsed={e.get('parsed', False)}  {u}")
    else:
        asyncio.run(run(args.symbol.upper(), args.round))
