"""Pinned chains: NIFTY and SENSEX streamed on one TrueData socket.

Every chain's index is REFERENCE (never evicted). The TRADING chains
(TRUEDATA_PINNED_SYMBOLS) put their strikes at ACTIVE; a chain that is only
VIEWED on the dashboard, and is not a trading symbol, puts its strikes at
ELASTIC — so it can never squeeze a trading chain (2026-09-23; it used to be
the other way round). A plan large enough (100+) streams every trading chain
in full. Each chain's rows carry ITS OWN index as the underlying.

Runnable without pytest:  PYTHONPATH=. python tests/test_td_pinned_chains.py
"""
from __future__ import annotations

import asyncio
from datetime import date

from app.core.config import settings
from app.ingest import atm_drift_watch
from app.ingest.subscription_budget import Priority
from app.ingest.truedata_feed import TrueDataFeedClient
from app.market import scripmaster_td
from app.market import td_identity as ident
from app.market.scripmaster_td import make_td_token
from app.services import spot_fallback

EXP_N = date(2026, 9, 22)
EXP_S = date(2026, 9, 17)


def _chain(symbol: str, expiry: date, atm: int, step: int, n: int, exchange: str) -> list:
    """2n+1 strikes x CE/PE around ``atm``."""
    out = []
    for k in range(-n, n + 1):
        strike = atm + k * step
        for ot in ("CE", "PE"):
            out.append(make_td_token(
                vendor_symbol=f"{symbol}{expiry:%y%m%d}{strike}{ot}", symbol=symbol,
                expiry=expiry, strike=strike, option_type=ot, exchange=exchange, lotsize=1,
            ))
    return out


NIFTY_CHAIN = _chain("NIFTY", EXP_N, 23300, 50, 11, "NSE")       # 46 contracts
SENSEX_CHAIN = _chain("SENSEX", EXP_S, 74500, 100, 11, "BSE")    # 46 contracts


def _feed(active: str = "NIFTY", capacity: int = 50) -> tuple[TrueDataFeedClient, asyncio.Queue]:
    q: asyncio.Queue = asyncio.Queue(maxsize=1000)

    async def _provider():
        return [], None

    f = TrueDataFeedClient(q, _provider, active_symbol=active)
    f._budget.set_capacity(capacity)
    return f, q


def _plan(f: TrueDataFeedClient, active_tokens: list, pinned: dict) -> None:
    f._active_tokens = active_tokens
    f._pinned_tokens = dict(pinned)
    f._budget.plan(f._slots_for(active_tokens))


def _symbols(f: TrueDataFeedClient) -> dict[str, int]:
    out: dict[str, int] = {}
    for s in f._budget.desired():
        out[s.symbol] = out.get(s.symbol, 0) + 1
    return out


def _drain(q: asyncio.Queue) -> list:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def _trade(vid: str, ltp: str, oi: str = "500000") -> list:
    return [vid, "2026-09-17T14:30:00", ltp, "50", ltp, "9000", ltp, ltp, ltp, ltp,
            oi, "480000", "1080000", "", "1"]


# ─────────────────────────────────────────────────────────────── planning
def test_trial_plan_keeps_the_viewed_chain_whole() -> None:
    """50 symbols - 2 margin = 48 usable: NIFTY's 47 exactly as before, and the
    one slot left goes to SENSEX's index (rank -1), never to a SENSEX strike."""
    f, _ = _feed(capacity=50)
    _plan(f, NIFTY_CHAIN, {"SENSEX": SENSEX_CHAIN})
    got = _symbols(f)
    assert got["NIFTY"] == 47, got
    assert got.get("SENSEX") == 1, got
    sensex = [s for s in f._budget.desired() if s.symbol == "SENSEX"]
    assert sensex[0].vendor_symbol == ident.index_ws_name("SENSEX")
    nifty_prios = {s.priority for s in f._budget.desired() if s.symbol == "NIFTY"}
    assert nifty_prios == {Priority.REFERENCE, Priority.ACTIVE}


def test_100_symbol_plan_streams_both_trading_chains_in_full() -> None:
    """The plan bought 2026-09-23: 98 usable slots hold NIFTY + SENSEX at
    ±11 strikes (47 + 47) with 4 to spare."""
    f, _ = _feed(capacity=100)
    _plan(f, NIFTY_CHAIN, {"SENSEX": SENSEX_CHAIN})
    assert _symbols(f) == {"NIFTY": 47, "SENSEX": 47}


BANK_CHAIN = _chain("BANKNIFTY", EXP_N, 52000, 100, 11, "NSE")   # 46 contracts


