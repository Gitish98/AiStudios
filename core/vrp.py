"""
Volatility Risk Premium — measure the premise the whole strategy rests on.

premium_harvest sells option premium. That is only a business if implied vol
systematically EXCEEDS the volatility that subsequently shows up: VRP = IV_t −
RV_{t..t+h}. Published research argues the index-option VRP has decayed toward
statistically zero over roughly the last 15 years. If that is true for the
instruments WE trade, then no amount of execution polish, sizing discipline or
risk engineering produces profit — those things bound losses on a zero-edge
process. So this is not an optimization; it is the go/no-go question.

Two ways to answer it, both served by the same pure functions here:
  1. NOW, on ~15 years of history: Cboe's VIX-family closes are 30-day implied
     vol for the index behind SPY/QQQ/IWM/DIA, and realized vol comes from the
     underlying's own closes. No waiting.
  2. LATER, on our own instruments: the same pairing over our banked ATM-IV
     snapshots, which measures what our own IV extraction actually captures.

THE UNIT TRAP THIS MODULE EXISTS TO PREVENT: VIX is quoted in VOL POINTS (18.70)
while realized vol from log returns is a DECIMAL (0.187). Subtracting one from
the other yields ~18.5 and looks like a spectacular premium. Every entry point
here normalizes to decimals and says so.

WHAT TO REPORT, AND WHY THE MEAN IS NOT ENOUGH: short-premium returns are
negatively skewed — many small wins, rare large losses. A positive MEAN VRP with
a deep negative tail can still be a losing business after costs, because the tail
arrives concentrated in single events. So `vrp_stats` reports the mean AND the
5th percentile AND the worst observation, and callers are expected to look at all
three.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Optional, Sequence

from .options_math import realized_vol

# VIX and friends are 30 CALENDAR-day implied vol ~= 21 trading days.
DEFAULT_HORIZON_TD = 21


def to_decimal_vol(v: float) -> float:
    """Normalize a volatility to a DECIMAL (0.187), accepting vol points (18.7).

    Cboe indices are published in points; our ATM-IV snapshots are decimals.
    Mixing them silently produces a ~100x wrong VRP that looks like a windfall.
    Anything above 1.5 is assumed to be points — no real annualized vol sits
    between 1.5 and 150% in decimal form, so the split is unambiguous in practice."""
    return v / 100.0 if v is not None and v > 1.5 else v


def forward_realized_vol(closes: Sequence[float], start_idx: int,
                         horizon: int = DEFAULT_HORIZON_TD) -> Optional[float]:
    """Annualized realized vol over the `horizon` trading days AFTER start_idx.

    Strictly forward-looking: the window begins at start_idx and uses only later
    closes, so a VRP computed from it contains no look-ahead. Returns None when
    the future window is incomplete — the most recent observations legitimately
    have no answer yet, and inventing one would bias the newest data."""
    if start_idx < 0 or horizon < 2:
        return None
    window = closes[start_idx:start_idx + horizon + 1]
    if len(window) < horizon + 1:      # not enough future yet
        return None
    return realized_vol(window)


def pair_iv_with_forward_rv(
    iv_by_date: dict[str, float],
    closes_by_date: dict[str, float],
    horizon: int = DEFAULT_HORIZON_TD,
) -> list[dict[str, Any]]:
    """Join an implied-vol series to the realized vol that FOLLOWED it.

    Both inputs are {YYYY-MM-DD: value}. Returns one row per date where both an
    IV reading and a complete forward window exist:
        {date, iv, rv, vrp}    (all vols as decimals; vrp = iv - rv)
    Dates present in only one series are skipped rather than interpolated."""
    dates = sorted(closes_by_date)
    idx = {d: i for i, d in enumerate(dates)}
    series = [closes_by_date[d] for d in dates]

    out: list[dict[str, Any]] = []
    for d in sorted(iv_by_date):
        i = idx.get(d)
        if i is None:
            continue                    # no price bar that day (holiday mismatch)
        rv = forward_realized_vol(series, i, horizon)
        if rv is None:
            continue                    # future window not complete yet
        iv = to_decimal_vol(float(iv_by_date[d]))
        out.append({"date": d, "iv": round(iv, 4), "rv": round(rv, 4),
                    "vrp": round(iv - rv, 4)})
    return out


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolated percentile on an already-sorted list."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def vrp_stats(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    """Distribution of the volatility risk premium.

    Reports the mean AND the tail, because a positive mean with a deep negative
    5th percentile describes a strategy that wins often and loses big — which is
    precisely how short-premium books die. `hit_rate` is how often IV exceeded
    subsequent RV at all; note that a high hit rate proves nothing on its own (our
    own backtest showed a 57% win rate with negative expectancy)."""
    n = len(pairs)
    if not n:
        return {"observations": 0, "note": "no paired observations yet"}
    vrps = sorted(p["vrp"] for p in pairs)
    mean = sum(vrps) / n
    hits = sum(1 for v in vrps if v > 0)
    return {
        "observations": n,
        "mean_vrp": round(mean, 4),
        "median_vrp": round(_percentile(vrps, 0.50), 4),
        "p05_vrp": round(_percentile(vrps, 0.05), 4),
        "p25_vrp": round(_percentile(vrps, 0.25), 4),
        "worst_vrp": round(vrps[0], 4),
        "hit_rate": round(hits / n, 4),
        "mean_iv": round(sum(p["iv"] for p in pairs) / n, 4),
        "mean_rv": round(sum(p["rv"] for p in pairs) / n, 4),
        "note": "VRP = IV - subsequent realized vol, in decimal vol. Positive mean "
                "means premium was, on average, overpriced. Judge the p05/worst tail "
                "too: short premium loses rarely and largely. Gross of costs.",
    }


def by_period(pairs: list[dict[str, Any]], years: int = 5) -> dict[str, Any]:
    """Split the sample into 'recent N years' vs everything older.

    The decay claim is specifically that the premium USED to exist and has since
    compressed; a single all-history average would hide exactly that. If recent
    VRP is materially thinner than the old regime, the strategy's premise is
    weaker than a full-sample number suggests."""
    if not pairs:
        return {"recent": vrp_stats([]), "older": vrp_stats([]), "cutoff": None}
    newest = max(p["date"] for p in pairs)
    try:
        cutoff = (_dt.date.fromisoformat(newest)
                  - _dt.timedelta(days=365 * years)).isoformat()
    except ValueError:
        return {"recent": vrp_stats(pairs), "older": vrp_stats([]), "cutoff": None}
    recent = [p for p in pairs if p["date"] >= cutoff]
    older = [p for p in pairs if p["date"] < cutoff]
    return {"cutoff": cutoff, "recent_years": years,
            "recent": vrp_stats(recent), "older": vrp_stats(older)}
