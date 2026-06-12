"""Build / merge data/symbols.json from an NSE marketwatch CSV.

Usage:
    python scripts/build_symbols.py <path-to-csv>

Behavior:
    - Reads the CSV (NSE marketwatch export, multi-section format with
      index header rows like "NIFTY 50", "NIFTY BANK", etc.).
    - Extracts the unique set of constituent symbols across all sections.
    - Loads data/symbols.json (the authoritative seed).
    - Reports any CSV symbols missing from the seed (and any seed symbols
      not present in the CSV — useful when the index composition changes).
    - If --append-missing is passed, appends missing CSV symbols with
      sector="Unknown" and fno_eligible=false so a human can fill in tags
      without losing the row entirely.

This script is idempotent: existing seed entries are never overwritten.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

# Force UTF-8 stdout on Windows so emoji/BOM/diacritics in symbol names don't blow up cp1252.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Valid NSE trading symbol: uppercase letters/digits with optional & or - (e.g. M&M, BAJAJ-AUTO).
_SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9&\-]{0,19}$")

ROOT = Path(__file__).resolve().parents[1]
SYMBOLS_FILE = ROOT / "data" / "symbols.json"

# Rows whose first cell is not a constituent stock — index headers + column headers.
NON_SYMBOL_ROWS = {
    "NIFTY 50",
    "NIFTY BANK",
    "NIFTY NEXT 50",
    "NIFTY MIDCAP SELECT",
    "NIFTY FINANCIAL SERVICES",
    "NIFTY FIN SERVICE",
    "NIFTY MIDCAP 50",
    "NIFTY MIDCAP 100",
    "SYMBOL",
}


def _parse_csv(path: Path) -> set[str]:
    """Return the set of unique constituent symbols across all sections."""
    symbols: set[str] = set()
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            first = (row[0] or "").strip().strip("﻿")
            if not first or first.upper() in NON_SYMBOL_ROWS:
                continue
            # Only accept rows whose first cell is a plausible NSE trading
            # symbol — skips multi-line header fragments and section dividers.
            if _SYMBOL_PATTERN.match(first.upper()):
                symbols.add(first.upper())
    return symbols


def _load_seed() -> dict:
    if not SYMBOLS_FILE.exists():
        return {"version": 1, "symbols": []}
    return json.loads(SYMBOLS_FILE.read_text(encoding="utf-8"))


def _save_seed(seed: dict) -> None:
    SYMBOLS_FILE.write_text(
        json.dumps(seed, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("csv", type=Path, help="Path to NSE marketwatch CSV")
    ap.add_argument(
        "--append-missing",
        action="store_true",
        help="Append CSV symbols not in the seed (with sector=Unknown).",
    )
    args = ap.parse_args()

    if not args.csv.exists():
        print(f"error: CSV not found: {args.csv}", file=sys.stderr)
        return 1

    csv_symbols = _parse_csv(args.csv)
    seed = _load_seed()
    seed_symbols = {entry["symbol"].upper() for entry in seed["symbols"]}

    missing_from_seed = sorted(csv_symbols - seed_symbols)
    extra_in_seed = sorted(seed_symbols - csv_symbols - {
        # Indices and synthetic entries that aren't expected in the CSV body.
        "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50",
    })

    print(f"CSV symbols: {len(csv_symbols)}")
    print(f"Seed symbols: {len(seed_symbols)}")
    print(f"Missing from seed: {len(missing_from_seed)}")
    for s in missing_from_seed:
        print(f"  - {s}")
    print(f"In seed but not in CSV: {len(extra_in_seed)}")
    for s in extra_in_seed[:20]:
        print(f"  - {s}")
    if len(extra_in_seed) > 20:
        print(f"  (... {len(extra_in_seed) - 20} more)")

    if args.append_missing and missing_from_seed:
        for sym in missing_from_seed:
            seed["symbols"].append({
                "symbol": sym,
                "display": sym,
                "kind": "stock",
                "sector": "Unknown",
                "exchange": "NSE",
                "spot_token": None,
                "fno_eligible": False,
                "lot_size": 0,
                "strike_step": 5,
            })
        _save_seed(seed)
        print(f"\nAppended {len(missing_from_seed)} entries to {SYMBOLS_FILE}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
