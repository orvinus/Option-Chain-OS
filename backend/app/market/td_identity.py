"""Contract identity across the four namespaces TrueData forces us to hold.

A single option contract has FOUR names in this system:

1. our tuple        ``(symbol, expiry, strike, option_type)`` — what every query
                    in the product actually groups by;
2. our token        ``td:NIFTY:260828:24500:CE`` — the TEXT primary-key component,
                    already used by six months of ``oi_archive_bars`` rows;
3. the vendor string``NIFTY26082824500CE`` — what goes in ``addsymbol``. The
                    convention appears in NO TrueData document; it was reverse
                    engineered from ``getSymbolOptionChain`` responses;
4. the vendor id    ``300000123`` — an opaque integer the socket returns in the
                    ``addsymbol`` ack and then uses in every subsequent frame.

(3) is untrusted by design: we never CONSTRUCT vendor strings for options, we only
ever echo back strings the vendor itself listed. The strike and type are read from
the string's TAIL — the same approach scripts/truedata_backfill.py uses, and the
reason the backfill survived without the vendor ever documenting its convention.

The tail parse requires ``<anything><yymmdd><strike><CE|PE>`` and validates the
six digits as a real date, so anything else fails CLOSED (returns None) rather
than producing a plausible wrong strike. That is the right trade — a contract we
cannot identify must not be subscribed, because the alternative is attributing
its open interest to the wrong strike. But it has an operational consequence: if
a segment we have not probed (MCX, BSE) names contracts differently, its
contracts silently never enter the universe. Callers MUST surface a non-zero
unparseable count instead of treating an empty chain as normal.

(2) is the load-bearing one. The live feed MUST emit byte-identical tokens to the
backfill, or the archive and the live table stop describing the same contract and
``oi_snapshots_unified`` silently splits every series in two.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from ..core.logging import get_logger
from ..market_data.truedata_rest import parse_option_tail

log = get_logger("td_identity")

# Sentinel expiry for instruments that have no option identity (index spot,
# continuous future). Must match scripts/truedata_backfill.py's SENTINEL_EXPIRY.
SENTINEL_EXPIRY = date(1970, 1, 1)

# Vendor WS subscription names for index spots.
#
# This is symbol_controller._INDEX_XTS_NAME's counterpart. The names are IDENTICAL
# to the XTS ones because both vendors use the exchange's own index names — but
# only "NIFTY BANK" is actually demonstrated as a WS subscription in any TrueData
# document ("NIFTY 50" appears solely in REST examples), so every entry here is
# provisional until probe B5 confirms it. A wrong name means that index's spot
# goes dark: no error, just no ticks.
INDEX_WS_NAMES: dict[str, str] = {
    "NIFTY": "NIFTY 50",
    "BANKNIFTY": "NIFTY BANK",
    "FINNIFTY": "NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NIFTY MID SELECT",
    "NIFTYNXT50": "NIFTY NEXT 50",
    "SENSEX": "SENSEX",
    "BANKEX": "BANKEX",
}

# Continuous ("-I") near-month future. Used as the reference price for MCX
# commodities, whose "spot" is the near future rolling monthly.
CONTINUOUS_FUTURE_SUFFIX = "-I"

_OPTION_TYPES = ("CE", "PE")


# --------------------------------------------------------------------------
# Tokens — the archive-compatible identity
# --------------------------------------------------------------------------
def option_token(symbol: str, expiry: date, strike: int, option_type: str) -> str:
    """``td:NIFTY:260828:24500:CE``.

    MUST stay byte-identical to the string scripts/truedata_backfill.py writes.
    A unit test pins this against real archive rows; do not "tidy" the format.
    """
    return f"td:{symbol}:{expiry:%y%m%d}:{int(strike)}:{option_type}"


def index_token(symbol: str) -> str:
    """``td:NIFTY:IDX`` — the archive's index-context row."""
    return f"td:{symbol}:IDX"


def future_token(symbol: str) -> str:
    """``td:NIFTY:FUT-I`` — the archive's continuous-future context row."""
    return f"td:{symbol}:FUT-I"


def contract_key(symbol: str, expiry: date, strike: int, option_type: str) -> str:
    """Vendor-neutral identity: the token minus its ``td:`` prefix.

    Mirrors the SQL ``contract_key()`` function from migration 0006 exactly, so a
    Python-side join key and a SQL-side one can never drift.
    """
    return f"{symbol}:{expiry:%y%m%d}:{int(strike)}:{option_type}"


def token_to_contract_key(token: str) -> str | None:
    """``td:NIFTY:260828:24500:CE`` -> ``NIFTY:260828:24500:CE``.

    Returns None for XTS-era numeric tokens and for the IDX/FUT context rows,
    which have no contract identity.
    """
    if not token.startswith("td:"):
        return None
    rest = token[3:]
    return rest if rest.count(":") == 3 else None


# --------------------------------------------------------------------------
# Vendor strings -> our tuple
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TdContract:
    """One subscribable TrueData option contract, fully identified."""

    vendor_symbol: str  # exactly as the vendor listed it — never reconstructed
    symbol: str
    expiry: date
    strike: int
    option_type: str  # 'CE' | 'PE'

    @property
    def token(self) -> str:
        return option_token(self.symbol, self.expiry, self.strike, self.option_type)

    @property
    def key(self) -> str:
        return contract_key(self.symbol, self.expiry, self.strike, self.option_type)