def test_viewed_non_trading_chain_never_squeezes_a_trading_chain() -> None:
    """2026-09-23: viewing BANKNIFTY on the dashboard used to outrank the
    trading chains and push SENSEX out on its own trading day. Now the
    trading chains stay whole and the viewed chain takes what is left — its
    index always, so its ATM still resolves."""
    f, _ = _feed(active="BANKNIFTY", capacity=100)
    f._spot_by_symbol.update({"NIFTY": 23300.0, "SENSEX": 74500.0, "BANKNIFTY": 52000.0})
    _plan(f, BANK_CHAIN, {"NIFTY": NIFTY_CHAIN, "SENSEX": SENSEX_CHAIN})
    got = _symbols(f)
    assert got["NIFTY"] == 47 and got["SENSEX"] == 47, got
    assert got["BANKNIFTY"] == 98 - 94, got
    kept = [s for s in f._budget.desired() if s.symbol == "BANKNIFTY"]
    assert any(s.moneyness_rank == -1 for s in kept), "the viewed chain keeps its index"
    assert {s.priority for s in kept if s.moneyness_rank >= 0} == {Priority.ELASTIC}


def test_paid_plan_streams_both_chains_in_full() -> None:
    f, _ = _feed(capacity=250)
    _plan(f, NIFTY_CHAIN, {"SENSEX": SENSEX_CHAIN})
    assert _symbols(f) == {"NIFTY": 47, "SENSEX": 47}


def test_partial_plan_thins_the_pinned_chain_symmetrically() -> None:
    """72 usable: NIFTY 47 + SENSEX index + the 24 SENSEX strikes nearest the money."""
    f, _ = _feed(capacity=74)
    f._spot_by_symbol["SENSEX"] = 74510.0
    _plan(f, NIFTY_CHAIN, {"SENSEX": SENSEX_CHAIN})
    assert _symbols(f) == {"NIFTY": 47, "SENSEX": 25}
    keys = [ident.token_to_contract_key(s.token) for s in f._budget.desired() if s.symbol == "SENSEX"]
    kept = sorted({int(k.split(":")[2]) for k in keys if k})
    assert len(kept) == 12, kept          # 24 contracts = 12 strikes x CE/PE
    assert min(kept) >= 74500 - 600 and max(kept) <= 74500 + 600, kept
    assert 74500 in kept


def test_active_symbol_is_never_also_planned_as_pinned() -> None:
    f, _ = _feed(active="SENSEX", capacity=250)
    _plan(f, SENSEX_CHAIN, {"SENSEX": SENSEX_CHAIN, "NIFTY": NIFTY_CHAIN})
    assert _symbols(f) == {"SENSEX": 47, "NIFTY": 47}
    sensex_prios = {s.priority for s in f._budget.desired() if s.symbol == "SENSEX"}
    assert sensex_prios == {Priority.REFERENCE, Priority.ACTIVE}


# ─────────────────────────────────────────────────────────── ticks/spots
def test_each_chain_carries_its_own_index_as_underlying() -> None:
    f, q = _feed(capacity=250)
    _plan(f, NIFTY_CHAIN, {"SENSEX": SENSEX_CHAIN})
    f._ids.bind_reference("900", "NIFTY 50")
    f._ids.bind_reference("901", "SENSEX")
    n_tok = ident.option_token("NIFTY", EXP_N, 23300, "CE")
    s_tok = ident.option_token("SENSEX", EXP_S, 74500, "CE")
    f._ids.bind("100", n_tok)
    f._ids.bind("101", s_tok)

    f._on_trade(_trade("900", "23282.90"))
    f._on_trade(_trade("901", "74512.40"))
    assert not _drain(q), "index frames never become rows"
    assert f.latest_underlying == 23282.90, "runtime spot = the ACTIVE symbol's index"

    f._on_trade(_trade("100", "92.40"))
    f._on_trade(_trade("101", "440.00"))
    ticks = {t.symbol: t for t in _drain(q)}
    assert ticks["NIFTY"].underlying == 23282.90
    assert ticks["SENSEX"].underlying == 74512.40
    assert ticks["SENSEX"].strike == 74500 and ticks["SENSEX"].expiry == EXP_S


def test_reference_symbol_maps_names_back_to_underlyings() -> None:
    assert ident.reference_symbol("NIFTY 50") == "NIFTY"
    assert ident.reference_symbol("sensex") == "SENSEX"
    assert ident.reference_symbol("NIFTY BANK") == "BANKNIFTY"
    assert ident.reference_symbol("CRUDEOIL-I") == "CRUDEOIL"
    assert ident.reference_symbol("NIFTY26092223300CE") is None
    assert ident.reference_symbol("") is None


