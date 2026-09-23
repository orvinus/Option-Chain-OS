"""Algo Config document tests — Appendix-A seed fidelity, §15 validation,
§14 runtime safety gate, audit diffing.

Run:  cd backend && PYTHONPATH=. python tests/test_algo_config_models.py

Pure model tests — no database, no network.
"""
from __future__ import annotations

from app.algo.audit import config_diff, flatten_config
from app.algo.config_models import (
    AlgoConfig,
    default_config,
    validate_document,
    zone_completeness,
)
from app.algo.config_store import _scope_of


def _fresh() -> AlgoConfig:
    return default_config()


# ──────────────────────────────────────────────────────────────────────────
# Appendix-A seed fidelity — the out-of-the-box document must match the
# reviewed dashboard mockups exactly (spec §18).
# ──────────────────────────────────────────────────────────────────────────

def test_seed_round_trips_through_json():
    cfg = _fresh()
    doc = cfg.model_dump(by_alias=True)
    again = AlgoConfig.model_validate(doc)
    assert again.model_dump(by_alias=True) == doc, (
        "the seed document must survive dump→validate unchanged — otherwise every "
        "save would silently rewrite untouched fields and flood the audit diff"
    )


def test_seed_matches_appendix_a_zone_table():
    cfg = _fresh()
    z = cfg.zone("monday", "Z1")
    assert z is not None
    assert (z.start, z.end, z.premium_min, z.premium_max, z.max_trades) == (
        "09:20", "10:30", 75.0, 125.0, 2
    ), "Monday Z1 must be the §18.1 row verbatim"
    tue3 = cfg.zone("tuesday", "Z3")
    assert tue3 is not None and (tue3.start, tue3.end) == ("14:10", "15:30")
    assert cfg.zone("wednesday", "Z2").zone_kill is True, (
        "§18.1 marks Wednesday Z2 'Killed (manual)'"
    )
    assert cfg.days["friday"].day_kill is True, "§18.1 marks Friday 'Day Off'"
    assert cfg.days["friday"].end_exit_enabled is False, (
        "§18.1 shows Friday's last-zone End-Exit Off"
    )


def test_seed_matches_appendix_a_risk_table():
    cfg = _fresh()
    tue = cfg.days["tuesday"]
    assert tue.all_in is True and tue.allocation_pct == 100.0
    wed = cfg.days["wednesday"]
    assert (wed.allocation_pct, wed.max_loss_pct, wed.max_profit_lock_pct,
            wed.max_consec_losses, wed.max_consec_loss_pct) == (40.0, 15.0, 25.0, 2, 20.0)


def test_seed_ump_zone_tiers_escalate():
    cfg = _fresh()
    for day in ("monday", "thursday"):
        z1, z3 = cfg.zone(day, "Z1").ump, cfg.zone(day, "Z3").ump
        assert z1.institutional.expansion_law_pct == 18.0
        assert z3.institutional.expansion_law_pct == 24.0
        assert z1.entry.max_sl_pct == 8.0 and z3.entry.max_sl_pct == 12.0
        assert z1.entry.trigger_timeout_bars == 4 and z3.entry.trigger_timeout_bars == 8
        assert z1.visual.zone_width_pct == 2.0 and z3.visual.zone_width_pct == 4.0


def test_seed_retest_alternation_pattern():
    cfg = _fresh()
    got = {
        day: [cfg.zone(day, z).ump.entry.enable_retest for z in ("Z1", "Z2", "Z3")]
        for day in ("monday", "tuesday", "wednesday")
    }
    assert got["monday"] == [True, False, True]
    assert got["tuesday"] == [False, True, False]
    assert got["wednesday"] == [True, False, True]


def test_last_zone_tracks_end_time_not_label():
    cfg = _fresh()
    assert cfg.last_zone_id("monday") == "Z3"
    # §2.3: 'the rule always tracks whichever zone is last, not a fixed label'.
    cfg.days["monday"].zones["Z2"].end = "15:40"
    assert cfg.last_zone_id("monday") == "Z2"


# ──────────────────────────────────────────────────────────────────────────
# §15 pre-save validation
# ──────────────────────────────────────────────────────────────────────────

