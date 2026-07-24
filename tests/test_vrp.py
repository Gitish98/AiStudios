"""
Volatility-risk-premium measurement. Fully offline and deterministic.

Locks the traps that would make the go/no-go answer wrong rather than merely
imprecise:
  - the VIX-points vs decimal-vol unit mismatch (a ~100x fake premium)
  - look-ahead (the RV window must start AFTER the IV reading)
  - incomplete forward windows returning None rather than a biased estimate
"""

from __future__ import annotations

import math

from core.vrp import (
    DEFAULT_HORIZON_TD, by_period, forward_realized_vol, pair_iv_with_forward_rv,
    to_decimal_vol, vrp_stats,
)


def test_unit_normalisation_prevents_the_100x_fake_premium():
    # VIX prints 18.70 (vol points); our ATM snapshots are 0.187 (decimal).
    # Subtracting points from decimals yields ~18.5 and looks like a windfall.
    assert to_decimal_vol(18.70) == 0.187
    assert to_decimal_vol(0.187) == 0.187      # already decimal, untouched
    assert to_decimal_vol(95.0) == 0.95        # VVIX-scale input
    assert to_decimal_vol(1.2) == 1.2          # 120% vol is plausible as a decimal


def _closes(n, daily_ret=0.0, start=100.0):
    """Deterministic close series with a constant log return (=> ~zero realized vol)."""
    out, p = [], start
    for _ in range(n):
        out.append(round(p, 6))
        p *= math.exp(daily_ret)
    return out


def test_forward_realized_vol_is_strictly_forward_looking():
    # Flat before index 30, volatile after. RV measured AT 30 must see the FUTURE
    # volatility, not the calm past — otherwise the VRP contains look-ahead.
    calm = _closes(31)
    vol = []
    p = calm[-1]
    for i in range(30):
        p *= math.exp(0.02 if i % 2 == 0 else -0.02)
        vol.append(round(p, 6))
    series = calm + vol

    rv_at_30 = forward_realized_vol(series, 30, horizon=21)
    assert rv_at_30 is not None and rv_at_30 > 0.20   # sees the coming turbulence
    rv_at_0 = forward_realized_vol(series, 0, horizon=21)
    assert rv_at_0 is not None and rv_at_0 < 0.01     # calm window stays calm


def test_incomplete_future_window_returns_none_not_a_guess():
    series = _closes(10)
    assert forward_realized_vol(series, 0, horizon=21) is None   # not enough future
    assert forward_realized_vol(series, 9, horizon=5) is None
    assert forward_realized_vol(series, -1, horizon=5) is None


def test_pairing_skips_dates_without_both_sides():
    closes = {f"2026-01-{d:02d}": 100.0 + d for d in range(1, 29)}
    iv = {"2026-01-01": 20.0,          # vol points -> normalised
          "2026-01-02": 0.20,          # decimal -> untouched
          "1999-01-01": 0.30}          # no matching close bar -> skipped
    pairs = pair_iv_with_forward_rv(iv, closes, horizon=5)
    dates = [p["date"] for p in pairs]
    assert "1999-01-01" not in dates
    assert all(p["iv"] < 1.5 for p in pairs), "IV must be normalised to decimals"
    assert all(round(p["iv"] - p["rv"], 4) == p["vrp"] for p in pairs)


def test_vrp_stats_reports_the_tail_not_just_the_mean():
    # A book that wins often and loses big: 9 small positives, 1 large negative.
    pairs = [{"date": f"2026-01-{i:02d}", "iv": 0.20, "rv": 0.18, "vrp": 0.02}
             for i in range(1, 10)]
    pairs.append({"date": "2026-01-10", "iv": 0.20, "rv": 0.80, "vrp": -0.60})
    s = vrp_stats(pairs)
    assert s["observations"] == 10
    assert s["hit_rate"] == 0.9              # a high hit rate...
    assert s["mean_vrp"] < 0                 # ...with a NEGATIVE mean
    assert s["worst_vrp"] == -0.60
    assert s["p05_vrp"] < 0                  # the tail is visible, not averaged away


def test_vrp_stats_empty_is_honest():
    s = vrp_stats([])
    assert s["observations"] == 0 and "no paired observations" in s["note"]


def test_by_period_splits_recent_from_older():
    old = [{"date": f"2015-01-{i:02d}", "iv": 0.25, "rv": 0.15, "vrp": 0.10}
           for i in range(1, 11)]
    new = [{"date": f"2026-01-{i:02d}", "iv": 0.18, "rv": 0.175, "vrp": 0.005}
           for i in range(1, 11)]
    split = by_period(old + new, years=5)
    # The decay story: a fat old premium and a thin recent one must not be
    # averaged into one comfortable number.
    assert split["older"]["mean_vrp"] == 0.10
    assert split["recent"]["mean_vrp"] == 0.005
    assert split["recent"]["mean_vrp"] < split["older"]["mean_vrp"]
