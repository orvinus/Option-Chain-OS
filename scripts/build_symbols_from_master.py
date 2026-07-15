"""Generate / grow data/symbols.json from the XTS instrument master.

Pulls the full options master (NSEFO + BSEFO + MCXFO) using the CURRENTLY STORED
market-data token (from the auth_sessions table) — it NEVER calls /auth/login, so
running it will not disturb a live feed (XTS is single-session-per-appKey). For
each F&O underlying it derives the exchange, kind, lot size, and strike step
(inferred from the nearest expiry's strike grid) and appends any symbols missing
from the seed. Existing hand-tuned entries are never overwritten (idempotent).

Prereq: the backend must have logged in at least once today so a fresh token is
in auth_sessions. If the token is stale you'll get a 401 — log in via the
dashboard first, then re-run.

Usage (from repo root):
    backend/.venv/Scripts/python.exe scripts/build_symbols_from_master.py --dry-run
    backend/.venv/Scripts/python.exe scripts/build_symbols_from_master.py            # writes
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# psycopg / selector-loop parity with the rest of the tooling on Windows.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from sqlalchemy import create_engine, text  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.logging import configure_logging  # noqa: E402
from app.market import scripmaster  # noqa: E402

# Quiet the per-line MCX-futures debug (they correctly fail the option filter).
configure_logging("WARNING")
from app.market.scripmaster import (  # noqa: E402
    _INDEX_NAMES,
    _infer_strike_step,
    _master_line_to_row,
    _row_to_token,
)
from app.market_data import xts_client  # noqa: E402

SYMBOLS_FILE = ROOT / "data" / "symbols.json"

# Internal exchange tag (from the master) -> registry exchange + sector + tier.
_TAG_TO_EXCHANGE = {"NFO": "NSE", "BFO": "BSE", "MFO": "MCX"}


def _load_token() -> str:
    """Latest jwt_token from auth_sessions. No login, ever."""
    eng = create_engine(settings.db_url_sync, pool_pre_ping=True)
    try:
        with eng.connect() as conn:
            row = conn.execute(
                text("SELECT jwt_token FROM auth_sessions ORDER BY issued_at DESC LIMIT 1")
            ).mappings().first()
    finally:
        eng.dispose()
    if not row or not row["jwt_token"]:
        raise SystemExit("No XTS token in auth_sessions — log in via the dashboard first.")
    return str(row["jwt_token"])


def _classify(tag: str, name: str) -> tuple[str, str, str, str]:
    """(exchange, kind, sector, poll_tier) for an underlying given its segment tag."""
    exchange = _TAG_TO_EXCHANGE.get(tag, "NSE")
    if name in _INDEX_NAMES:
        return exchange, "index", "Indices", "fast"
    if tag == "MFO":
        return exchange, "commodity", "Commodities", "mid"
    sector = "BSE Stocks" if exchange == "BSE" else "NSE Stocks"
    return exchange, "stock", sector, "slow"


async def _derive_universe(token: str) -> dict[str, dict]:
    """Return {SYMBOL: entry_dict} for every F&O underlying in the master."""
    dump = await xts_client.get_master(token, list(scripmaster._FO_SEGMENTS))
    # name -> list of InstrumentToken (only options; near-expiry used for step)
    groups: dict[str, list] = {}
    for line in dump.splitlines():
        row = _master_line_to_row(line.strip())
        if not row:
            continue
        tok = _row_to_token(row)
        if tok and tok.name:
            groups.setdefault(tok.name, []).append(tok)

    today = datetime.utcnow().date()
    out: dict[str, dict] = {}
    for name, toks in groups.items():
        tag = Counter(t.exchange for t in toks).most_common(1)[0][0]
        exchange, kind, sector, tier = _classify(tag, name)
        lots = [t.lotsize for t in toks if t.lotsize and t.lotsize > 0]
        lot_size = Counter(lots).most_common(1)[0][0] if lots else 0
        future = sorted({t.expiry for t in toks if t.expiry >= today})
        near = future[0] if future else (sorted({t.expiry for t in toks})[0] if toks else None)
        step = _infer_strike_step(t.strike for t in toks if near and t.expiry == near) if near else 0
        out[name] = {
            "symbol": name,
            "display": name,
            "kind": kind,
            "sector": sector,
            "exchange": exchange,
            "spot_token": None,
            "fno_eligible": True,
            "lot_size": int(lot_size),
            "strike_step": int(step) if step > 0 else 50,
            "poll_tier": tier,
        }
    return out


def _load_seed() -> dict:
    if not SYMBOLS_FILE.exists():
        return {"version": 1, "symbols": []}
    return json.loads(SYMBOLS_FILE.read_text(encoding="utf-8"))


def _save_seed(seed: dict) -> None:
    SYMBOLS_FILE.write_text(
        json.dumps(seed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="Report only; do not write symbols.json.")
    ap.add_argument("--exchange", choices=["NSE", "BSE", "MCX"], action="append",
                    help="Limit appended symbols to these exchange(s). Default: all.")
    args = ap.parse_args()

    token = _load_token()
    universe = await _derive_universe(token)

    seed = _load_seed()
    existing = {e["symbol"].upper() for e in seed["symbols"]}
    wanted_ex = set(args.exchange) if args.exchange else {"NSE", "BSE", "MCX"}

    by_ex: Counter = Counter()
    to_add: list[dict] = []
    for name, entry in sorted(universe.items()):
        if entry["exchange"] not in wanted_ex:
            continue
        by_ex[entry["exchange"]] += 1
        if name in existing:
            continue
        to_add.append(entry)

    print(f"Master underlyings: {len(universe)}  (by exchange: {dict(by_ex)})")
    print(f"Already in seed:     {len(existing)}")
    print(f"New to append:       {len(to_add)}")
    for e in to_add[:30]:
        print(f"  + {e['exchange']:3} {e['kind']:9} {e['symbol']:14} lot={e['lot_size']:<6} step={e['strike_step']}")
    if len(to_add) > 30:
        print(f"  (... {len(to_add) - 30} more)")

    if args.dry_run:
        print("\n[dry-run] no changes written.")
        return 0
    if to_add:
        seed["symbols"].extend(to_add)
        _save_seed(seed)
        print(f"\nAppended {len(to_add)} entries to {SYMBOLS_FILE}")
    else:
        print("\nNothing to append; seed already covers the master.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
