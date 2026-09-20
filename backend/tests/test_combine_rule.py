"""Signal Console Feature 1 — combination rule and neutral handling.

The spec's worked examples (§1.2–§1.7) turned into executable checks, plus the
two properties that matter most to this codebase: the defaults must reproduce
the pre-feature hard-coded unanimity exactly, and the live on-screen strip must
never be able to disagree with the engine that trades.
"""
from __future__ import annotations

import pytest

from app.algo.combine import combine_readings
from app.algo.config_models import ZoneConfig

ALL3 = ["oi_change", "multi_tf", "ratio"]


def _d(enabled, readings, rule="unanimous", neutral_mode="block"):
    return combine_readings(enabled, readings, rule=rule, neutral_mode=neutral_mode)


def _r(*vals):
    """Positional readings for oi_change / multi_tf / ratio."""
    return dict(zip(ALL3, vals))


# ── §1.3 the Majority table, verbatim ─────────────────────────────────────

@pytest.mark.parametrize(
    "readings, enabled, expected",
    [
        (_r("CALL", "CALL", "CALL"), ALL3, "CALL"),                 # 3-0
        (_r("CALL", "CALL", "PUT"), ALL3, "CALL"),                  # 2-1
        (_r("CALL", "PUT", "PUT"), ALL3, "PUT"),                    # 1-2
        (_r("CALL", "PUT", "x"), ALL3[:2], None),                   # 1-1 tie, third OFF
        (_r("CALL", "x", "x"), ALL3[:1], "CALL"),                   # 1-0, two OFF
    ],
)
def test_majority_table(readings, enabled, expected):
    assert _d(enabled, readings, rule="majority").direction == expected


# ── §1.4 Unanimous, and how it differs ────────────────────────────────────

def test_example_3_setting_changes_the_meaning_of_disagreement():
    readings = _r("CALL", "CALL", "PUT")
    assert _d(ALL3, readings, rule="majority").direction == "CALL"
    assert _d(ALL3, readings, rule="unanimous").direction is None


def test_example_4_off_does_not_participate_under_either_rule():
    # CALL / CALL / OFF → CALL both ways: two enabled, both agree.
    readings = _r("CALL", "CALL", "ignored-because-off")
    for rule in ("majority", "unanimous"):
        assert _d(ALL3[:2], readings, rule=rule).direction == "CALL"


# ── §1.5 NEUTRAL is an abstention or a blocker ────────────────────────────

def test_example_5_neutral_plus_two_call():
    readings = _r("NO_TRADE", "CALL", "CALL")
    assert _d(ALL3, readings, neutral_mode="abstain").direction == "CALL"
    assert _d(ALL3, readings, neutral_mode="block").direction is None


def test_example_6_neutral_plus_call_plus_put():
    readings = _r("NO_TRADE", "CALL", "PUT")
    # Abstain: the survivors tie under majority.
    assert _d(ALL3, readings, rule="majority", neutral_mode="abstain").direction is None
    # Block: neutral stops it before the tie is even reached.
    d = _d(ALL3, readings, rule="majority", neutral_mode="block")
    assert d.direction is None and "neutral" in d.reason


# ── §1.6 the OFF/NEUTRAL table ────────────────────────────────────────────

@pytest.mark.parametrize(
    "enabled, readings, mode, expected",
    [
        (["oi_change"], {"oi_change": "CALL"}, "abstain", "CALL"),
        (["oi_change"], {"oi_change": "NO_TRADE"}, "abstain", None),
        (["oi_change", "multi_tf"], _r("CALL", "PUT"), "abstain", None),
        ([], {}, "abstain", None),
        (ALL3, _r("NO_TRADE", "CALL", "CALL"), "abstain", "CALL"),
        (ALL3, _r("NO_TRADE", "CALL", "CALL"), "block", None),
        (ALL3, _r("NO_TRADE", "NO_TRADE", "CALL"), "abstain", "CALL"),
        (ALL3, _r("NO_TRADE", "NO_TRADE", "CALL"), "block", None),
        (ALL3, _r("NO_TRADE", "NO_TRADE", "NO_TRADE"), "abstain", None),
    ],
)
def test_off_and_neutral_table(enabled, readings, mode, expected):
    assert _d(enabled, readings, rule="majority", neutral_mode=mode).direction == expected


