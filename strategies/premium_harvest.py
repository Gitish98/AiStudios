"""
Premium Harvesting — Phase 0 starter engine (PUT CREDIT SPREADS on liquid ETFs).

Why credit spreads and not cash-secured puts? On a small account a CSP on a
$500 ETF ties up $50k of collateral — it simply doesn't fit. A put credit spread
is DEFINED-RISK (max loss = width − credit), fits a small account, and is exactly
what your options Level 3 approval is for. It collects premium when implied vol
is elevated and mean-reverts — the "collect premiums during cycles" intent.

Mechanics (see docs/04-strategies.md §2):
  - Only sell premium when IV RANK is elevated (richer premium).
  - SHORT a put near a target delta (~0.25) in a 30–45 DTE window.
  - BUY the next strike down to define risk (the long wing).
  - Require liquidity (open interest + tight bid/ask) on the short leg.
  - max_loss = (width − net_credit) × 100, fully defined.

This is intentionally conservative and ETF-only — no single-stock earnings binary.
"""

from __future__ import annotations

from core.brokers.base import OptionContract, OrderLeg
from core.options_math import MIN_IV_OBSERVATIONS, iv_rank
from .base import Signal, Strategy, StrategyContext


class PremiumHarvest(Strategy):
    name = "premium_harvest"

    def __init__(self, params: dict | None = None):
        p = params or {}
        self.min_iv_rank = float(p.get("min_iv_rank", 0.40))
        # Minimum IV observations before an IV rank is trusted at all. A short
        # window manufactures ~100% ranks in a gently rising regime — see
        # core.options_math.iv_rank.
        self.min_iv_observations = int(p.get("min_iv_observations", MIN_IV_OBSERVATIONS))
        self.target_delta = float(p.get("target_put_delta", 0.25))
        self.delta_lo = float(p.get("delta_min", 0.12))
        self.delta_hi = float(p.get("delta_max", 0.40))
        self.dte_min = int(p.get("dte_min", 30))
        self.dte_max = int(p.get("dte_max", 45))
        self.min_open_interest = int(p.get("min_open_interest", 250))
        self.max_spread_pct = float(p.get("max_spread_pct", 0.15))
        self.min_credit_ratio = float(p.get("min_credit_ratio", 0.20))  # credit / width
        self.required_level = int(p.get("required_approval_level", 3))   # spreads => level 3

    def generate(self, ctx: StrategyContext) -> list[Signal]:
        chain = ctx.option_chain
        if not chain or ctx.spot <= 0:
            return []

        atm_iv = _atm_iv(chain, ctx.spot)
        if atm_iv is None or not ctx.iv_history:
            return []
        rank = iv_rank(atm_iv, ctx.iv_history, self.min_iv_observations)
        if rank is None or rank < self.min_iv_rank:
            return []

        puts = [
            c for c in chain
            if c.option_type == "put"
            and self.dte_min <= c.dte <= self.dte_max
            and c.strike < ctx.spot
            and c.delta is not None
            and self.delta_lo <= abs(c.delta) <= self.delta_hi
        ]
        if len(puts) < 2:
            return []

        # SHORT put: closest |delta| to target.
        puts.sort(key=lambda c: abs(abs(c.delta) - self.target_delta))
        short = puts[0]

        # Liquidity on the leg we're short.
        if short.open_interest < self.min_open_interest or short.spread_pct > self.max_spread_pct:
            return []

        # LONG wing: the nearest strike BELOW the short, same expiration.
        lower = [
            c for c in chain
            if c.option_type == "put" and c.expiration == short.expiration
            and c.strike < short.strike
        ]
        if not lower:
            return []
        long_leg = max(lower, key=lambda c: c.strike)  # nearest strike below short

        width = round(short.strike - long_leg.strike, 2)
        net_credit = round(short.mid - long_leg.mid, 2)
        if width <= 0 or net_credit <= 0:
            return []
        if net_credit / width < self.min_credit_ratio:
            return []  # not enough premium for the risk

        credit_dollars = round(net_credit * 100, 2)
        max_loss = round((width - net_credit) * 100, 2)
        if max_loss <= 0:
            return []

        legs = [
            OrderLeg(symbol=short.symbol, side="sell", qty=1, asset_class="option",
                     option_type="put", strike=short.strike, expiration=short.expiration),
            OrderLeg(symbol=long_leg.symbol, side="buy", qty=1, asset_class="option",
                     option_type="put", strike=long_leg.strike, expiration=long_leg.expiration),
        ]
        rationale = (
            f"Put credit spread {ctx.underlying} {short.strike:g}/{long_leg.strike:g}P "
            f"{short.expiration} ({short.dte}DTE): IVR {rank:.0%} >= {self.min_iv_rank:.0%}, "
            f"short delta {short.delta:.2f}, credit ${credit_dollars:.0f}, "
            f"max loss ${max_loss:.0f}, OI {short.open_interest}."
        )
        return [Signal(
            strategy="put_credit_spread",
            underlying=ctx.underlying,
            legs=legs,
            rationale=rationale,
            limit_price=net_credit,           # net credit (positive)
            est_credit=credit_dollars,
            max_loss=max_loss,
            required_approval_level=self.required_level,
            is_day_trade=False,
            meta={"iv_rank": rank, "short_delta": short.delta, "dte": short.dte,
                  "short_strike": short.strike, "long_strike": long_leg.strike,
                  "width": width, "expiration": short.expiration},
        )]


def _atm_iv(chain: list[OptionContract], spot: float) -> float | None:
    candidates = [c for c in chain if c.implied_vol and c.implied_vol > 0]
    if not candidates:
        return None
    nearest = min(candidates, key=lambda c: abs(c.strike - spot))
    return nearest.implied_vol
