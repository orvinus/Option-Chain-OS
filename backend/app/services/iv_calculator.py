"""Black–Scholes implied volatility (pure Python, no scipy).

Bracketed bisection on sigma for robustness across strikes.
"""
from __future__ import annotations

import math
from typing import Literal

OptionSide = Literal["CE", "PE"]


def _norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def bs_price(side: OptionSide, S: float, K: float, T: float, r: float, sigma: float) -> float:
    """European option price (continuous compounding, q=0)."""
    if sigma <= 0:
        sigma = 1e-10
    if T <= 0:
        intrinsic = max((S - K) if side == "CE" else (K - S), 0.0)
        return intrinsic
    sqrt_t = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    disc = math.exp(-r * T)
    if side == "CE":
        return S * _norm_cdf(d1) - K * disc * _norm_cdf(d2)
    return K * disc * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def calc_iv(
    side: OptionSide,
    market_price: float | None,
    S: float | None,
    K: float,
    T: float,
    r: float = 0.065,
) -> float | None:
    """Return annualized IV as decimal (e.g. 0.18), or None if not solvable."""
    if market_price is None or S is None or S <= 0 or K <= 0:
        return None
    if market_price <= 0:
        return None

    # Minimum time as fraction of year (~1 hour floor)
    t_eff = max(float(T), 1.0 / (365.25 * 24))

    intrinsic = max((S - K) if side == "CE" else (K - S), 0.0)
    lower_bound = intrinsic * math.exp(-r * t_eff)
    if market_price + 1e-9 < lower_bound:
        return None

    price_model = lambda sig: bs_price(side, S, K, t_eff, r, sig)

    lo, hi = 1e-6, 5.0
    f_lo = price_model(lo) - market_price
    f_hi = price_model(hi) - market_price
    tries = 0
    while f_hi < 0 and tries < 30:
        hi *= 1.4
        f_hi = price_model(hi) - market_price
        tries += 1
    if f_lo > 0 or f_hi < 0:
        return None

    sigma = lo
    for _ in range(100):
        sigma = (lo + hi) / 2.0
        mid = price_model(sigma) - market_price
        if abs(mid) < 1e-7 * max(market_price, 1.0):
            return float(max(sigma, 1e-8))
        fl = price_model(lo) - market_price
        if fl * mid <= 0:
            hi = sigma
        else:
            lo = sigma

    return float(max(sigma, 1e-8)) if math.isfinite(sigma) else None
