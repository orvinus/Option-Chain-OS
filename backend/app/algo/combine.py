"""The ONE rule that turns several indicator readings into a single direction.

Signal Console Feature 1 (Combination Rule and Neutral Handling), §1.2–§1.7.

Before this module the rule was hard-coded unanimity in two places — the
orchestrator and the live on-screen strip. Both now call ``combine_readings``,
because the moment Majority became selectable a second copy would have let the
signal on screen disagree with the engine that trades.

Three states, not four
----------------------
The spec names four readings — CALL, PUT, NEUTRAL and OFF. This system only
ever carries three strings ("CALL", "PUT", "NO_TRADE") because **OFF has no
reading at all**: a disabled indicator is simply absent from the zone's
``enabled_indicators`` list, so it is never evaluated and can never vote. That
satisfies §1.6 ("OFF must be handled before NEUTRAL") structurally rather than
by a branch, and it is why §1.8's warning — never treat OFF as NEUTRAL — cannot
be violated here by accident.

"NO_TRADE" from an enabled indicator is the spec's NEUTRAL.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping, Optional, Sequence

from .config_models import CombineRule, NeutralMode

Side = Literal["CALL", "PUT"]


@dataclass(frozen=True)
class CombinedDecision:
    """``direction`` is None for the spec's NO TRADE. ``reason`` is the
    human-readable evidence that goes into the decision trace."""
    direction: Optional[Side]
    reason: str


def combine_readings(
    enabled: Sequence[str],
    readings: Mapping[str, str],
    *,
    rule: CombineRule = "unanimous",
    neutral_mode: NeutralMode = "block",
) -> CombinedDecision:
    """Apply §1.7's decision sequence to the ENABLED indicators only.

    A missing key defaults to "NO_TRADE": an enabled indicator that somehow
    produced nothing must never be silently dropped from the count.
    """
    # 1. OFF is already gone — ``enabled`` is the zone's enabled list.
    # 2. Nothing enabled at all → NO TRADE. (§14 completeness normally gates
    #    the zone long before this, but the rule must stand on its own.)
    if not enabled:
        return CombinedDecision(None, "no indicator is enabled")

    votes = [readings.get(i, "NO_TRADE") for i in enabled]
    neutrals = [v for v in votes if v not in ("CALL", "PUT")]

    # 3. Blocker mode: one enabled neutral is enough to stop everything (§1.5).
    if neutrals and neutral_mode == "block":
        n = len(neutrals)
        return CombinedDecision(
            None,
            f"{n} enabled indicator{'s are' if n > 1 else ' is'} neutral, "
            f"which blocks a decision",
        )

    # 4. Abstention mode: neutrals are ignored and the rest decide.
    live = [v for v in votes if v in ("CALL", "PUT")]

    # 5. Nothing directional left.
    if not live:
        return CombinedDecision(None, "no enabled indicator has a direction")

    call = live.count("CALL")
    put = live.count("PUT")
    # The reason has to answer "why THIS result" on its own — so say when a
    # neutral was dropped, otherwise the counts look like the whole story.
    n = len(neutrals)
    dropped = f" ({n} neutral ignored)" if n else ""

    # 6. Majority or Unanimous over what remains.
    #
    # Majority is tested explicitly and unanimity is the fallback, ON PURPOSE:
    # an unrecognised rule value (a typo, a config from a future version, a
    # caller passing "UNANIMOUS" in the wrong case) must degrade to the
    # STRICTEST behaviour. Falling through to majority instead would quietly
    # loosen the entry filter, which is the one direction a bug must never go.
    if rule == "majority":
        if call > put:
            return CombinedDecision("CALL", f"majority: {call} call against {put} put{dropped}")
        if put > call:
            return CombinedDecision("PUT", f"majority: {put} put against {call} call{dropped}")
        return CombinedDecision(None, f"majority tied {call} to {put}{dropped}")

    if call and put:
        return CombinedDecision(
            None, f"enabled indicators disagree ({call} call, {put} put){dropped}"
        )
    side: Side = "CALL" if call else "PUT"
    return CombinedDecision(
        side, f"all {len(live)} voting indicators agree ({side.lower()}){dropped}"
    )
