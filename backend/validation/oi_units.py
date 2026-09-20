"""The OI-unit tripwire, vendor-neutral.

Open interest can be reported in CONTRACTS (lots) or in UNITS (contracts x lot
size). Getting it wrong rescales every OI number, every PCR and every OI-delta in
the product by 20-75x — and it does so silently, because the numbers stay
internally consistent. It is the single highest-severity data risk in a vendor
migration, and the cheapest to detect: the ratio between two sources of the same
contract's OI is either ~1 (same units) or ~lot size (different units).

Extracted from layer_a_broker so the same check can be armed three ways during
the TrueData migration:

    XTS  vs NSE        — the existing arming; proves the current pipeline
    TD   vs NSE        — proves TrueData INDEPENDENTLY, not merely equal to XTS
    TD   vs XTS        — the shadow-run parity check

The band is deliberately tight (0.98-1.02). A genuine unit mismatch is off by a
whole lot size, so anything between "slightly off" and "20x off" is not a units
bug — it is a different bug, and widening the band to accommodate it would
disarm the tripwire.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

# Ratio must sit within +/-2% of 1.0. Nothing legitimate lands between this band
# and a lot-size multiple.
BAND_LO = 0.98
BAND_HI = 1.02

# Below this OI, rounding and stale quotes dominate the ratio. Measured in the
# reference source's own units.
MIN_LIQUID_OI = 50


@dataclass(slots=True)
class UnitVerdict:
    label: str
    verdict: str          # PASS | FAIL | SKIP
    median_ratio: float | None
    samples: int
    note: str
    # Best-guess lot size if the ratio looks like a units mismatch, so the
    # operator sees "this is a lots-vs-units bug, scale by 65" rather than just
    # "ratio 65.0".
    implied_scale: int | None = None


def check_oi_units(
    pairs: list[tuple[float, float]],
    label: str,
    lot: int | None = None,
) -> UnitVerdict:
    """``pairs`` = [(ours, reference), ...] for the same contracts at the same instant.

    Filters to liquid contracts, takes the MEDIAN ratio (robust to a handful of
    stale strikes in a way the mean is not), and reports.
    """
    ratios = [
        ours / ref
        for ours, ref in pairs
        if ref and ref >= MIN_LIQUID_OI and ours
    ]
    if not ratios:
        return UnitVerdict(
            label=label,
            verdict="SKIP",
            median_ratio=None,
            samples=0,
            note=f"no contracts with reference OI >= {MIN_LIQUID_OI}",
        )

    med = statistics.median(ratios)
    ok = BAND_LO <= med <= BAND_HI
    implied: int | None = None
    if not ok:
        # If the ratio is near a whole multiple (or its reciprocal), name it —
        # that is the actionable form of the finding.
        candidate = med if med > 1 else (1 / med if med else 0)
        if candidate >= 2 and abs(candidate - round(candidate)) < 0.05:
            implied = int(round(candidate))

    note = f"median ratio over {len(ratios)} liquid contracts"
    if implied:
        direction = "ours is in UNITS, reference in LOTS" if med > 1 else \
                    "ours is in LOTS, reference in UNITS"
        note += f" — looks like a {implied}x units mismatch ({direction})"
        if lot and implied == lot:
            note += f"; {implied} == this symbol's lot size"

    return UnitVerdict(
        label=label,
        verdict="PASS" if ok else "FAIL",
        median_ratio=round(med, 4),
        samples=len(ratios),
        note=note,
        implied_scale=implied,
    )