def test_off_is_never_confused_with_neutral(self=None):
    """§1.8's warning. Disabling an indicator must not create a NO TRADE the
    way an enabled neutral one does."""
    both_call = _r("CALL", "CALL", "NO_TRADE")
    # 'ratio' ENABLED and neutral, blocker mode → blocked.
    assert _d(ALL3, both_call, neutral_mode="block").direction is None
    # The same readings with 'ratio' switched OFF → it votes not at all.
    assert _d(ALL3[:2], both_call, neutral_mode="block").direction == "CALL"


# ── the properties this codebase cares about ──────────────────────────────

def test_zone_defaults_reproduce_the_pre_feature_rule():
    z = ZoneConfig(start="09:20", end="10:00")
    assert z.combine_rule == "unanimous" and z.neutral_mode == "block"

    def old_hard_coded_rule(enabled, readings):
        vals = [readings.get(i, "NO_TRADE") for i in enabled]
        if not vals or vals[0] not in ("CALL", "PUT"):
            return None
        return vals[0] if all(v == vals[0] for v in vals) else None

    sides = ("CALL", "PUT", "NO_TRADE")
    for a in sides:
        for b in sides:
            for c in sides:
                for enabled in (ALL3, ALL3[:2], ALL3[:1], []):
                    readings = _r(a, b, c)
                    assert (
                        _d(enabled, readings, rule=z.combine_rule,
                           neutral_mode=z.neutral_mode).direction
                        == old_hard_coded_rule(enabled, readings)
                    ), (enabled, readings)


def test_missing_reading_counts_as_neutral_not_as_absent():
    # An enabled indicator that produced nothing must not be silently dropped.
    d = _d(ALL3, {"oi_change": "CALL", "multi_tf": "CALL"}, neutral_mode="block")
    assert d.direction is None and "neutral" in d.reason


def test_reasons_are_specific_enough_for_the_trace():
    assert "disagree" in _d(ALL3, _r("CALL", "CALL", "PUT")).reason
    assert "tied" in _d(ALL3[:2], _r("CALL", "PUT"), rule="majority").reason
    assert "no indicator is enabled" == _d([], {}).reason
    assert "agree" in _d(ALL3, _r("PUT", "PUT", "PUT")).reason


# ── the developer instruction's own matrix, verbatim ──────────────────────
#
# "Combined Decision & Neutral Reading Rules" (2026-09-19). Written in the
# spec's own vocabulary — OFF/NEUTRAL/CALL/PUT and ABSTAIN/BLOCK — and
# translated at the boundary, so the table below can be diffed against the
# document line by line without decoding our internal spellings.

def _spec(mtf: str, qae: str, smc: str, neutral_mode: str, rule: str) -> str:
    enabled, readings = [], {}
    for key, val in (("multi_tf", mtf), ("ratio", qae), ("oi_change", smc)):
        if val == "OFF":
            continue                      # OFF never enters the combination
        enabled.append(key)
        readings[key] = "NO_TRADE" if val == "NEUTRAL" else val
    d = combine_readings(
        enabled, readings,
        rule=rule.lower(),
        neutral_mode="abstain" if neutral_mode == "ABSTAIN" else "block",
    )
    return d.direction or "NO TRADE"


