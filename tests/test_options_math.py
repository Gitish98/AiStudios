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
    hist = [0.10, 0.20, 0.30, 0.40, 0.50]
    assert abs(om.iv_rank(0.30, hist) - 0.5) < 1e-9
    assert abs(om.iv_rank(0.50, hist) - 1.0) < 1e-9
    assert abs(om.iv_rank(0.10, hist) - 0.0) < 1e-9
    # current above history clamps to 1.0
    assert om.iv_rank(0.99, hist) == 1.0


def test_iv_rank_needs_history():
    assert om.iv_rank(0.3, []) is None
    assert om.iv_rank(0.3, [0.3]) is None  # degenerate


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