# Overnight carry (default ON, locked user decision 2026-08-19) deliberately
# keeps two LOUD §15 warnings on every save — the seed is otherwise clean.
_CARRY_WARNING_PREFIXES = (
    "global: OVERNIGHT CARRY is ON",
    "global: End-Exit is configured on",
)


def _non_carry(warnings: list[str]) -> list[str]:
    return [w for w in warnings if not w.startswith(_CARRY_WARNING_PREFIXES)]


def test_seed_document_validates_clean():
    errors, warnings = validate_document(_fresh())
    assert errors == [], f"the reviewed reference config must save cleanly: {errors}"
    assert _non_carry(warnings) == [], f"…and without unexpected warnings: {warnings}"
    assert len(warnings) == 2, f"exactly the two standing carry warnings: {warnings}"


def test_overlapping_zones_block_save():
    cfg = _fresh()
    cfg.days["monday"].zones["Z2"].start = "10:00"   # inside Z1's 09:20–10:30
    errors, _ = validate_document(cfg)
    assert any("overlap" in e for e in errors), errors


def test_start_after_end_blocks_save():
    cfg = _fresh()
    cfg.days["monday"].zones["Z1"].end = "09:00"
    errors, _ = validate_document(cfg)
    assert any("before end" in e for e in errors), errors


def test_zone_outside_exchange_window_blocks_save():
    cfg = _fresh()
    cfg.days["monday"].zones["Z1"].start = "09:00"   # before 09:15
    errors, _ = validate_document(cfg)
    assert any("outside 09:15" in e for e in errors), errors


def test_inverted_premium_band_blocks_save():
    cfg = _fresh()
    cfg.days["monday"].zones["Z1"].premium_min = 200.0   # > max 125
    errors, _ = validate_document(cfg)
    assert any("premium" in e for e in errors), errors


def test_zero_indicators_warns_but_does_not_block():
    cfg = _fresh()
    cfg.days["monday"].zones["Z1"].enabled_indicators = []
    errors, warnings = validate_document(cfg)
    assert errors == [], "a zero-indicator zone is legal to SAVE (§4.1) …"
    assert any("never produce a direction" in w for w in warnings), (
        "… but must be flagged, not silently left to always No-Trade"
    )


def test_extreme_ump_values_are_not_errors():
    # §5.2: no field in the UMP section has a minimum or maximum — an extreme
    # value is a trading decision, not a validation failure.
    cfg = _fresh()
    z = cfg.days["monday"].zones["Z1"]
    z.ump.entry.max_sl_pct = 500.0
    z.ump.institutional.expansion_law_pct = 1000.0
    errors, _ = validate_document(cfg)
    assert errors == [], errors
    assert zone_completeness(cfg, "monday", "Z1") == []


def test_config_lock_defaults_and_all_timeframes():
    cfg = _fresh()
    # Config Lock ships OFF (lock) / ON (confirm) and validates cleanly.
    assert cfg.global_.config_lock.lock_market_hours is False
    assert cfg.global_.config_lock.require_save_confirm is True
    # MTF default now computes every timeframe the parser accepts.
    z = cfg.days["monday"].zones["Z1"]
    assert z.mtf_ratio.timeframes == [
        "1m", "3m", "5m", "10m", "15m", "30m", "1h", "2h", "3h", "full_day",
    ]
    errors, warnings = validate_document(cfg)
    assert errors == [] and _non_carry(warnings) == []


def test_config_lock_exemption_predicate():
    """The market-hours lock allows a save ONLY when every changed path is
    inside global.config_lock.* — the exact predicate config_store enforces."""
    from app.algo.audit import config_diff

    base = _fresh().model_dump(by_alias=True)

    unlock = _fresh()
    unlock.global_.config_lock.lock_market_hours = True
    changes = config_diff(base, unlock.model_dump(by_alias=True))
    assert changes and all(p.startswith("global.config_lock.") for p, _, _ in changes)

    mixed = _fresh()
    mixed.global_.config_lock.lock_market_hours = True
    mixed.global_.fees.brokerage_per_order = 20.0
    changes2 = config_diff(base, mixed.model_dump(by_alias=True))
    assert not all(p.startswith("global.config_lock.") for p, _, _ in changes2)


