"""Phase-0 empirical probe for MCX (and BSE/NSE) F&O support — READ ONLY.

Reuses the CURRENTLY STORED market-data token (auth_sessions) and NEVER calls
/auth/login, so it will not disturb a live feed. Confirms the six unknowns that
cannot be read from code:
  1. MCX F&O segment tag/prefix (is it "MCXFO"? segment 51?)
  2. MCX master column layout vs NSEFO (LotSize=12, UnderlyingId=14, Strike=17...)
  3. MCX Series / InstrumentType values
  4. per-commodity strike step & lot size
  5. that our _master_line_to_row parses MCX lines (parse rate)
  6. that /instruments/quotes returns OI/LTP for a commodity option

Run from repo root. Point DB at the compose DB via the socat proxy if running
on the host (native postgres owns 5432):
    DB_URL_SYNC=postgresql+psycopg://postgres:postgres@localhost:55432/oi \\
      backend/.venv/Scripts/python.exe scripts/probe_mcx.py
"""
from __future__ import annotations

import asyncio
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import httpx  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.logging import configure_logging  # noqa: E402
from app.market import scripmaster as s  # noqa: E402
from app.market_data import xts_client  # noqa: E402

# Suppress the per-line mcx debug spam (futures correctly fail the option filter).
configure_logging("WARNING")


def load_token() -> str:
    eng = create_engine(settings.db_url_sync, pool_pre_ping=True)
    try:
        with eng.connect() as conn:
            row = conn.execute(
                text("SELECT jwt_token, issued_at FROM auth_sessions ORDER BY issued_at DESC LIMIT 1")
            ).mappings().first()
    finally:
        eng.dispose()
    if not row or not row["jwt_token"]:
        raise SystemExit("No token in auth_sessions — log in via the dashboard first.")
    print(f"token issued_at: {row['issued_at']}")
    return str(row["jwt_token"])


def analyse_segment(dump: str, prefix: str, sample: int = 3) -> list[str]:
    lines = [ln for ln in dump.splitlines() if ln[:len(prefix)].upper() == prefix]
    print(f"\n=== {prefix}: {len(lines)} raw lines ===")
    for ln in lines[:sample]:
        parts = ln.split("|")
        print(f"  fields={len(parts)} :: {ln[:200]}")
    return lines


async def main() -> int:
    token = load_token()
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(
            f"{xts_client.base_url()}/instruments/indexlist",
            params={"exchangeSegment": xts_client.SEG_NSECM},
            headers=xts_client.authed_headers(token),
        )
        print(f"token check (indexlist): HTTP {r.status_code}")
        if r.status_code == 401:
            raise SystemExit("Token is stale (401). Log in via the dashboard, then re-run.")

    # 1) Fetch the full FO master (all three segments).
    dump = await xts_client.get_master(token, ["NSEFO", "BSEFO", "MCXFO"])
    print(f"master total lines: {len(dump.splitlines()):,}")

    nse = analyse_segment(dump, "NSEFO")
    bse = analyse_segment(dump, "BSEFO")
    mcx = analyse_segment(dump, "MCXFO")

    if not mcx:
        print("\n!! No MCXFO lines — appKey may lack MCX entitlement, or the segment "
              "prefix differs. Try get_master(token, ['MCXFO']) alone and inspect.")
    else:
        # 2/3) Column layout + series/type distributions on MCX option lines.
        opt = [ln for ln in mcx if ln.split("|")[s._COL_INSTRUMENT_TYPE].strip() == s._INSTRUMENT_TYPE_OPTIONS]
        print(f"\nMCXFO InstrumentType==2 (options): {len(opt)} of {len(mcx)}")
        series = Counter(ln.split("|")[s._COL_SERIES].strip().upper() for ln in mcx if len(ln.split('|')) > s._COL_SERIES)
        itypes = Counter(ln.split("|")[s._COL_INSTRUMENT_TYPE].strip() for ln in mcx if len(ln.split('|')) > s._COL_INSTRUMENT_TYPE)
        print(f"MCXFO Series values: {dict(series)}")
        print(f"MCXFO InstrumentType values: {dict(itypes)}")

        print("\n--- sample MCX OPTION (type 2) raw lines + column check ---")
        for ln in opt[:4]:
            p = ln.split("|")
            print(f"  fields={len(p)} :: {ln[:190]}")
            print(f"    name[3]={p[3]!r} series[5]={p[5]!r} lot[12]={p[12]!r} uid[14]={p[14]!r} "
                  f"exp[16]={p[16]!r} strike[17]={p[17]!r} optype[18]={p[18]!r}")

        # 5) Parse rate through our normaliser.
        parsed = [s._master_line_to_row(ln) for ln in mcx]
        ok = [p for p in parsed if p]
        print(f"our _master_line_to_row parsed: {len(ok)}/{len(mcx)} MCX lines")
        names = Counter(p["name"] for p in ok)
        print(f"distinct MCX underlyings: {len(names)} -> {list(names)[:20]}")

        # 4) Per-commodity near-expiry step + lot.
        from datetime import datetime
        today = datetime.utcnow().date()
        for name in ["CRUDEOIL", "GOLD", "SILVER", "NATURALGAS", "COPPER", "ZINC"]:
            toks = [s._row_to_token(p) for p in ok if p["name"] == name]
            toks = [t for t in toks if t]
            if not toks:
                print(f"  {name}: (none)")
                continue
            fut = sorted({t.expiry for t in toks if t.expiry >= today}) or sorted({t.expiry for t in toks})
            near = fut[0]
            step = s._infer_strike_step(t.strike for t in toks if t.expiry == near)
            lot = Counter(t.lotsize for t in toks if t.lotsize).most_common(1)
            sample_strikes = sorted({t.strike for t in toks if t.expiry == near})[:6]
            print(f"  {name}: near={near} step={step} lot={lot[0][0] if lot else '?'} "
                  f"exch_type={toks[0].exchange_type} strikes~{sample_strikes}")

        # 6) Quote test: one commodity option (CE+PE) — do OI/LTP come back?
        crude = [s._row_to_token(p) for p in ok if p["name"] == "CRUDEOIL"]
        crude = [t for t in crude if t]
        if crude:
            near = sorted({t.expiry for t in crude if t.expiry >= today} or {t.expiry for t in crude})[0]
            probe = [t for t in crude if t.expiry == near][:6]
            insts = [{"exchangeSegment": t.exchange_type, "exchangeInstrumentID": int(t.token)} for t in probe]
            async with httpx.AsyncClient(timeout=30.0) as client:
                for code, label in ((xts_client.MSG_TOUCHLINE, "1501/LTP"), (xts_client.MSG_OPENINTEREST, "1510/OI")):
                    resp = await client.post(
                        f"{xts_client.base_url()}/instruments/quotes",
                        json={"instruments": insts, "xtsMessageCode": code, "publishFormat": "JSON"},
                        headers=xts_client.authed_headers(token),
                    )
                    entries = xts_client.parse_quote_entries(resp.json()) if resp.status_code == 200 else []
                    print(f"\nCRUDEOIL quote {label}: HTTP {resp.status_code}, {len(entries)} quotes")
                    for e in entries[:3]:
                        if code == xts_client.MSG_TOUCHLINE:
                            print(f"    iid={e.get('ExchangeInstrumentID')} ltp/vol={xts_client.quote_ltp_volume(e)}")
                        else:
                            print(f"    iid={e.get('ExchangeInstrumentID')} oi={xts_client.quote_oi(e)}")
    print("\n=== probe complete (read-only; no login performed) ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
