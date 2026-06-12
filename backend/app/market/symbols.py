"""Symbol registry — sources of truth for the multi-symbol expansion.

Loads ``data/symbols.json`` once at startup into an in-memory dict. Each entry
describes one tradable instrument (index or stock) with its sector, lot size,
strike step, and the SmartAPI spot token (for indices) or ``None`` (for stocks,
where the spot token is resolved at runtime via ``searchScrip('NSE', symbol)``).

The registry is the canonical answer to "which symbols can the dashboard show?".
The actual option-contract universe per symbol is resolved separately by
``scripmaster.resolve_option_universe(spot, symbol)``.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..core.config import PROJECT_ROOT
from ..core.logging import get_logger

log = get_logger("symbols")

SYMBOLS_FILE = PROJECT_ROOT / "data" / "symbols.json"


@dataclass(frozen=True)
class SymbolEntry:
    symbol: str
    display: str
    kind: str           # "index" | "stock"
    sector: str
    exchange: str       # "NSE"
    spot_token: str | None
    fno_eligible: bool
    lot_size: int
    strike_step: int


class SymbolRegistry:
    def __init__(self) -> None:
        self._by_symbol: dict[str, SymbolEntry] = {}
        self._lock = threading.Lock()
        self._loaded = False

    def load(self) -> None:
        with self._lock:
            if self._loaded:
                return
            if not SYMBOLS_FILE.exists():
                log.warning("symbols.seed_missing", path=str(SYMBOLS_FILE))
                self._loaded = True
                return
            raw = json.loads(SYMBOLS_FILE.read_text(encoding="utf-8"))
            for item in raw.get("symbols", []):
                entry = SymbolEntry(
                    symbol=str(item["symbol"]).upper(),
                    display=str(item.get("display") or item["symbol"]),
                    kind=str(item.get("kind", "stock")),
                    sector=str(item.get("sector", "Misc")),
                    exchange=str(item.get("exchange", "NSE")),
                    spot_token=item.get("spot_token"),
                    fno_eligible=bool(item.get("fno_eligible", False)),
                    lot_size=int(item.get("lot_size") or 0),
                    strike_step=int(item.get("strike_step") or 50),
                )
                self._by_symbol[entry.symbol] = entry
            self._loaded = True
            log.info("symbols.loaded", count=len(self._by_symbol))

    def get(self, symbol: str) -> SymbolEntry | None:
        self.load()
        return self._by_symbol.get((symbol or "").upper())

    def require(self, symbol: str) -> SymbolEntry:
        entry = self.get(symbol)
        if entry is None:
            raise KeyError(f"Unknown symbol: {symbol!r}")
        return entry

    def all(self) -> list[SymbolEntry]:
        self.load()
        return list(self._by_symbol.values())

    def by_sector(self) -> list[tuple[str, list[SymbolEntry]]]:
        """Return [(sector, [entries...]), ...] with Indices first, then alpha."""
        groups: dict[str, list[SymbolEntry]] = {}
        for e in self.all():
            groups.setdefault(e.sector, []).append(e)
        ordered: list[tuple[str, list[SymbolEntry]]] = []
        if "Indices" in groups:
            ordered.append(("Indices", sorted(groups.pop("Indices"), key=lambda x: x.symbol)))
        for sector in sorted(groups.keys()):
            ordered.append((sector, sorted(groups[sector], key=lambda x: x.symbol)))
        return ordered

    def update_fno_flag(self, symbol: str, fno_eligible: bool) -> None:
        """Update fno_eligible in-memory (post-startup searchScrip probe result)."""
        entry = self.get(symbol)
        if entry is None:
            return
        with self._lock:
            self._by_symbol[entry.symbol] = SymbolEntry(
                symbol=entry.symbol, display=entry.display, kind=entry.kind,
                sector=entry.sector, exchange=entry.exchange, spot_token=entry.spot_token,
                fno_eligible=fno_eligible, lot_size=entry.lot_size, strike_step=entry.strike_step,
            )

    def update_spot_token(self, symbol: str, spot_token: str) -> None:
        entry = self.get(symbol)
        if entry is None:
            return
        with self._lock:
            self._by_symbol[entry.symbol] = SymbolEntry(
                symbol=entry.symbol, display=entry.display, kind=entry.kind,
                sector=entry.sector, exchange=entry.exchange, spot_token=spot_token,
                fno_eligible=entry.fno_eligible, lot_size=entry.lot_size, strike_step=entry.strike_step,
            )


_registry = SymbolRegistry()


def get_registry() -> SymbolRegistry:
    return _registry


def iter_symbols() -> Iterable[SymbolEntry]:
    return get_registry().all()
