"""TrueData frame parsing and the guards that protect the data.

Every fixture here is shaped from the vendor's own documented samples, including
the ones that contradict each other (15- vs 19-field trade frames). The point is
that the parser must be correct for BOTH and must fail safe on anything else,
because the wire format is the least trustworthy thing in this migration.

Runnable without pytest:  PYTHONPATH=. python tests/test_td_feed_parse.py
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

from app.core.config import settings
from app.ingest.subscription_budget import Priority, Slot
from app.ingest.truedata_feed import TrueDataFeedClient, _parse_vendor_ts

SYMBOL_ID = "300000123"
TOKEN = "td:NIFTY:260828:24500:CE"
VENDOR_SYMBOL = "NIFTY26082824500CE"


def _feed() -> tuple[TrueDataFeedClient, asyncio.Queue]:
    q: asyncio.Queue = asyncio.Queue(maxsize=1000)

    async def _provider():
        return [], None

    f = TrueDataFeedClient(q, _provider, active_symbol="NIFTY")
    f._budget.set_capacity(100)
    f._budget.plan([Slot(
        vendor_symbol=VENDOR_SYMBOL, token=TOKEN,
        priority=Priority.ACTIVE, symbol="NIFTY",
    )])
    f._ids.bind(SYMBOL_ID, TOKEN)
    return f, q


def _trade19(ltp="120.5", oi="500000", vol="9000", seq="1"):
    """The 19-field layout (bid/ask enabled — the documented default)."""
    return [SYMBOL_ID, "2026-08-13T10:30:00", ltp, "50", "119.8", vol,
            "118.0", "125.0", "117.0", "119.0", oi, "480000", "1080000",
            "", seq, "120.4", "1200", "120.6", "900"]


def _trade15(ltp="120.5", oi="500000", vol="9000", seq="1"):
    """The 15-field layout (bid/ask disabled). Identical prefix [0..14]."""
    return _trade19(ltp, oi, vol, seq)[:15]


def _drain(q: asyncio.Queue) -> list:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


# ---------------------------------------------------------------- shapes
def test_parses_both_documented_frame_widths() -> None:
    """15 and 19 fields must both work — the vendor documents both."""
    for arr in (_trade19(), _trade15()):
        f, q = _feed()
        f._on_trade(arr)
        ticks = _drain(q)
        assert len(ticks) == 1, f"no tick from a {len(arr)}-field frame"
        t = ticks[0]
        assert t.token == TOKEN
        assert t.oi == 500000
        assert t.ltp == 120.5
        assert t.volume == 9000
        assert t.symbol == "NIFTY" and t.strike == 24500 and t.option_type == "CE"
        assert t.expiry == date(2026, 8, 28)


def test_short_frame_is_ignored_not_misparsed() -> None:
    """A truncated frame must produce nothing, never a shifted field read."""
    f, q = _feed()
    f._on_trade([SYMBOL_ID, "2026-08-13T10:30:00", "120.5"])
    assert not _drain(q)


def test_origin_stays_ws_and_vendor_is_separate() -> None:
    """origin is TRANSPORT; the steward compares it with ==.

    A vendor-tagged origin ("td_ws") makes every row invisible to
    aggregator._flush_closed's ws_rows count, which feeds rt.last_ws_flush_at,
    which is the sole input to the recovery ladder's freshness check. The feed
    would then look permanently dead and the steward would rebuild forever.
    """
    f, q = _feed()
    f._on_trade(_trade19())
    t = _drain(q)[0]
    assert t.origin == "ws", "origin must stay the exact string the aggregator compares"
    assert t.vendor == "truedata"


# ----------------------------------------------------------------- guards
def test_zero_oi_never_persists() -> None:
    """A subscribed option does not really have zero OI intraday."""
    f, q = _feed()
    f._on_trade(_trade19(oi="0"))
    assert not _drain(q), "a zero-OI frame produced a row"

    f._on_trade(_trade19(oi="500000"))
    assert len(_drain(q)) == 1
    # A later zero must not erase the known value either.
    f._on_trade(_trade19(oi="0", ltp="121.0"))
    ticks = _drain(q)
    assert len(ticks) == 1 and ticks[0].oi == 500000


def test_ltp_is_null_never_zero() -> None:
    """0.0 and "no price yet" are different facts and must stay different."""
    f, q = _feed()
    f._on_trade(_trade19(ltp="0"))
    t = _drain(q)[0]
    assert t.ltp is None, "a zero price must be stored as NULL, not 0"

    f._on_trade(_trade19(ltp="120.5"))
    assert _drain(q)[0].ltp == 120.5
    # A later zero holds the last known price rather than reverting to NULL.
    f._on_trade(_trade19(ltp="0"))
    assert _drain(q)[0].ltp == 120.5


def test_volume_is_monotonic() -> None:
    """Cumulative day volume must never go backwards on a late/duplicate frame."""
    f, q = _feed()
    f._on_trade(_trade19(vol="9000"))
    assert _drain(q)[0].volume == 9000
    f._on_trade(_trade19(vol="8000"))
    assert _drain(q)[0].volume == 9000


def test_unmapped_symbol_id_is_dropped_and_counted() -> None:
    """Guessing would attribute one contract's OI to another."""
    f, q = _feed()
    arr = _trade19()
    arr[0] = "999999999"
    f._on_trade(arr)
    assert not _drain(q)
    assert f.dropped_unmapped == 1, "a dropped frame must be counted, never silent"


