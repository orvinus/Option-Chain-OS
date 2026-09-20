"""Slot allocator for the TrueData symbol budget.

TrueData caps concurrent subscriptions per account (``maxsymbols`` in the login
response — 50 on the trial, 250/800/900 on documented paid tiers). Unlike the XTS
~50-cap, which was undocumented and had to be inferred from masked error strings,
this one is declared up front. So instead of discovering truncation after the
fact, we allocate against a known ceiling and never ask for more than we hold.

Two responsibilities:

1. **Allocate.** Reserved slots (index spots and the active chain) can never be
   evicted; elastic slots (watchlist chains) are filled with what remains and
   truncated SYMMETRICALLY around the money, so a budget squeeze thins the wings
   evenly rather than lopping off one side of the chain.

2. **Replay.** Nothing in TrueData's documentation says whether subscriptions
   survive a reconnect, so we assume they do not. This class is the single source
   of truth for what SHOULD be subscribed; after every reconnect the feed replays
   ``desired()`` wholesale. That also makes ``swap_subscription`` a set diff
   (add = desired − live, remove = live − desired) instead of the XTS client's
   unsubscribe-then-resubscribe dance.

Deliberately tier-agnostic: capacity arrives at login. Moving from the 50-symbol
trial to an 800-symbol plan is a config event, not a code change — which is the
whole reason STRIKE_WINDOW can finally rise above the 11 that the XTS cap forced.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

from ..core.config import settings
from ..core.logging import get_logger

log = get_logger("sub_budget")

# When the login reply declares no ceiling at all, assume the documented
# TrueData trial cap rather than disabling the allocator (see set_capacity).
_UNKNOWN_CAPACITY_FLOOR = 50


class Priority(IntEnum):
    """Lower value = evicted last."""

    REFERENCE = 0  # index spot / continuous future — without these there is no ATM
    ACTIVE = 1     # the chain the user is looking at
    ELASTIC = 2    # watchlist / pre-warmed chains


@dataclass(frozen=True, slots=True)
class Slot:
    """One subscribable vendor symbol."""

    vendor_symbol: str
    token: str            # 'td:...' for options; the reference name for spots/futures
    priority: Priority
    symbol: str           # underlying, for logging and per-symbol accounting
    # Distance from the money in strike steps. Only meaningful for options; drives
    # symmetric truncation so the surviving window stays centred.
    moneyness_rank: int = 0


@dataclass
class BudgetState:
    capacity: int = 0
    reserved: int = 0
    elastic: int = 0
    dropped: int = 0
    live: int = 0


class SubscriptionBudget:
    """Desired-state ledger for the live subscription set."""

    def __init__(self) -> None:
        self._capacity: int = 0
        self._desired: dict[str, Slot] = {}   # token -> Slot
        self._live: set[str] = set()          # tokens the vendor has ACKed
        self._last_dropped: int = 0
        # token -> vendor symbol for every slot ever planned. Needed because a
        # to-REMOVE token is by definition no longer in _desired, so its wire
        # name must come from here (sending internal 'td:…' tokens in
        # removesymbol was a silent no-op — subscription leak, 2026-08-18).
        self._vendor_by_token: dict[str, str] = {}

    # ---------------- capacity ----------------
    def set_capacity(self, maxsymbols: int) -> None:
        """Adopt the ceiling the vendor declared at login.

        A safety margin is held back so an ATM re-centre (which briefly wants the
        old and new edge strikes simultaneously) can never trip 'symbol limit
        reached' mid-swap. The override exists only to pin a LOWER ceiling — e.g.
        to leave room for a second consumer on the same account.
        """
        declared = int(maxsymbols or 0)
        override = int(settings.truedata_maxsymbols_override or 0)
        if override > 0:
            declared = min(declared, override) if declared > 0 else override
        if declared <= 0:
            # The login reply carried no usable ceiling (observed live:
            # maxsymbols=0 on the trial). 0 must mean UNKNOWN, never
            # "unlimited" — with capacity 0 the allocator short-circuits and
            # every symmetric-truncation safeguard is silently disabled, the
            # exact XTS failure mode this class exists to prevent. Assume the
            # documented trial floor; no margin subtraction on a guess that
            # is already conservative (a margin here could cut strikes that
            # demonstrably subscribe fine today).
            log.error(
                "budget.capacity_unknown",
                declared=declared,
                assuming=_UNKNOWN_CAPACITY_FLOOR,
                hint="login reply had no maxsymbols — set TRUEDATA_MAXSYMBOLS_OVERRIDE to pin the real plan ceiling",
            )
            cap = _UNKNOWN_CAPACITY_FLOOR
        else:
            cap = max(0, declared - max(0, int(settings.truedata_budget_safety_margin)))
        if cap != self._capacity:
            log.info(
                "budget.capacity",
                declared=declared,
                usable=cap,
                margin=settings.truedata_budget_safety_margin,
            )
        self._capacity = cap

    @property
    def capacity(self) -> int:
        return self._capacity

    # ---------------- desired state ----------------
    def plan(self, slots: list[Slot]) -> None:
        """Replace the desired set, truncating to capacity by priority.

        Called on every symbol switch and ATM re-centre. Truncation is reported,
        never silent: the XTS client's hardest bug class was a chain quietly
        missing its outer strikes because the broker masked the limit error.
        """
        for s in slots:
            self._vendor_by_token[s.token] = s.vendor_symbol
        self._desired = self._allocate(slots)
        # Bound the name map: keep entries for anything desired OR still live
        # (a live-but-undesired token is exactly what diff() must unsubscribe).
        keep = set(self._desired) | self._live
        for t in list(self._vendor_by_token):
            if t not in keep:
                del self._vendor_by_token[t]

    def _allocate(self, slots: list[Slot]) -> dict[str, Slot]:
        if self._capacity <= 0:
            # Pre-login: keep the full wish list so the first post-login replay is
            # complete. Nothing is subscribed until capacity is known anyway.
            return {s.token: s for s in slots}

        by_priority: dict[Priority, list[Slot]] = {}
        for s in slots:
            by_priority.setdefault(s.priority, []).append(s)

        kept: dict[str, Slot] = {}
        remaining = self._capacity
        dropped = 0

        for prio in sorted(by_priority):
            group = by_priority[prio]
            if len(group) <= remaining:
                for s in group:
                    kept[s.token] = s
                remaining -= len(group)
                continue

            if prio is Priority.REFERENCE:
                # Should be impossible (a handful of spots against any real tier),
                # but if it ever happens, an unpriced chain is worse than a partial
                # one — take references and let the chain starve.
                for s in group[:remaining]:
                    kept[s.token] = s
                dropped += len(group) - remaining
                remaining = 0
                log.error("budget.reference_truncated", wanted=len(group), capacity=self._capacity)
                continue

            # Symmetric truncation: keep the innermost `remaining` strikes so the
            # window stays centred on the money instead of losing one whole wing.
            group.sort(key=lambda s: (s.moneyness_rank, s.vendor_symbol))
            for s in group[:remaining]:
                kept[s.token] = s
            dropped += len(group) - remaining
            log.warning(
                "budget.truncated",
                priority=prio.name,
                wanted=len(group),
                kept=remaining,
                dropped=len(group) - remaining,
            )
            remaining = 0

        self._last_dropped = dropped
        return kept

    def desired(self) -> list[Slot]:
        return list(self._desired.values())

    def desired_symbols(self) -> list[str]:
        """The vendor strings to send in ``addsymbol`` — the replay payload."""
        return [s.vendor_symbol for s in self._desired.values()]

    # ---------------- diffing ----------------
    def diff(self) -> tuple[list[Slot], list[tuple[str, str]]]:
        """(to_add slots, to_remove (token, VENDOR_SYMBOL) pairs) against what
        the vendor has ACKed. The vendor symbol is what goes on the wire in
        ``removesymbol``; the token is what ``mark_dropped`` bookkeeps. The old
        return (bare tokens) made every unsubscribe a silent vendor-side no-op."""
        want = set(self._desired)
        add = [self._desired[t] for t in want - self._live]
        remove = [
            (t, self._vendor_by_token.get(t, t)) for t in self._live - want
        ]
        return add, remove

    # ---------------- ack tracking ----------------
    def mark_live(self, tokens: list[str]) -> None:
        self._live.update(tokens)

    def mark_dropped(self, tokens: list[str]) -> None:
        self._live.difference_update(tokens)

    def reset_live(self) -> None:
        """A reconnect invalidates every acknowledgement.

        Called on socket teardown so the next connect replays the FULL desired set
        rather than diffing against subscriptions the server may no longer hold.
        """
        self._live.clear()

    def live_tokens(self) -> list[str]:
        return list(self._live)

    @property
    def live_count(self) -> int:
        return len(self._live)

    def state(self) -> BudgetState:
        reserved = sum(
            1 for s in self._desired.values() if s.priority <= Priority.ACTIVE
        )
        return BudgetState(
            capacity=self._capacity,
            reserved=reserved,
            elastic=len(self._desired) - reserved,
            dropped=self._last_dropped,
            live=len(self._live),
        )
