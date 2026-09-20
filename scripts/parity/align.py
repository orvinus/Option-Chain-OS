"""Timestamp-level alignment of TradingView events against our engine events
for ONE contract — pure functions over the parsed structures.

A match = same (5-minute bar, kind) after normalisation; TRAIL_SET vs
TRAIL_RAISE is accepted as the same kind (Pine labels the first rung "SET",
later ones "↑") and recorded as a LABEL diff. A matched pair whose value
differs by more than ``PRICE_TOL`` (₹0.011 — half a paisa above the vendor's
0.05 tick rounding) is a VALUE diff, except MAX_SL (the label carries the
running low, not the stop). Unmatched events are TV-only / platform-only.

Every tolerance is a documented data property, not a way to hide misses.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .tv_parse import OurEvent, TvContract, near_level, parse_levels, triple  # noqa: F401

PRICE_TOL = 0.011


def bar5(minute: str) -> str:
    """'mm-dd HH:MM' → same string floored to the 5-minute bar."""
    return minute[:-2] + f"{(int(minute[-2:]) // 5) * 5:02d}"


@dataclass
class Diff:
    kind: str          # LABEL | VALUE | TV_ONLY | PLATFORM_ONLY | TIMING
    minute: str
    detail: str
    event_kind: str
    text: str


@dataclass
class ContractAlignment:
    symbol: str
    tv_n: int
    ours_n: int
    matched: int
    diffs: list[Diff] = field(default_factory=list)
    first_structural: Optional[Diff] = None
    tv_before_first: int = 0
    truncated_capture: bool = False

    @property
    def exact(self) -> int:
        return self.matched - sum(1 for d in self.diffs if d.kind in ("LABEL", "VALUE"))


def align_events(
    symbol: str,
    tv_signals: list,
    ours: list[OurEvent],
    *,
    min_date: Optional[str] = None,
    truncated: bool = False,
) -> ContractAlignment:
    sigs = [(bar5(s.minute), s.value, s.kind, s.text) for s in tv_signals]
    ev = [(bar5(e.minute), e.value, e.kind, e.text) for e in ours]
    if min_date:
        sigs = [x for x in sigs if x[0] >= min_date]
        ev = [x for x in ev if x[0] >= min_date]
    i = j = 0
    match = 0
    diffs: list[Diff] = []
    while i < len(sigs) and j < len(ev):
        a, b = sigs[i], ev[j]
        same_kind = a[2] == b[2] or ({a[2], b[2]} == {"TRAIL_SET", "TRAIL_RAISE"})
        if a[0] == b[0] and same_kind:
            match += 1
            i += 1
            j += 1
            if a[2] != b[2]:
                diffs.append(Diff("LABEL", a[0], f"tv {a[2]} vs ours {b[2]}", a[2], a[3]))
            if abs(a[1] - b[1]) > PRICE_TOL and a[2] != "MAX_SL":
                diffs.append(Diff("VALUE", a[0], f"tv {a[1]} vs ours {b[1]}", a[2], a[3]))
        elif a[0] < b[0] or (a[0] == b[0] and a[2] < b[2]):
            diffs.append(Diff("TV_ONLY", a[0], f"{a[1]}", a[2], a[3]))
            i += 1
        else:
            diffs.append(Diff("PLATFORM_ONLY", b[0], f"{b[1]}", b[2], b[3]))
            j += 1
    for a in sigs[i:]:
        diffs.append(Diff("TV_ONLY", a[0], f"{a[1]}", a[2], a[3]))
    for b in ev[j:]:
        diffs.append(Diff("PLATFORM_ONLY", b[0], f"{b[1]}", b[2], b[3]))
    # TIMING: the same kind present on both sides within ±1 bar but unmatched
    tv_only = [d for d in diffs if d.kind == "TV_ONLY"]
    ours_only = [d for d in diffs if d.kind == "PLATFORM_ONLY"]
    used: set[int] = set()
    for d in tv_only:
        for k, o in enumerate(ours_only):
            if k in used or o.event_kind != d.event_kind:
                continue
            if _bars_apart(d.minute, o.minute) == 1:
                used.add(k)
                diffs.append(Diff("TIMING", d.minute, f"tv {d.minute} vs ours {o.minute}", d.event_kind, d.text))
                break
    first = next((d for d in diffs if d.kind in ("TV_ONLY", "PLATFORM_ONLY")), None)
    before = sum(1 for a in sigs if a[0] < first.minute) if first else 0
    return ContractAlignment(
        symbol=symbol, tv_n=len(sigs), ours_n=len(ev), matched=match, diffs=diffs,
        first_structural=first, tv_before_first=before, truncated_capture=truncated,
    )


def _bars_apart(a: str, b: str) -> int:
    def mins(s: str) -> int:
        return int(s[-5:-3]) * 60 + int(s[-2:])
    if a[:5] != b[:5]:
        return 999
    return abs(mins(a) - mins(b)) // 5


@dataclass
class StageDiff:
    daily_ok: list[bool]
    weekly_ok: bool
    h1_tv: int
    h1_ours: int
    h1_matched: int
    levels_tv: int
    levels_ours: int
    levels_matched: int


def compare_stages(tv: TvContract, dump: dict) -> StageDiff:
    dbg = tv.dbg
    daily_ok: list[bool] = []
    for i, key in enumerate(("D1", "D2", "D3")):
        tvv = triple(dbg.get(key, ""))
        our = dump["daily"][i] if i < len(dump.get("daily", [])) else None
        daily_ok.append(bool(tvv and our and all(near_level(a, b) for a, b in zip(tvv, our))))
    tvw = triple(dbg.get("W", ""))
    weekly_ok = bool(tvw and dump.get("weekly") and all(near_level(a, b) for a, b in zip(tvw, dump["weekly"])))
    tvh = [float(x) for x in dbg.get("H1", "").split(",") if x]
    ourh = dump.get("h1_struct", [])
    h1_matched = sum(1 for x in tvh if any(near_level(x, y) for y in ourh))
    tvl = parse_levels(dbg.get("LV", ""))
    ourl = [(int(t), float(p)) for t, p in dump.get("levels", [])]
    lv_matched = sum(1 for t, p in tvl if any(t == t2 and near_level(p, p2) for t2, p2 in ourl))
    return StageDiff(daily_ok, weekly_ok, len(tvh), len(ourh), h1_matched, len(tvl), len(ourl), lv_matched)
