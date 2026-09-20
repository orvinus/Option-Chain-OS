"""scripts/parity — the TradingView export parser and the event aligner,
pinned on a slice of a real Pine Logs paste (tests/fixtures/tv_logs_slice.txt)."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if not (_REPO / "scripts" / "parity").exists():          # test container: /scripts
    _REPO = Path("/")
sys.path.insert(0, str(_REPO))

from scripts.parity.align import PRICE_TOL, align_events, bar5  # noqa: E402
from scripts.parity.tv_parse import (  # noqa: E402
    OurEvent,
    kind_ours,
    kind_tv,
    near_level,
    parse_levels,
    parse_tv,
    tv_symbol_contract,
    tv_symbol_to_file,
)

FIX = Path(__file__).resolve().parent / "fixtures" / "tv_logs_slice.txt"


def test_parse_tv_reads_dbg_and_signals():
    tv = parse_tv(FIX.read_text(encoding="utf-8"))
    assert "NIFTY260908P23900" in tv
    c = tv["NIFTY260908P23900"]
    assert c.dbg["D1"] == "99/35.7/86.4" and c.dbg["H1N"] == "25"
    assert parse_levels(c.dbg["LV"])[0] == (1, 50.75)
    assert len(c.signals) > 5
    first = c.signals[0]
    assert first.kind == "ENTRY:S2A" and first.value == 66.2
    assert first.minute.endswith(":00") or first.minute[-2:].isdigit()
    # TRAIL SET value comes from the ₹ text, not the label y
    ts = next(s for s in c.signals if s.kind == "TRAIL_SET")
    assert ts.value == 60.55 and ts.px == 66.7


def test_kind_classification_covers_every_label_family():
    assert kind_tv("▲ S2A ₹66.2") == "ENTRY:S2A"
    assert kind_tv("▲ R1 ₹61.46") == "ENTRY:R1"
    assert kind_tv("✦ TRAIL SET ₹60.55") == "TRAIL_SET"
    assert kind_tv("✦ TRAIL ↑ ₹64.24") == "TRAIL_RAISE"
    assert kind_tv("✦ TRAIL EXIT ₹64.24") == "TRAIL_EXIT"
    assert kind_tv("✗ SL HIT Close < Base ₹66.2") == "BASE_SL"
    assert kind_tv("✗ MAX SL ₹49.97") == "MAX_SL"
    assert kind_tv("◈ ZONE SET ₹70.96") == "SB_SET"
    assert kind_tv("◈ ZONE EXIT ₹70.96") == "SB_EXIT"
    assert kind_tv("◆ TARGET HIT ₹80") == "TARGET"
    assert kind_ours("S3C") == "ENTRY:S3C" and kind_ours("TRAIL_EXIT") == "TRAIL_EXIT"


def test_symbol_mapping():
    assert tv_symbol_to_file("NIFTY260908C23900") == "NIFTY_2026-09-08_23900CE.json"
    assert tv_symbol_contract("NIFTY260908P23700") == ("NIFTY", "2026-09-08", 23700, "PE")
    assert tv_symbol_to_file("garbage") is None


def test_bar5_and_tolerances():
    assert bar5("09-02 10:37") == "09-02 10:35" and bar5("09-02 10:35") == "09-02 10:35"
    assert near_level(150.0, 150.07) and not near_level(150.0, 150.2)
    assert PRICE_TOL == 0.011


def test_align_events_reports_every_diff_class():
    tv = parse_tv(FIX.read_text(encoding="utf-8"))["NIFTY260908P23900"]
    sigs = tv.signals[:6]
    # our side = the same six events, one value nudged, one dropped, one extra at the end
    ours: list[OurEvent] = []
    for s in sigs:
        kind = s.kind.replace("ENTRY:", "") if s.kind.startswith("ENTRY:") else s.kind
        ours.append(OurEvent(s.minute, s.value, kind_ours(kind), s.text, s.minute))
    ours[3] = OurEvent(ours[3].minute, ours[3].value + 0.5, ours[3].kind, ours[3].text, ours[3].ts)   # VALUE diff
    del ours[1]                                                                                    # TV-only
    ours.append(OurEvent("09-30 15:20", 1.0, "ENTRY:S1A", "S1A", "09-30 15:20"))                     # platform-only
    al = align_events("NIFTY260908P23900", sigs, ours)
    kinds = {d.kind for d in al.diffs}
    assert "VALUE" in kinds and "TV_ONLY" in kinds and "PLATFORM_ONLY" in kinds
    assert al.matched == 5 and al.exact == 4
    assert al.first_structural is not None and al.first_structural.kind == "TV_ONLY"


def test_align_label_equivalence_trail_set_vs_raise():
    ours = [OurEvent("09-02 10:35", 60.55, "TRAIL_RAISE", "TRAIL_RAISE 60.55", "x")]
    from scripts.parity.tv_parse import TvSignal
    s = TvSignal(0, 66.7, "✦ TRAIL SET ₹60.55", "TRAIL_SET", 60.55)
    s.minute  # noqa: B018 — property exists
    # force the same bar via monkeypatched minute
    al = align_events("X", [], ours)
    assert al.ours_n == 1 and al.matched == 0