def test_live_mode_requires_broker_connected():
    cfg = _fresh()
    cfg.global_.paper.paper_mode = False
    cfg.global_.demat_balance = 50000.0
    errors, _ = validate_document(cfg, broker_configured=False)
    assert any("no broker is connected" in e for e in errors), errors
    errors2, _ = validate_document(cfg, broker_configured=True)
    assert not any("broker" in e for e in errors2), errors2
    # Pure-config callers (no runtime fact) skip the broker check entirely.
    errors3, _ = validate_document(cfg)
    assert not any("broker" in e for e in errors3), errors3


def test_live_mode_requires_demat_balance():
    cfg = _fresh()
    cfg.global_.paper.paper_mode = False
    cfg.global_.demat_balance = 0.0
    errors, _ = validate_document(cfg, broker_configured=True)
    assert any("demat balance" in e for e in errors), errors
    # Paper mode: same missing balance is only a warning (unchanged M0 rule).
    cfg.global_.paper.paper_mode = True
    errors2, warnings2 = validate_document(cfg)
    assert not any("demat" in e for e in errors2)
    assert any("demat balance" in w for w in warnings2)


# ──────────────────────────────────────────────────────────────────────────
# §14 runtime safety gate
# ──────────────────────────────────────────────────────────────────────────

def test_gate_passes_for_seed_zones():
    cfg = _fresh()
    assert zone_completeness(cfg, "monday", "Z1") == []
    assert zone_completeness(cfg, "thursday", "Z3") == []


def test_gate_blocks_broken_premium_band():
    cfg = _fresh()
    cfg.days["monday"].zones["Z1"].premium_max = 10.0
    problems = zone_completeness(cfg, "monday", "Z1")
    assert any("premium band" in p for p in problems), problems


def test_gate_blocks_zero_indicators():
    cfg = _fresh()
    cfg.days["monday"].zones["Z1"].enabled_indicators = []
    problems = zone_completeness(cfg, "monday", "Z1")
    assert any("no entry-filter indicator" in p for p in problems), problems


def test_gate_blocks_invalid_day_risk():
    cfg = _fresh()
    cfg.days["monday"].allocation_pct = 0.0   # all_in is False on Monday
    problems = zone_completeness(cfg, "monday", "Z1")
    assert any("allocation" in p for p in problems), problems


def test_gate_blocks_negative_ump_value():
    cfg = _fresh()
    cfg.days["monday"].zones["Z1"].ump.entry.max_sl_pct = -1.0
    problems = zone_completeness(cfg, "monday", "Z1")
    assert any("max SL" in p for p in problems), problems


def test_gate_reports_missing_day_and_zone():
    cfg = _fresh()
    del cfg.days["monday"].zones["Z2"]
    assert zone_completeness(cfg, "monday", "Z2") == [
        "monday Z2: zone configuration missing entirely"
    ]
    del cfg.days["tuesday"]
    assert zone_completeness(cfg, "tuesday", "Z1") == [
        "tuesday: day configuration missing entirely"
    ]


# ──────────────────────────────────────────────────────────────────────────
# Audit diffing (§11.3 — exact old→new per changed field)
# ──────────────────────────────────────────────────────────────────────────

def test_config_diff_reports_exact_leaf_change():
    old = _fresh().model_dump(by_alias=True)
    new_cfg = _fresh()
    new_cfg.days["monday"].zones["Z1"].premium_max = 150.0
    new = new_cfg.model_dump(by_alias=True)
    changes = config_diff(old, new)
    assert changes == [("days.monday.zones.Z1.premium_max", 125.0, 150.0)], changes


def test_config_diff_treats_lists_as_one_unit():
    old = _fresh().model_dump(by_alias=True)
    new_cfg = _fresh()
    new_cfg.days["monday"].zones["Z1"].enabled_indicators = ["oi_change"]
    changes = config_diff(old, new_cfg.model_dump(by_alias=True))
    paths = [c[0] for c in changes]
    assert paths == ["days.monday.zones.Z1.enabled_indicators"], (
        "reordering/shrinking a list must diff as ONE row, not index-keyed noise"
    )


def test_flatten_produces_dotted_paths():
    flat = flatten_config(_fresh().model_dump(by_alias=True))
    assert flat["days.monday.zones.Z1.premium_min"] == 75.0
    assert flat["global.fees.brokerage_per_order"] == 17.0


