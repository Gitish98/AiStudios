"""
Volatility-Compression Breakout — own cheap premium into an expansion.

Thesis: volatility mean-reverts. After a tight squeeze (Bollinger bandwidth in a
low percentile, plus NR7 / inside-day / ATR contraction), range tends to expand.
When option IV is ALSO low (cheap), the disciplined expression is a DEFINED-RISK
DEBIT vertical in the direction the range is leaning:
  - upper half of range  -> bull CALL debit spread
  - lower half of range  -> bear PUT  debit spread

Why a debit spread and not a long single option: defined, capped cost; cheaper
than an outright long; and it caps the win — we are harvesting a *probable
expansion*, not betting the farm on a moonshot. Max loss = the debit paid.

Filters are deliberately strict (this should fire rarely):
  - bandwidth percentile <= squeeze_pct  (a real squeeze)
  - at least one of: NR7, inside day      (range contraction confirmation)
  - ATR(fast)/ATR(slow) <= atr_contraction
  - IV rank <= max_iv_rank                 (premium is cheap to BUY)
  - clear directional lean (range_position outside the neutral band)
  - liquidity on both legs; debit/width within a sane band
"""

from __future__ import annotations

from core.indicators import (
    atr_contraction_ratio, bandwidth_percentile, inside_day, nr7, range_position,
)
from core.options_math import MIN_IV_OBSERVATIONS, iv_rank
from .base import Signal, Strategy, StrategyContext

def _resolve_iv_rank(ctx, atm_iv, min_obs):
    """IV rank + provenance. Prefers a real benchmark series supplied by execution
    (252-day, from core.data.cboe); otherwise ranks our own ATM IV against our own
    bootstrapped history. Never mixes the two — the current reading and the
    history always come from the same series."""
    if ctx.iv_rank_value is not None:
        return ctx.iv_rank_value, (ctx.iv_rank_source or "external"), ctx.iv_rank_observations
    return iv_rank(atm_iv, ctx.iv_history, min_obs), "bootstrap", len(ctx.iv_history or [])

from strategies.premium_harvest import _atm_iv  # reuse ATM-IV helper
from core.brokers.base import OrderLeg


