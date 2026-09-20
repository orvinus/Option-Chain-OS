"""Subscription-budget tests.

The XTS client's worst bug class was a silently truncated option chain: the
broker masked its ~50-instrument limit behind ambiguous errors, so the outer
strikes just quietly went missing and every wing analytic read holes without any
signal that data was absent rather than flat.

TrueData declares its ceiling up front, so truncation becomes a decision we make
rather than an accident we suffer. These tests pin the three properties that make
that decision safe: references are never evicted, truncation is symmetric around
the money, and a reconnect invalidates every acknowledgement so the replay is
complete rather than diffed against state the server may no longer hold.

Runnable without pytest:  PYTHONPATH=. python tests/test_subscription_budget.py
"""
from __future__ import annotations

from app.ingest.subscription_budget import Priority, Slot, SubscriptionBudget


def _chain(symbol: str, n_strikes: int, priority: Priority) -> list[Slot]:
    """A synthetic chain, ranked by distance from the money."""
    out: list[Slot] = []
    for rank in range(n_strikes):
        for ot in ("CE", "PE"):
            out.append(
                Slot(
                    vendor_symbol=f"{symbol}2608{24000 + rank * 50}{ot}",
                    token=f"td:{symbol}:260828:{24000 + rank * 50}:{ot}",
                    priority=priority,
                    symbol=symbol,
                    moneyness_rank=rank,
                )
            )
    return out


def _refs() -> list[Slot]:
    return [
        Slot(vendor_symbol="NIFTY 50", token="td:NIFTY:IDX",
             priority=Priority.REFERENCE, symbol="NIFTY"),
        Slot(vendor_symbol="SENSEX", token="td:SENSEX:IDX",
             priority=Priority.REFERENCE, symbol="SENSEX"),
    ]


def test_capacity_reserves_a_safety_margin() -> None:
    """An ATM re-centre briefly wants old and new edge strikes at once.

    Running at exactly maxsymbols means that overlap trips 'symbol limit reached'
    mid-swap, which is the one moment the chain must not be incomplete.
    """
    b = SubscriptionBudget()
    b.set_capacity(50)
    assert b.capacity == 48   # default margin 2

    b.set_capacity(800)
    assert b.capacity == 798


def test_tier_change_is_config_only() -> None:
    """Trial 50 -> paid 800 must need no code change; the ceiling comes from login."""
    slots = _refs() + _chain("NIFTY", 60, Priority.ACTIVE)   # 2 + 120

    trial = SubscriptionBudget()
    trial.set_capacity(50)
    trial.plan(list(slots))
    assert len(trial.desired()) == 48

    paid = SubscriptionBudget()
    paid.set_capacity(800)
    paid.plan(list(slots))
    assert len(paid.desired()) == 122, "the whole wish list fits at the paid tier"


def test_references_are_never_evicted() -> None:
    """An unpriced chain is worse than a partial one: without spot there is no ATM."""
    b = SubscriptionBudget()
    b.set_capacity(12)   # usable 10
    b.plan(_refs() + _chain("NIFTY", 40, Priority.ACTIVE))
    kept = b.desired()
    assert len(kept) == 10
    ref_tokens = {s.token for s in kept if s.priority is Priority.REFERENCE}
    assert ref_tokens == {"td:NIFTY:IDX", "td:SENSEX:IDX"}


def test_truncation_is_symmetric_around_the_money() -> None:
    """A squeeze must thin both wings, not amputate one.

    Sorting by moneyness_rank keeps the innermost strikes; a naive sort by symbol
    or token would keep whichever half sorts first and leave the chain lopsided.
    """
    b = SubscriptionBudget()
    b.set_capacity(10)   # usable 8 -> 4 strikes x 2 legs
    b.plan(_chain("NIFTY", 20, Priority.ACTIVE))
    kept = b.desired()
    assert len(kept) == 8
    ranks = sorted({s.moneyness_rank for s in kept})
    assert ranks == [0, 1, 2, 3], f"kept the wrong strikes: {ranks}"
    # Both legs survive at every kept strike.
    for rank in ranks:
        legs = {s.token.rsplit(":", 1)[1] for s in kept if s.moneyness_rank == rank}
        assert legs == {"CE", "PE"}, f"rank {rank} lost a leg: {legs}"


def test_active_chain_outranks_elastic_watchlist() -> None:
    b = SubscriptionBudget()
    b.set_capacity(14)   # usable 12
    b.plan(
        _refs()
        + _chain("NIFTY", 4, Priority.ACTIVE)        # 8
        + _chain("BANKNIFTY", 10, Priority.ELASTIC)  # 20
    )
    kept = b.desired()
    assert len(kept) == 12
    assert sum(1 for s in kept if s.priority is Priority.REFERENCE) == 2
    assert sum(1 for s in kept if s.priority is Priority.ACTIVE) == 8
    assert sum(1 for s in kept if s.priority is Priority.ELASTIC) == 2


def test_truncation_is_reported_not_silent() -> None:
    b = SubscriptionBudget()
    b.set_capacity(10)
    b.plan(_chain("NIFTY", 20, Priority.ACTIVE))
    st = b.state()
    assert st.capacity == 8
    assert st.dropped == 32, "the operator must be able to see what was dropped"


def test_diff_produces_add_and_remove_sets() -> None:
    b = SubscriptionBudget()
    b.set_capacity(100)
    first = _chain("NIFTY", 3, Priority.ACTIVE)
    b.plan(first)
    add, remove = b.diff()
    assert len(add) == 6 and not remove

    b.mark_live([s.token for s in add])
    assert b.live_count == 6

    # An ATM re-centre: the window slides by one strike.
    second = _chain("NIFTY", 3, Priority.ACTIVE)[2:] + _chain("NIFTY", 5, Priority.ACTIVE)[6:]
    b.plan(second)
    add, remove = b.diff()
    assert add or remove, "a shifted window must produce a diff"
    assert all(t not in {s.token for s in b.desired()} for t in remove)


def test_reconnect_invalidates_every_ack() -> None:
    """Nothing documents whether subscriptions survive a reconnect, so assume not.

    Diffing against stale acks after a reconnect would leave the feed believing
    it is subscribed to contracts the server has forgotten — a silent, permanent
    hole in the chain that no error surfaces.
    """
    b = SubscriptionBudget()
    b.set_capacity(100)
    slots = _chain("NIFTY", 3, Priority.ACTIVE)
    b.plan(slots)
    b.mark_live([s.token for s in slots])
    assert b.live_count == 6

    b.reset_live()
    assert b.live_count == 0
    add, remove = b.diff()
    assert len(add) == 6 and not remove, "the full desired set must be replayed"


def test_no_capacity_yet_keeps_the_full_wish_list() -> None:
    """Before login we do not know the ceiling; nothing is subscribed yet anyway."""
    b = SubscriptionBudget()
    slots = _chain("NIFTY", 50, Priority.ACTIVE)
    b.plan(slots)
    assert len(b.desired()) == 100


def _run_all() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run_all()
    print("\nall subscription_budget tests passed")