def test_pinned_symbol_list_is_parsed_and_deduplicated() -> None:
    original = settings.truedata_pinned_symbols
    try:
        settings.truedata_pinned_symbols = " nifty, SENSEX ,NIFTY,, "
        assert settings.truedata_pinned_symbol_list == ["NIFTY", "SENSEX"]
        settings.truedata_pinned_symbols = ""
        assert settings.truedata_pinned_symbol_list == []
    finally:
        settings.truedata_pinned_symbols = original


# ────────────────────────────────────────────────────────── re-centring
def test_refresh_pinned_centres_recentres_and_drops() -> None:
    calls: list[tuple[str, float]] = []

    async def fake_resolve(spot, symbol=None, **_kw):
        calls.append((symbol, spot))
        return (SENSEX_CHAIN if symbol == "SENSEX" else NIFTY_CHAIN), [EXP_S]

    async def fake_window():
        return 11

    async def fake_db_spot(_symbol):
        return None

    saved = (scripmaster_td.resolve_td_option_universe, atm_drift_watch._effective_window,
             spot_fallback.db_last_underlying, settings.truedata_pinned_symbols)
    scripmaster_td.resolve_td_option_universe = fake_resolve
    atm_drift_watch._effective_window = fake_window
    spot_fallback.db_last_underlying = fake_db_spot
    settings.truedata_pinned_symbols = "NIFTY,SENSEX"
    try:
        async def go():
            f, _ = _feed(active="NIFTY", capacity=250)

            # No spot anywhere yet: subscribe the index alone, no strikes.
            assert await f._refresh_pinned() is True
            assert f._pinned_tokens == {"SENSEX": []} and calls == []

            # Index tick arrives -> the chain is centred on it.
            f._spot_by_symbol["SENSEX"] = 74500.0
            assert await f._refresh_pinned() is True
            assert calls == [("SENSEX", 74500.0)]
            assert len(f._pinned_tokens["SENSEX"]) == 46

            # Small move (< 0.4 x 11 strikes x 100 = 440 pts): nothing to do.
            f._spot_by_symbol["SENSEX"] = 74800.0
            assert await f._refresh_pinned() is False
            assert len(calls) == 1

            # Big move: re-centre.
            f._spot_by_symbol["SENSEX"] = 75000.0
            assert await f._refresh_pinned() is True
            assert calls[-1] == ("SENSEX", 75000.0)

            # Dashboard switches to SENSEX: SENSEX leaves the pinned set and
            # NIFTY (previously active) joins it.
            f._active_symbol = "SENSEX"
            f._spot_by_symbol["NIFTY"] = 23300.0
            assert await f._refresh_pinned() is True
            assert set(f._pinned_tokens) == {"NIFTY"}
            assert calls[-1] == ("NIFTY", 23300.0)
        asyncio.run(go())
    finally:
        (scripmaster_td.resolve_td_option_universe, atm_drift_watch._effective_window,
         spot_fallback.db_last_underlying, settings.truedata_pinned_symbols) = saved


def test_swap_with_no_pinned_symbols_is_the_old_single_chain() -> None:
    async def fake_window():
        return 11

    saved = (atm_drift_watch._effective_window, settings.truedata_pinned_symbols)
    atm_drift_watch._effective_window = fake_window
    settings.truedata_pinned_symbols = ""
    try:
        async def go():
            f, _ = _feed(capacity=50)
            await f.swap_subscription(NIFTY_CHAIN, None, "NIFTY", None, spot=23300.0)
            assert _symbols(f) == {"NIFTY": 47}
            assert f._pinned_tokens == {}
        asyncio.run(go())
    finally:
        atm_drift_watch._effective_window, settings.truedata_pinned_symbols = saved


def test_relay_follower_keeps_only_its_own_spot() -> None:
    from app.ingest.td_relay_feed import TdRelayFeedClient
    from app.ingest.types import Tick
    from app.ws.td_relay import tick_to_frame
    import json
    from datetime import datetime, timezone

    q: asyncio.Queue = asyncio.Queue(maxsize=10)

    async def _provider():
        return [], None

    c = TdRelayFeedClient(q, _provider, active_symbol="NIFTY")

    def frame(symbol, strike, underlying):
        t = Tick(ts=datetime.now(timezone.utc), token=f"td:{symbol}:x", symbol=symbol,
                 expiry=EXP_S, strike=strike, option_type="CE", ltp=1.0, oi=1, volume=1,
                 underlying=underlying, origin="ws", vendor="truedata")
        return json.dumps(tick_to_frame(t))

    c._on_message(frame("NIFTY", 23300, 23282.9), None)
    c._on_message(frame("SENSEX", 74500, 74512.4), None)
    assert c.latest_underlying == 23282.9
    assert q.qsize() == 2, "the follower still stores every chain"


def _run_all() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run_all()
    print("\nall pinned-chain tests passed")