class VolatilityBreakout(Strategy):
    name = "volatility_breakout"

    def __init__(self, params: dict | None = None):
        p = params or {}
        self.squeeze_pct = float(p.get("squeeze_pct", 0.20))
        self.atr_contraction = float(p.get("atr_contraction", 0.85))
        self.max_iv_rank = float(p.get("max_iv_rank", 0.35))
        self.min_iv_observations = int(p.get("min_iv_observations", MIN_IV_OBSERVATIONS))
        self.range_hi = float(p.get("range_high", 0.65))   # >= -> bullish lean
        self.range_lo = float(p.get("range_low", 0.35))    # <= -> bearish lean
        self.dte_min = int(p.get("dte_min", 25))
        self.dte_max = int(p.get("dte_max", 45))
        self.target_long_delta = float(p.get("target_long_delta", 0.45))
        self.min_open_interest = int(p.get("min_open_interest", 250))
        self.max_spread_pct = float(p.get("max_spread_pct", 0.15))
        self.max_debit_ratio = float(p.get("max_debit_ratio", 0.55))  # debit/width ceiling
        self.required_level = int(p.get("required_approval_level", 3))

    def generate(self, ctx: StrategyContext) -> list[Signal]:
        closes, highs, lows = ctx.closes, ctx.highs, ctx.lows
        if len(closes) < 60 or ctx.spot <= 0 or not ctx.option_chain:
            return []

        # 1. Squeeze present?
        bw_pct = bandwidth_percentile(closes)
        if bw_pct is None or bw_pct > self.squeeze_pct:
            return []
        if not (nr7(highs, lows) or inside_day(highs, lows)):
            return []
        atr_ratio = atr_contraction_ratio(highs, lows, closes)
        if atr_ratio is None or atr_ratio > self.atr_contraction:
            return []

        # 2. Premium must be cheap to BUY.
        atm_iv = _atm_iv(ctx.option_chain, ctx.spot)
        if atm_iv is None or not ctx.iv_history:
            return []
        ivr, iv_src, iv_n = _resolve_iv_rank(atm_iv=atm_iv, ctx=ctx,
                                            min_obs=self.min_iv_observations)
        if ivr is None or ivr > self.max_iv_rank:
            return []

        # 3. Directional lean.
        rpos = range_position(closes)
        if rpos is None:
            return []
        if rpos >= self.range_hi:
            family, bullish = "call", True
        elif rpos <= self.range_lo:
            family, bullish = "put", False
        else:
            return []  # no clear lean -> stand aside

        return self._build_debit_spread(ctx, family, bullish, bw_pct, ivr, rpos,
                                       iv_src, iv_n)

    def _build_debit_spread(self, ctx, family, bullish, bw_pct, ivr, rpos,
                            iv_src="", iv_n=0) -> list[Signal]:
        chain = [c for c in ctx.option_chain
                 if c.option_type == family
                 and self.dte_min <= c.dte <= self.dte_max
                 and c.delta is not None]
        if len(chain) < 2:
            return []

        # LONG leg ~0.45 delta (slightly ITM/ATM), SHORT one strike further OTM.
        chain.sort(key=lambda c: abs(abs(c.delta) - self.target_long_delta))
        long_leg = chain[0]
        if (long_leg.open_interest < self.min_open_interest
                or long_leg.spread_pct > self.max_spread_pct
                or long_leg.bid <= 0 or long_leg.ask <= 0):
            return []

        # Short leg: next strike in the OTM direction (calls -> higher; puts -> lower).
        if family == "call":
            outers = [c for c in chain if c.strike > long_leg.strike]
            short_leg = min(outers, key=lambda c: c.strike) if outers else None
        else:
            outers = [c for c in chain if c.strike < long_leg.strike]
            short_leg = max(outers, key=lambda c: c.strike) if outers else None
        if short_leg is None:
            return []
        # The short leg must ALSO have a real two-sided market — otherwise its mid
        # silently falls back to `last`, mis-pricing the debit/max_loss/limit.
        if (short_leg.open_interest < self.min_open_interest
                or short_leg.spread_pct > self.max_spread_pct
                or short_leg.bid <= 0 or short_leg.ask <= 0):
            return []

        width = round(abs(short_leg.strike - long_leg.strike), 2)
        debit = round(long_leg.mid - short_leg.mid, 2)
        if width <= 0 or debit <= 0 or debit / width > self.max_debit_ratio:
            return []

        debit_dollars = round(debit * 100, 2)
        max_loss = debit_dollars
        structure = "call_debit_spread" if family == "call" else "put_debit_spread"
        legs = [
            OrderLeg(symbol=long_leg.symbol, side="buy", qty=1, asset_class="option",
                     option_type=family, strike=long_leg.strike, expiration=long_leg.expiration),
            OrderLeg(symbol=short_leg.symbol, side="sell", qty=1, asset_class="option",
                     option_type=family, strike=short_leg.strike, expiration=short_leg.expiration),
        ]
        direction = "bull" if bullish else "bear"
        rationale = (
            f"{direction} {family} debit spread {ctx.underlying} "
            f"{long_leg.strike:g}/{short_leg.strike:g} {long_leg.expiration} "
            f"({long_leg.dte}DTE): squeeze bw%={bw_pct:.0%}, IVR {ivr:.0%} (cheap), "
            f"range pos {rpos:.0%}, debit ${debit_dollars:.0f}, max loss ${max_loss:.0f}."
        )
        return [Signal(
            strategy=structure,
            underlying=ctx.underlying,
            legs=legs,
            rationale=rationale,
            limit_price=debit,                  # net debit (positive price)
            est_credit=-debit_dollars,          # negative => debit (our sign convention)
            max_loss=max_loss,
            required_approval_level=self.required_level,
            is_day_trade=False,
            meta={"bandwidth_pct": bw_pct, "iv_rank": ivr, "iv_rank_source": iv_src, "iv_obs": iv_n, "range_pos": rpos,
                  "structure": structure, "width": width},
        )]
