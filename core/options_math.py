"""
Options math — pure stdlib (math only), deterministic, unit-tested.

Black-Scholes pricing + greeks, implied volatility by bisection, IV rank /
percentile, annualized realized volatility, and an earnings implied-move helper.
No numpy/pandas dependency so it runs anywhere, including a phone.
"""

from __future__ import annotations

import math
from typing import Sequence

SQRT_2PI = math.sqrt(2.0 * math.pi)


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


def norm_cdf(x: float) -> float:
    """Standard normal CDF via erf — accurate and stdlib-only."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _d1_d2(S: float, K: float, t: float, r: float, sigma: float, q: float = 0.0):
    if S <= 0 or K <= 0 or t <= 0 or sigma <= 0:
        return None, None
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    return d1, d2


def bs_price(
    S: float, K: float, t: float, r: float, sigma: float,
    option_type: str = "call", q: float = 0.0,
) -> float:
    """Black-Scholes(-Merton) price. t in years, r/q annualized, sigma annualized."""
    if t <= 0 or sigma <= 0:
        # Intrinsic value at/after expiry.
        intrinsic = (S - K) if option_type == "call" else (K - S)
        return max(0.0, intrinsic)
    d1, d2 = _d1_d2(S, K, t, r, sigma, q)
    if option_type == "call":
        return S * math.exp(-q * t) * norm_cdf(d1) - K * math.exp(-r * t) * norm_cdf(d2)
    return K * math.exp(-r * t) * norm_cdf(-d2) - S * math.exp(-q * t) * norm_cdf(-d1)


def bs_greeks(
    S: float, K: float, t: float, r: float, sigma: float,
    option_type: str = "call", q: float = 0.0,
) -> dict[str, float]:
    """Delta, gamma, theta (per day), vega (per 1 vol point)."""
    d1, d2 = _d1_d2(S, K, t, r, sigma, q)
    if d1 is None:
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    pdf = norm_pdf(d1)
    disc_q = math.exp(-q * t)
    if option_type == "call":
        delta = disc_q * norm_cdf(d1)
    else:
        delta = disc_q * (norm_cdf(d1) - 1.0)
    gamma = disc_q * pdf / (S * sigma * math.sqrt(t))
    vega = S * disc_q * pdf * math.sqrt(t) / 100.0  # per 1 percentage-point of vol
    term1 = -(S * disc_q * pdf * sigma) / (2.0 * math.sqrt(t))
    if option_type == "call":
        theta = (term1
                 - r * K * math.exp(-r * t) * norm_cdf(d2)
                 + q * S * disc_q * norm_cdf(d1)) / 365.0
    else:
        theta = (term1
                 + r * K * math.exp(-r * t) * norm_cdf(-d2)
                 - q * S * disc_q * norm_cdf(-d1)) / 365.0
    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega}


def implied_vol(
    price: float, S: float, K: float, t: float, r: float,
    option_type: str = "call", q: float = 0.0,
    lo: float = 1e-4, hi: float = 5.0, tol: float = 1e-5, max_iter: int = 100,
) -> float | None:
    """Implied volatility by bisection. Returns None if price is outside the
    no-arbitrage band (can't be inverted)."""
    if price <= 0 or t <= 0 or S <= 0 or K <= 0:
        return None
    # Reject only genuinely sub-arbitrage prices. For European options the lower
    # bound is the DISCOUNTED intrinsic, not (S-K)/(K-S): an ITM put can trade
    # below K-S when r>0, and an ITM call below S-K when q>0 — those are valid.
    if option_type == "call":
        lower = max(0.0, S * math.exp(-q * t) - K * math.exp(-r * t))
    else:
        lower = max(0.0, K * math.exp(-r * t) - S * math.exp(-q * t))
    if price < lower - 1e-6:
        return None
    f_lo = bs_price(S, K, t, r, lo, option_type, q) - price
    f_hi = bs_price(S, K, t, r, hi, option_type, q) - price
    if f_lo * f_hi > 0:
        return None
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        f_mid = bs_price(S, K, t, r, mid, option_type, q) - price
        if abs(f_mid) < tol:
            return mid
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return 0.5 * (lo + hi)


MIN_IV_OBSERVATIONS = 60


def iv_rank(current_iv: float, history: Sequence[float],
            min_observations: int = MIN_IV_OBSERVATIONS) -> float | None:
    """IV Rank = where current IV sits between the min and max of `history`, in
    [0, 1]. Returns None — meaning "stand aside" — unless the history is both
    non-degenerate AND long enough to mean anything.

    The sample floor is the whole point. IV rank is conventionally a 1-YEAR
    statistic, but this formula happily returns a number from 2 observations, and
    a short window makes it actively misleading rather than merely noisy: over a
    few weeks of gently rising vol, today's reading IS the max, so the rank prints
    ~100% ("premium is extremely rich!") at an implied vol that is below average
    by any historical standard. That is a manufactured sell signal. Requiring a
    real sample makes the system stand aside until it can actually judge — the
    fail-closed direction.

    Note the returned number is a rank over WHATEVER window `history` covers, not
    necessarily a year; callers should surface the sample size alongside it so a
    60-day rank is never mistaken for a 252-day one."""
    vals = [v for v in history if v is not None and v > 0]
    if len(vals) < max(2, int(min_observations)):
        return None
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return None
    return max(0.0, min(1.0, (current_iv - lo) / (hi - lo)))


def iv_percentile(current_iv: float, history: Sequence[float]) -> float | None:
    """IV Percentile = fraction of historical readings below current IV, in [0, 1]."""
    vals = [v for v in history if v is not None and v > 0]
    if not vals:
        return None
    below = sum(1 for v in vals if v < current_iv)
    return below / len(vals)


def realized_vol(closes: Sequence[float], periods_per_year: int = 252) -> float | None:
    """Annualized realized volatility from a close series (log returns)."""
    cs = [c for c in closes if c and c > 0]
    if len(cs) < 3:
        return None
    rets = [math.log(cs[i] / cs[i - 1]) for i in range(1, len(cs))]
    n = len(rets)
    mean = sum(rets) / n
    var = sum((x - mean) ** 2 for x in rets) / (n - 1)
    return math.sqrt(var) * math.sqrt(periods_per_year)


def implied_move(straddle_price: float, spot: float) -> float | None:
    """Approx expected move (as a fraction of spot) priced into an ATM straddle."""
    if spot <= 0 or straddle_price <= 0:
        return None
    return (straddle_price * 0.85) / spot  # ~0.85 factor: standard ATM-straddle approximation
