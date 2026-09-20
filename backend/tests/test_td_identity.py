"""Contract-identity tests — the guard rail on six months of archived data.

``oi_archive_bars`` already holds ~6 months of TrueData history keyed on tokens
built inside ``scripts/truedata_backfill.py``. The live TrueData feed will emit
tokens built inside ``app/market/td_identity.py``. If those two ever disagree by
so much as a zero-pad or a separator, ``oi_snapshots_unified`` silently splits
every contract's series in two: replay and the timeseries charts would show a
contract appearing to start from nothing on cutover day, with no error anywhere.

So this file pins the format against the backfill's own source, not against a
copy of the format. If someone "tidies" either side, this test fails.

Runnable without pytest:  PYTHONPATH=. python tests/test_td_identity.py
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from app.market.td_identity import (
    INDEX_WS_NAMES,
    SymbolIdMap,
    contract_key,
    continuous_future_name,
    future_token,
    index_token,
    index_ws_name,
    is_reference_instrument,
    option_token,
    parse_contract,
    token_to_contract_key,
)

BACKFILL = Path(__file__).resolve().parents[2] / "scripts" / "truedata_backfill.py"


def test_token_matches_backfill_source() -> None:
    """The backfill's f-strings are the specification; read them and compare.

    Pinning against the actual source rather than a hardcoded expectation is the
    point: a copy of the format in a test is just a second place to drift.
    """
    src = BACKFILL.read_text(encoding="utf-8")

    # Option token, at truedata_backfill.py's chain-pull site.
    assert 'f"td:{symbol}:{expiry:%y%m%d}:{strike}:{opt}"' in src, (
        "the backfill's option-token format changed — td_identity.option_token "
        "must be updated in lockstep or the archive splits from the live table"
    )
    assert 'f"td:{symbol}:IDX"' in src
    assert 'f"td:{symbol}:FUT-I"' in src

    # And now the same values, produced by the module under test.
    symbol, expiry, strike, opt = "NIFTY", date(2026, 8, 28), 24500, "CE"
    expected = f"td:{symbol}:{expiry:%y%m%d}:{strike}:{opt}"
    assert option_token(symbol, expiry, strike, opt) == expected == "td:NIFTY:260828:24500:CE"
    assert index_token(symbol) == "td:NIFTY:IDX"
    assert future_token(symbol) == "td:NIFTY:FUT-I"


def test_strike_is_never_zero_padded_or_floated() -> None:
    """A float strike would render as '24500.0' and never match the archive."""
    assert option_token("NIFTY", date(2026, 8, 28), 24500.0, "PE") == "td:NIFTY:260828:24500:PE"
    # SENSEX strikes are five digits and were the source of a real paise-scaling
    # bug in the XTS master; make sure nothing rescales them here.
    assert option_token("SENSEX", date(2026, 9, 4), 81500, "CE") == "td:SENSEX:260904:81500:CE"


def test_contract_key_round_trips_with_the_token() -> None:
    tok = option_token("NIFTY", date(2026, 8, 28), 24500, "CE")
    key = contract_key("NIFTY", date(2026, 8, 28), 24500, "CE")
    assert tok == f"td:{key}"
    assert token_to_contract_key(tok) == key
    # XTS-era numeric tokens and context rows have no contract identity.
    assert token_to_contract_key("48123") is None
    assert token_to_contract_key("td:NIFTY:IDX") is None


def test_contract_key_matches_the_sql_function() -> None:
    """Migration 0006 defines contract_key() in SQL; the shapes must agree.

    Read the migration and check the concatenation order, so a change to either
    side is caught here rather than by a cross-vendor join that silently returns
    zero rows.
    """
    mig = (
        Path(__file__).resolve().parents[1]
        / "alembic" / "versions" / "0006_td_shadow_and_overlap_fix.py"
    ).read_text(encoding="utf-8")
    assert "sym || ':' || to_char(exp, 'YYMMDD') || ':' || strk::TEXT || ':' || ot" in mig


def test_parse_contract_reads_the_tail_not_the_middle() -> None:
    """Strike/type come from the END of the vendor string.

    Anything may precede the expiry — the underlying's name, a series tag — and
    the parse is unaffected. That is what let the backfill work without the
    vendor ever documenting its naming convention.
    """
    c = parse_contract("NIFTY26082824500CE", "NIFTY", date(2026, 8, 28))
    assert c is not None
    assert (c.strike, c.option_type) == (24500, "CE")
    assert c.token == "td:NIFTY:260828:24500:CE"
    assert c.vendor_symbol == "NIFTY26082824500CE"

    # Decimal strikes (MCX) survive; the token rounds to an int like the archive.
    c2 = parse_contract("CRUDEOIL2608285600.5CE", "CRUDEOIL", date(2026, 8, 28))
    assert c2 is not None and c2.strike == 5600


def test_alternative_expiry_encoding_fails_closed() -> None:
    """A NON-yymmdd encoding must yield None, never a plausible wrong strike.

    The tail regex requires exactly six digits before the strike AND validates
    them with strptime, so 'NIFTY28AUG2624500CE' splits as ('262450', '0') ->
    month 24 -> rejected. Failing closed is the correct behaviour: a contract we
    cannot identify is one we must not subscribe, because the alternative is
    attributing its OI to the wrong strike.

    The operational consequence is a REAL RISK, not a theoretical one: if any
    segment (MCX or BSE, which the probes have not covered) names contracts
    differently, those contracts silently never enter the universe. Probe N9/B1
    must confirm the naming per segment, and the feed must log a non-zero
    unparseable count rather than treating an empty chain as normal.
    """
    assert parse_contract("NIFTY28AUG2624500CE", "NIFTY", date(2026, 8, 28)) is None
    # A greedy "trailing digits" parse would have produced strike 26082824500 here.
    assert parse_contract("NIFTY26130124500CE", "NIFTY", date(2026, 8, 28)) is None  # month 13


def test_parse_contract_rejects_foreign_and_unparseable_rows() -> None:
    assert parse_contract("", "NIFTY", date(2026, 8, 28)) is None
    assert parse_contract("RELIANCE26082824500CE", "NIFTY", date(2026, 8, 28)) is None
    assert parse_contract("NIFTY-I", "NIFTY", date(2026, 8, 28)) is None
    assert parse_contract("NIFTY26082824500XX", "NIFTY", date(2026, 8, 28)) is None


def test_reference_instruments_are_recognised() -> None:
    """These must never become rows.

    option_oi_snapshots.option_type is CHAR(2); 'IDX' and 'FUT' overflow it, and
    the aggregator only guards against 'IDX'. The feed's contract is to set
    latest_underlying from these and return without enqueuing.
    """
    assert is_reference_instrument("NIFTY 50")
    assert is_reference_instrument("NIFTY BANK")
    assert is_reference_instrument("SENSEX")
    assert is_reference_instrument("CRUDEOIL-I")
    assert is_reference_instrument("NIFTY-I")
    assert not is_reference_instrument("NIFTY26082824500CE")


def test_index_names_cover_every_registry_index() -> None:
    """Any index in the registry without a WS name would have no spot at all."""
    from app.market.symbols import get_registry

    missing = [
        e.symbol
        for e in get_registry().all()
        if e.kind == "index" and e.symbol.upper() not in INDEX_WS_NAMES
    ]
    assert not missing, f"no TrueData WS index name mapped for {missing}"
    assert index_ws_name("NIFTY") == "NIFTY 50"
    assert index_ws_name("UNKNOWNIDX") == "UNKNOWNIDX"
    assert continuous_future_name("crudeoil") == "CRUDEOIL-I"


def test_symbol_id_map_is_session_scoped() -> None:
    m = SymbolIdMap()
    m.bind(300000123, "td:NIFTY:260828:24500:CE")
    m.bind_reference(200000004, "NIFTY BANK")
    assert m.token_for("300000123") == "td:NIFTY:260828:24500:CE"
    assert m.token_for(300000123) == "td:NIFTY:260828:24500:CE"  # int or str
    assert m.reference_for(200000004) == "NIFTY BANK"
    assert m.id_for("td:NIFTY:260828:24500:CE") == "300000123"
    assert m.token_for("999") is None

    m.drop_token("td:NIFTY:260828:24500:CE")
    assert m.token_for("300000123") is None

    m.bind(1, "td:NIFTY:260828:24600:PE")
    m.clear()
    assert len(m) == 0, "a reconnect must invalidate every vendor id"


def _run_all() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run_all()
    print("\nall td_identity tests passed")