SPEC_MATRIX = [
    # §13 Complete Example Matrix — all sixteen rows
    ("CALL", "CALL", "CALL", "ABSTAIN", "MAJORITY", "CALL"),
    ("CALL", "CALL", "PUT", "ABSTAIN", "MAJORITY", "CALL"),
    ("CALL", "PUT", "PUT", "ABSTAIN", "MAJORITY", "PUT"),
    ("CALL", "PUT", "NEUTRAL", "ABSTAIN", "MAJORITY", "NO TRADE"),
    ("CALL", "CALL", "NEUTRAL", "ABSTAIN", "MAJORITY", "CALL"),
    ("CALL", "CALL", "NEUTRAL", "BLOCK", "MAJORITY", "NO TRADE"),
    ("PUT", "PUT", "NEUTRAL", "BLOCK", "MAJORITY", "NO TRADE"),
    ("CALL", "OFF", "CALL", "ABSTAIN", "MAJORITY", "CALL"),
    ("CALL", "OFF", "PUT", "ABSTAIN", "MAJORITY", "NO TRADE"),
    ("OFF", "OFF", "CALL", "ABSTAIN", "MAJORITY", "CALL"),
    ("OFF", "OFF", "OFF", "ABSTAIN", "MAJORITY", "NO TRADE"),
    ("CALL", "CALL", "CALL", "ABSTAIN", "UNANIMOUS", "CALL"),
    ("PUT", "PUT", "PUT", "ABSTAIN", "UNANIMOUS", "PUT"),
    ("CALL", "CALL", "PUT", "ABSTAIN", "UNANIMOUS", "NO TRADE"),
    ("CALL", "OFF", "CALL", "ABSTAIN", "UNANIMOUS", "CALL"),
    ("CALL", "OFF", "PUT", "ABSTAIN", "UNANIMOUS", "NO TRADE"),
    # §2 Examples 1–3 — OFF is removed first
    ("CALL", "CALL", "OFF", "ABSTAIN", "MAJORITY", "CALL"),
    ("CALL", "OFF", "PUT", "ABSTAIN", "MAJORITY", "NO TRADE"),
    ("OFF", "OFF", "CALL", "ABSTAIN", "MAJORITY", "CALL"),
    # §4 Examples 4–6 — ABSTAIN drops NEUTRAL before counting
    ("CALL", "CALL", "NEUTRAL", "ABSTAIN", "MAJORITY", "CALL"),
    ("CALL", "NEUTRAL", "PUT", "ABSTAIN", "MAJORITY", "NO TRADE"),
    ("PUT", "NEUTRAL", "PUT", "ABSTAIN", "MAJORITY", "PUT"),
    # §5 Examples 7–9 — BLOCK stops before any counting
    ("CALL", "CALL", "NEUTRAL", "BLOCK", "MAJORITY", "NO TRADE"),
    ("PUT", "PUT", "NEUTRAL", "BLOCK", "MAJORITY", "NO TRADE"),
    ("CALL", "PUT", "NEUTRAL", "BLOCK", "MAJORITY", "NO TRADE"),
    # §7 Examples 10–13
    ("CALL", "PUT", "NEUTRAL", "BLOCK", "UNANIMOUS", "NO TRADE"),
    # §9 every indicator OFF, and §10 nothing directional left
    ("OFF", "OFF", "OFF", "ABSTAIN", "UNANIMOUS", "NO TRADE"),
    ("NEUTRAL", "NEUTRAL", "OFF", "ABSTAIN", "MAJORITY", "NO TRADE"),
    ("NEUTRAL", "NEUTRAL", "OFF", "ABSTAIN", "UNANIMOUS", "NO TRADE"),
    ("NEUTRAL", "NEUTRAL", "NEUTRAL", "ABSTAIN", "MAJORITY", "NO TRADE"),
]


@pytest.mark.parametrize("mtf,qae,smc,mode,rule,expected", SPEC_MATRIX)
def test_developer_instruction_matrix(mtf, qae, smc, mode, rule, expected):
    assert _spec(mtf, qae, smc, mode, rule) == expected


@pytest.mark.parametrize("state,abstain,block", [
    ("OFF", "CALL", "CALL"),        # ignored under both
    ("NEUTRAL", "CALL", "NO TRADE"),  # ignored vs blocks
    ("CALL", "CALL", "CALL"),       # counted under both
    ("PUT", "CALL", "CALL"),        # counted under both (2 call vs 1 put)
])
def test_section_11_state_table(state, abstain, block):
    """§11: the four states, and the one row where OFF and NEUTRAL differ."""
    assert _spec("CALL", state, "CALL", "ABSTAIN", "MAJORITY") == abstain
    assert _spec("CALL", state, "CALL", "BLOCK", "MAJORITY") == block


def test_an_unknown_rule_degrades_to_the_strictest_behaviour():
    """A typo, a wrong case, or a config from a future version must never
    quietly LOOSEN the entry filter into majority."""
    for bogus in ("UNANIMOUS", "Majority", "", "quorum", "unknown"):
        assert combine_readings(ALL3, _r("CALL", "CALL", "PUT"), rule=bogus).direction is None
    # ...while the one exact spelling that means majority still works.
    assert combine_readings(ALL3, _r("CALL", "CALL", "PUT"), rule="majority").direction == "CALL"


def test_live_strip_uses_the_same_function_as_the_engine():
    """The duplicate rule that used to live in live_stream is gone; if it ever
    comes back this import check is the tripwire."""
    import inspect

    from app.algo import live_stream, orchestrator

    assert "combine_readings" in inspect.getsource(live_stream._combined)
    assert "combine_readings" in inspect.getsource(orchestrator.ZoneOrchestrator._combine)
