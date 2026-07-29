"""Black–Scholes analytic Greeks (pure Python, no scipy).

Uses the same BS assumptions as ``iv_calculator``: continuous compounding, q=0
(or r=0 for commodity options-on-futures / Black-76 approximation).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

OptionSide = Literal["CE", "PE"]


def _norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


@dataclass(frozen=True)
class Greeks:
    delta: float
    gamma: float
    theta: float  # per calendar day
    vega: float   # per 1% IV move


def calc_greeks(
    side: OptionSide,
    S: float | None,
    K: float,
    T: float,
    r: float,
    sigma: float | None,
) -> Greeks | None:
    """Return analytic BS Greeks, or None if inputs are invalid.

    Theta is calendar-day (divide annual theta by 365.25).
    Vega is for a 1 percentage-point move in IV (÷ 100).
    """
    if S is None or S <= 0 or K <= 0 or sigma is None or sigma <= 0:
        return None
    t_eff = max(float(T), 1.0 / (365.25 * 24))
    sqrt_t = math.sqrt(t_eff)
    if sqrt_t <= 0:
        return None

    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * t_eff) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    pdf_d1 = _norm_pdf(d1)
    disc = math.exp(-r * t_eff)

    gamma = pdf_d1 / (S * sigma * sqrt_t)
    vega = S * pdf_d1 * sqrt_t / 100.0  # per 1% IV

    if side == "CE":
        delta = _norm_cdf(d1)
        theta_annual = (
            -(S * pdf_d1 * sigma) / (2.0 * sqrt_t)
            - r * K * disc * _norm_cdf(d2)
        )
    else:
        delta = _norm_cdf(d1) - 1.0
        theta_annual = (
            -(S * pdf_d1 * sigma) / (2.0 * sqrt_t)
            + r * K * disc * _norm_cdf(-d2)
        )

    return Greeks(
        delta=float(delta),
        gamma=float(gamma),
        theta=float(theta_annual / 365.25),
        vega=float(vega),
    )


def synthetic_future(
    spot: float | None,
    atm_call_ltp: float | None,
    atm_put_ltp: float | None,
    atm_strike: float | None,
) -> float | None:
    """Put-call parity synthetic future: K + C - P (when both sides available)."""
    if atm_call_ltp is not None and atm_put_ltp is not None and atm_strike is not None:
        return float(atm_strike) + float(atm_call_ltp) - float(atm_put_ltp)
    return float(spot) if spot is not None else None