def test_scope_of_maps_paths_to_day_zone_global():
    assert _scope_of("days.monday.zones.Z1.premium_max") == "monday/Z1"
    assert _scope_of("days.monday.max_loss_pct") == "monday"
    assert _scope_of("global.fees.gst_pct") == "global"


def test_strike_scan_count_defaults_are_governed():
    """Model default 1 = old stored documents (incl. frozen backtest configs)
    keep their original single-strike behaviour on re-parse; the Appendix-A
    seed ships the intended multi-strike 3; the live config upgrades via an
    explicit AUDITED save, never via a silent code default (2026-08-18)."""
    from app.algo.config_models import ZoneConfig

    assert ZoneConfig().strike_scan_count == 1
    cfg = default_config()
    assert all(
        z.strike_scan_count == 3
        for d in cfg.days.values()
        for z in d.zones.values()
    )


# ── §8 paper completeness: fill source + latency ──────────────────────────

def test_paper_fill_source_bid_ask_mid_is_normalised_to_ltp():
    """The feed stores LTP only. An old document that saved 'bid_ask_mid'
    must still LOAD (Literal kept) but comes out as 'ltp', flagged so the
    pre-save validator can say so; the flag itself never serialises."""
    from app.algo.config_models import PaperConfig

    p = PaperConfig(fill_source="bid_ask_mid")
    assert p.fill_source == "ltp"
    assert p.fill_source_normalised is True
    assert "fill_source_normalised" not in p.model_dump()
    assert PaperConfig().fill_source == "ltp"
    assert PaperConfig().fill_source_normalised is False

    doc = _fresh().model_dump(mode="json", by_alias=True)
    doc["global"]["paper"]["fill_source"] = "bid_ask_mid"
    cfg = AlgoConfig.model_validate(doc)
    assert cfg.global_.paper.fill_source == "ltp"
    errors, warnings = validate_document(cfg)
    assert not errors
    assert any("fill source normalised to LTP (the feed stores LTP only)" in w for w in warnings), warnings

    # A clean document raises no fill-source warning at all.
    _, clean = validate_document(_fresh())
    assert not any("fill source" in w for w in clean)


def test_paper_latency_above_runtime_cap_warns_but_saves():
    from app.algo.config_models import PAPER_LATENCY_CAP_MS

    assert PAPER_LATENCY_CAP_MS == 2000
    cfg = _fresh()
    cfg.global_.paper.latency_ms = 2000
    errors, warnings = validate_document(cfg)
    assert not errors and not any("latency" in w for w in warnings)

    cfg.global_.paper.latency_ms = 2001
    errors, warnings = validate_document(cfg)
    assert not errors, "an over-cap latency is a warning, never a blocked save"
    assert any("runtime clamps to 2000 ms" in w for w in warnings), warnings


def _run_all() -> None:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL  {name}: {e}")
    if failures:
        raise SystemExit(f"{failures} test(s) failed")
    print("all algo config model tests passed")


if __name__ == "__main__":
    _run_all()


def test_strike_scan_count_is_no_longer_capped_at_10():
    """2026-09-23: the zone "Strikes" box stopped at 10. The ceiling is now
    one chain side at the widest data window (±60 → 121)."""
    import pytest
    from pydantic import ValidationError

    from app.algo.config_models import ZoneConfig

    base = _fresh().days["wednesday"].zones["Z1"].model_dump()
    assert ZoneConfig.model_validate({**base, "strike_scan_count": 23}).strike_scan_count == 23
    assert ZoneConfig.model_validate({**base, "strike_scan_count": 121}).strike_scan_count == 121
    with pytest.raises(ValidationError):
        ZoneConfig.model_validate({**base, "strike_scan_count": 122})


def test_strike_scan_count_above_the_collected_strikes_warns():
    cfg = _fresh()
    day = cfg.days["wednesday"]
    day.data_strike_window = 11                     # 23 strikes collected per side
    day.zones["Z1"].strike_scan_count = 23
    _, warnings = validate_document(cfg)
    assert not any("exceeds the" in w for w in warnings), "23 of 23 is fine"
    day.zones["Z1"].strike_scan_count = 30
    _, warnings = validate_document(cfg)
    assert any("wednesday Z1: strike scan count 30 exceeds the 23 strikes collected" in w
               for w in warnings), warnings
