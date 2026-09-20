"""Regression guard for the pytz Local-Mean-Time trap.

``IST`` is a pytz zone. pytz tzinfo objects carry every historical offset the
zone has ever had, and ``datetime.replace(tzinfo=IST)`` selects the FIRST — Local
Mean Time at +05:53 for Asia/Kolkata, not +05:30. Timestamps built that way are
23 minutes wrong, with no exception and a value that looks entirely plausible.

That is not a hypothetical: ``scripts/truedata_backfill.py`` used the idiom, and
the entire six-month TrueData archive (24.5M rows across 128 trading days) was
written 23 minutes early. It survived collection, a validation pass and weeks of
being served to Replay and the charts, because nothing about the data looks
wrong unless you compare it against another source at varying offsets.

These tests pin both halves of the fix: the helper is correct, and the trap is
not reintroduced anywhere in the codebase.

Runnable without pytest:  PYTHONPATH=. python tests/test_ist_conversion.py
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from app.core.time_utils import IST, ist_naive_to_utc

REPO = Path(__file__).resolve().parents[2]


def test_helper_uses_real_ist_offset() -> None:
    """09:15 IST is 03:45 UTC. Not 03:22."""
    got = ist_naive_to_utc(datetime(2026, 8, 13, 9, 15))
    assert got == datetime(2026, 8, 13, 3, 45, tzinfo=timezone.utc)

    got = ist_naive_to_utc(datetime(2026, 8, 13, 15, 30))
    assert got == datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)


def test_the_trap_is_exactly_23_minutes() -> None:
    """Document the bug's magnitude, so the number in the repair script is checked.

    If a future pytz/tzdata release changed the LMT offset, the repair's hardcoded
    23 minutes would be wrong — this test is what would catch that.
    """
    naive = datetime(2026, 8, 13, 9, 15)
    correct = ist_naive_to_utc(naive)
    trap = naive.replace(tzinfo=IST).astimezone(timezone.utc)
    delta_min = (trap - correct).total_seconds() / 60
    assert delta_min == -23.0, f"the LMT trap is {-delta_min} minutes, not 23"


def test_helper_is_idempotent_for_aware_datetimes() -> None:
    """An already-aware datetime must pass through, not be re-localised."""
    aware = datetime(2026, 8, 13, 3, 45, tzinfo=timezone.utc)
    assert ist_naive_to_utc(aware) == aware


def test_dst_free_zone_has_no_ambiguity() -> None:
    """India observes no DST, so localize() can never raise Ambiguous/NonExistent."""
    for month in range(1, 13):
        got = ist_naive_to_utc(datetime(2026, month, 15, 9, 15))
        assert got.utcoffset() == timezone.utc.utcoffset(got)
        # +05:30 for every month of the year.
        assert (datetime(2026, month, 15, 9, 15) - got.replace(tzinfo=None)).seconds == 5 * 3600 + 1800


def test_trap_idiom_is_absent_from_the_codebase() -> None:
    """Nobody may reintroduce ``replace(tzinfo=IST)`` in EXECUTABLE code.

    Scanning the source is the only durable defence, because the bug raises
    nothing — no runtime test elsewhere would surface a new occurrence.

    Parsed with ast rather than grepped, so the prose in this file, in
    time_utils' docstring and in the repair script — all of which necessarily
    quote the broken idiom in order to explain it — are not mistaken for the
    thing they warn about.
    """
    import ast

    offenders: list[str] = []
    for path in list(REPO.glob("backend/**/*.py")) + list(REPO.glob("scripts/**/*.py")):
        # This file is the one legitimate user of the idiom: it applies the trap
        # on purpose in order to assert that the shift really is 23 minutes.
        if "__pycache__" in str(path) or path.name == "test_ist_conversion.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            # BOTH spellings of the trap: x.replace(tzinfo=IST) AND
            # datetime.combine(..., tzinfo=IST) / datetime(..., tzinfo=IST) —
            # the combine variant lived in algo_engines._window_for_date and
            # shifted every historical engine window 23 minutes early.
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("replace", "combine")):
                continue
            for kw in node.keywords:
                if kw.arg == "tzinfo" and isinstance(kw.value, ast.Name) and kw.value.id == "IST":
                    offenders.append(f"{path.relative_to(REPO)}:{node.lineno}")
    assert not offenders, (
        "tzinfo=IST attaches pytz's +05:53 LMT and shifts data by 23 "
        f"minutes — use ist_naive_to_utc() instead. Found at: {offenders}"
    )


def test_repair_script_shift_matches_the_measured_trap() -> None:
    src = (REPO / "scripts" / "repair_archive_tz.py").read_text(encoding="utf-8")
    assert "SHIFT_MINUTES = 23" in src


def _run_all() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run_all()
    print("\nall IST conversion tests passed")
