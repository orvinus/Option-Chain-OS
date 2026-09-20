"""Internal data types shared between the WS client and the aggregator."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(slots=True)
class Tick:
    ts: datetime  # tz-aware UTC
    token: str
    symbol: str
    expiry: date
    strike: int
    option_type: str  # 'CE' | 'PE' | 'IDX' (for spot)
    # None when no price is known yet (e.g. an untraded strike, or the first frame
    # after a reconnect wiped the merged state). Persisted as NULL — never 0, which
    # would be indistinguishable from a real quote.
    ltp: float | None
    oi: int
    volume: int
    underlying: float | None = None  # filled in by aggregator from latest spot
    # Which pipeline produced this tick: "ws" (live socket) or "poller" (REST).
    # The session steward judges FEED health on WS-origin data only — REST
    # failover rows keep the dashboard alive but must never mask a dead socket.
    #
    # These two strings are COMPARED EXACTLY in aggregator._absorb and
    # _flush_closed, and `ws_rows` from that comparison is the sole input to
    # rt.last_ws_flush_at and therefore to the whole recovery ladder. Do NOT
    # extend this field with vendor-tagged values like "td_ws" — every such row
    # becomes invisible to the steward, which then sees a permanently stale feed
    # and rotates/rebuilds/exits forever. Vendor goes in `vendor`, below.
    origin: str = "ws"
    # Which VENDOR produced this tick: "xts" | "truedata". Orthogonal to origin
    # (transport) on purpose. Routes the tick to the right sink during the shadow
    # run, and lets a stray shadow tick be spotted if it ever reaches the live
    # aggregator. Never persisted — the token prefix already carries provenance.
    vendor: str = "xts"

    # Vendor's own timestamp, when it supplies one (TrueData does; XTS does not).
    # `ts` stays ARRIVAL time so bucketing semantics are unchanged across the
    # migration; this rides alongside purely so the shadow store can measure
    # feed latency, which the platform has never been able to do.
    vendor_ts: datetime | None = None