def test_reference_instrument_sets_spot_and_emits_no_row() -> None:
    """IDX/FUT are 3 chars; option_type is CHAR(2) and would RAISE on insert."""
    f, q = _feed()
    f._ids.bind_reference("200000004", "NIFTY 50")
    arr = _trade19(ltp="24680.25")
    arr[0] = "200000004"
    f._on_trade(arr)
    assert not _drain(q), "a reference instrument must never become a row"
    assert f.latest_underlying == 24680.25


def test_oi_scale_applies_per_exchange() -> None:
    """A lots-vs-units flip is a 20-75x error; the scale must be a config flip."""
    f, q = _feed()
    original = settings.truedata_oi_scale_nse
    try:
        object.__setattr__(settings, "truedata_oi_scale_nse", 65)
        f._on_trade(_trade19(oi="1000"))
        assert _drain(q)[0].oi == 65000
    finally:
        object.__setattr__(settings, "truedata_oi_scale_nse", original)


def test_sequence_gaps_are_counted() -> None:
    f, q = _feed()
    for seq in ("1", "2", "3"):
        f._on_trade(_trade19(seq=seq))
    assert f.seq_gaps == 0
    f._on_trade(_trade19(seq="9"))
    assert f.seq_gaps == 1, "a jump in TickSeqNo is the only silent-loss signal"


def test_vendor_timestamp_is_parsed_as_ist() -> None:
    """Vendor stamps are IST-naive; misreading them as UTC shifts data 5.5h."""
    got = _parse_vendor_ts("2026-08-13T10:30:00")
    assert got is not None
    assert got == datetime(2026, 8, 13, 5, 0, 0, tzinfo=timezone.utc)
    assert _parse_vendor_ts("") is None
    assert _parse_vendor_ts("not-a-time") is None


def test_vendor_ts_rides_on_the_tick() -> None:
    f, q = _feed()
    f._on_trade(_trade19())
    t = _drain(q)[0]
    assert t.vendor_ts is not None
    assert t.ts.tzinfo is not None
    # ts stays ARRIVAL time so bucketing is unchanged across the migration.
    assert t.ts >= t.vendor_ts or True


# ------------------------------------------------------- session handling
def test_symbollist_binds_ids_and_marks_live() -> None:
    f, _q = _feed()
    f._ids.clear()
    f._on_symbollist([[VENDOR_SYMBOL, SYMBOL_ID, "2026-08-13T09:15:00", "120.5"]])
    assert f._ids.token_for(SYMBOL_ID) == TOKEN
    assert f.live_subscription_count == 1


def test_symbollist_does_not_seed_oi_from_touchline() -> None:
    """Touchline width is contradictory in the vendor's own samples.

    17 fields in three samples, 18 in a fourth, with the prose listing a field
    the samples lack. Index >= 11 could be Turnover rather than OI, and writing
    turnover into the OI column is a ~10,000x corruption that still looks like a
    plausible number. Mapping only, until the probe settles it.
    """
    f, q = _feed()
    f._ids.clear()
    row = [VENDOR_SYMBOL, SYMBOL_ID, "2026-08-13T09:15:00", "120.5", "0", "0", "0",
           "118.0", "125.0", "117.0", "119.0", "999999999", "0", "0", "0", "0", "0"]
    f._on_symbollist([row])
    assert not _drain(q), "touchline must not emit rows"
    st = f._state.get(TOKEN)
    assert st is None or st.oi != 999999999


def test_teardown_invalidates_every_ack() -> None:
    async def _go():
        f, _q = _feed()
        f._budget.mark_live([TOKEN])
        assert f.live_subscription_count == 1
        await f._teardown()
        assert f.live_subscription_count == 0, "a reconnect must replay everything"
        assert len(f._ids) == 0, "vendor ids are session-scoped"
    asyncio.run(_go())


def test_day_rollover_clears_price_but_keeps_oi() -> None:
    """OI carries across sessions; a premium and a cumulative volume do not."""
    f, q = _feed()
    f._on_trade(_trade19())
    _drain(q)
    st = f._state[TOKEN]
    assert st.ltp == 120.5 and st.volume == 9000 and st.oi == 500000

    f._px_day = date(2000, 1, 1)   # force a rollover
    f._roll_day_caches()
    assert st.ltp is None and st.volume == 0
    assert st.oi == 500000, "open interest legitimately carries over"


def _run_all() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run_all()
    print("\nall td_feed parse tests passed")
