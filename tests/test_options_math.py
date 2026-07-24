"""Tests for the options math — pricing, greeks, IV inversion, IV rank."""

import math

from core import options_math as om


def test_put_call_parity():
    S, K, t, r, sigma = 100.0, 100.0, 0.5, 0.04, 0.25
    call = om.bs_price(S, K, t, r, sigma, "call")
    put = om.bs_price(S, K, t, r, sigma, "put")
    # C - P = S - K e^{-rt}
    lhs = call - put
    rhs = S - K * math.exp(-r * t)
    assert abs(lhs - rhs) < 1e-6


def test_call_delta_in_bounds():
    g = om.bs_greeks(100, 100, 0.5, 0.04, 0.25, "call")
    assert 0.0 < g["delta"] < 1.0
    gp = om.bs_greeks(100, 100, 0.5, 0.04, 0.25, "put")
    assert -1.0 < gp["delta"] < 0.0


def test_implied_vol_roundtrip():
    S, K, t, r, true_sigma = 100.0, 105.0, 0.4, 0.04, 0.30
    price = om.bs_price(S, K, t, r, true_sigma, "call")
    iv = om.implied_vol(price, S, K, t, r, "call")
    assert iv is not None
    assert abs(iv - true_sigma) < 1e-3


def test_iv_rank_bounds():
    # Tests the FORMULA, so the sample floor is opted out of explicitly
    # (min_observations=2). Production callers use the real floor.
    hist = [0.10, 0.20, 0.30, 0.40, 0.50]
    assert abs(om.iv_rank(0.30, hist, 2) - 0.5) < 1e-9
    assert abs(om.iv_rank(0.50, hist, 2) - 1.0) < 1e-9
    assert abs(om.iv_rank(0.10, hist, 2) - 0.0) < 1e-9
    # current above history clamps to 1.0
    assert om.iv_rank(0.99, hist, 2) == 1.0


def test_iv_rank_needs_history():
    assert om.iv_rank(0.3, [], 2) is None
    assert om.iv_rank(0.3, [0.3], 2) is None  # degenerate


def test_realized_vol_positive():
    closes = [100, 101, 99, 102, 98, 103, 97]
    rv = om.realized_vol(closes)
    assert rv is not None and rv > 0


def test_implied_vol_rejects_sub_intrinsic():
    # Price below the discounted no-arb bound can't be inverted.
    assert om.implied_vol(0.5, 100, 90, 0.5, 0.04, "call") is None


def test_implied_vol_itm_put_with_rate():
    # Regression: an ITM put can trade below undiscounted K-S when r>0 and still
    # be a perfectly valid price. It must still invert (not return None).
    S, K, t, r, true_sigma = 50.0, 100.0, 1.0, 0.10, 0.20
    price = om.bs_price(S, K, t, r, true_sigma, "put")  # ~40.49, below K-S=50
    assert price < (K - S)  # genuinely below undiscounted intrinsic
    iv = om.implied_vol(price, S, K, t, r, "put")
    assert iv is not None and abs(iv - true_sigma) < 1e-3


def test_iv_rank_requires_a_meaningful_sample():
    """REGRESSION: iv_rank used to return a number from 2 observations, and all
    three strategies gate on it. Over a few weeks of gently rising vol today's
    reading IS the max, so the rank prints ~100% ('premium is rich!') at an
    implied vol that is historically below average — a manufactured sell signal
    on the system's very first trades. Short history must mean STAND ASIDE."""
    from core.options_math import MIN_IV_OBSERVATIONS, iv_rank

    # The real shape of our banked data at ~day 20: calm drifting up.
    short_hist = [0.129, 0.134, 0.128, 0.133, 0.138, 0.128, 0.122, 0.134,
                  0.127, 0.139, 0.154, 0.154, 0.140, 0.165, 0.162, 0.165]
    today = 0.165
    assert len(short_hist) < MIN_IV_OBSERVATIONS
    # Without a floor this returns ~1.0. With one it must refuse.
    assert iv_rank(today, short_hist) is None
    assert iv_rank(today, short_hist, min_observations=2) == 1.0   # the old, bad answer

    # A long enough history is trusted and ranks sensibly.
    long_hist = [0.12 + 0.10 * (i % 10) / 10 for i in range(MIN_IV_OBSERVATIONS)]
    r = iv_rank(0.13, long_hist)
    assert r is not None and 0.0 <= r <= 1.0

    # Degenerate (flat) history is still refused even when long enough.
    assert iv_rank(0.2, [0.2] * MIN_IV_OBSERVATIONS) is None


def test_strategies_inherit_the_iv_sample_floor():
    """The floor must actually reach the engines, not just live in the helper."""
    from core.options_math import MIN_IV_OBSERVATIONS
    from strategies.premium_harvest import PremiumHarvest
    from strategies.breakout import VolatilityBreakout
    from strategies.earnings import EarningsVol

    assert PremiumHarvest({}).min_iv_observations == MIN_IV_OBSERVATIONS
    assert VolatilityBreakout({}).min_iv_observations == MIN_IV_OBSERVATIONS
    assert EarningsVol({}).min_iv_observations == MIN_IV_OBSERVATIONS
    # ...and stays operator-configurable.
    assert PremiumHarvest({"min_iv_observations": 252}).min_iv_observations == 252


def test_fill_greeks_from_mid_recovers_a_sane_delta():
    """The delayed-data fallback: recover greeks from the mid when the broker
    sent none. Round-trips against bs_price so it must be internally consistent."""
    from core.options_math import fill_greeks_from_mid, bs_price
    S, K, dte, r, sigma = 690.0, 650.0, 36, 0.04, 0.25
    t = dte / 365.0
    mid = bs_price(S, K, t, r, sigma, "put")          # a known-good mid
    g = fill_greeks_from_mid(S, K, dte, "put", mid)
    assert g is not None
    assert abs(g["implied_vol"] - sigma) < 1e-2       # inverts back to input vol
    assert -0.5 < g["delta"] < 0.0                    # OTM put: small negative delta
    assert g["gamma"] > 0 and g["vega"] > 0

    # A 30-delta short put strike lands in the strategy's [0.12, 0.40] band.
    atm_mid = bs_price(S, S * 0.96, t, r, 0.22, "put")
    gd = fill_greeks_from_mid(S, S * 0.96, dte, "put", atm_mid)
    assert 0.12 <= abs(gd["delta"]) <= 0.45


def test_fill_greeks_from_mid_refuses_uninvertible_input():
    from core.options_math import fill_greeks_from_mid
    assert fill_greeks_from_mid(690, 650, 0, "put", 5.0) is None      # zero DTE
    assert fill_greeks_from_mid(690, 650, 36, "put", 0.0) is None     # no price
    assert fill_greeks_from_mid(690, 650, 36, "put", None) is None
    assert fill_greeks_from_mid(0, 650, 36, "put", 5.0) is None       # no spot
    # A mid below intrinsic can't be inverted -> None, never a garbage vol.
    assert fill_greeks_from_mid(690, 800, 36, "put", 1.0) is None