def parse_contract(vendor_symbol: str, symbol: str, expiry: date) -> TdContract | None:
    """Identify a vendor contract string listed under a known (symbol, expiry).

    Strike and type come from the TAIL via the same regex the backfill uses, so
    an unfamiliar expiry encoding in the middle cannot corrupt the strike. The
    prefix check guards against a chain response leaking a different underlying
    (BANKNIFTY rows inside a NIFTY chain — they share the first four characters,
    which is exactly why the check is deliberately loose and the tail is trusted).
    """
    raw = (vendor_symbol or "").strip()
    if not raw:
        return None
    tail = parse_option_tail(raw)
    if tail is None:
        return None
    strike_f, opt = tail
    opt = opt.upper()
    if opt not in _OPTION_TYPES:
        return None
    if not raw.upper().startswith(symbol.upper()[:4]):
        return None
    return TdContract(
        vendor_symbol=raw,
        symbol=symbol,
        expiry=expiry,
        strike=int(round(strike_f)),
        option_type=opt,
    )


def index_ws_name(symbol: str) -> str:
    """Vendor WS subscription name for an index spot; falls back to the symbol."""
    return INDEX_WS_NAMES.get(symbol.upper(), symbol.upper())


def continuous_future_name(symbol: str) -> str:
    """``CRUDEOIL-I`` — the rolling near-month future used as a commodity's spot."""
    return f"{symbol.upper()}{CONTINUOUS_FUTURE_SUFFIX}"


# Reference instruments carry no option identity and must NEVER be persisted as
# rows: option_oi_snapshots.option_type is CHAR(2), so 'IDX'/'FUT' overflow it,
# and the aggregator only guards against 'IDX'. The feed sets latest_underlying
# from these and returns without enqueuing.
_REF_SUFFIXES = (CONTINUOUS_FUTURE_SUFFIX,)
_INDEX_NAME_SET = {v.upper() for v in INDEX_WS_NAMES.values()}


_INDEX_SYMBOL_BY_NAME = {v.upper(): k for k, v in INDEX_WS_NAMES.items()}


def reference_symbol(vendor_symbol: str) -> str | None:
    """Underlying a reference instrument prices: ``NIFTY 50`` -> NIFTY,
    ``CRUDEOIL-I`` -> CRUDEOIL. None for anything that is not a reference."""
    up = (vendor_symbol or "").strip().upper()
    if not up:
        return None
    if up in _INDEX_SYMBOL_BY_NAME:
        return _INDEX_SYMBOL_BY_NAME[up]
    if up.endswith(CONTINUOUS_FUTURE_SUFFIX):
        return up[: -len(CONTINUOUS_FUTURE_SUFFIX)] or None
    return None


def is_reference_instrument(vendor_symbol: str) -> bool:
    """True for index spots and continuous futures — price context, not contracts."""
    up = (vendor_symbol or "").strip().upper()
    if not up:
        return False
    return up in _INDEX_NAME_SET or up.endswith(_REF_SUFFIXES)


# --------------------------------------------------------------------------
# Vendor ids <-> tokens
# --------------------------------------------------------------------------
class SymbolIdMap:
    """Bidirectional vendor-id <-> token map, rebuilt on every reconnect.

    Vendor ids are only meaningful within a session: nothing documents whether
    they are stable across reconnects, so this is cleared and repopulated from
    each ``addsymbol`` ack rather than persisted. A frame whose id is unmapped is
    counted and dropped — it is almost always a straggler from a prior
    subscription, and guessing would attribute one contract's OI to another.
    """

    __slots__ = ("_by_id", "_by_token", "_ref_by_id", "unmapped_frames")

    def __init__(self) -> None:
        self._by_id: dict[str, str] = {}      # vendor id -> td: token
        self._by_token: dict[str, str] = {}   # td: token -> vendor id
        self._ref_by_id: dict[str, str] = {}  # vendor id -> reference symbol
        self.unmapped_frames: int = 0

    def bind(self, vendor_id: str | int, token: str) -> None:
        vid = str(vendor_id)
        self._by_id[vid] = token
        self._by_token[token] = vid

    def bind_reference(self, vendor_id: str | int, vendor_symbol: str) -> None:
        self._ref_by_id[str(vendor_id)] = vendor_symbol

    def token_for(self, vendor_id: str | int) -> str | None:
        return self._by_id.get(str(vendor_id))

    def reference_for(self, vendor_id: str | int) -> str | None:
        return self._ref_by_id.get(str(vendor_id))

    def id_for(self, token: str) -> str | None:
        return self._by_token.get(token)

    def drop_token(self, token: str) -> None:
        vid = self._by_token.pop(token, None)
        if vid is not None:
            self._by_id.pop(vid, None)

    def clear(self) -> None:
        self._by_id.clear()
        self._by_token.clear()
        self._ref_by_id.clear()

    def __len__(self) -> int:
        return len(self._by_id) + len(self._ref_by_id)
