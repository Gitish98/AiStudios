"""
Earnings-Vol — harvest the earnings volatility risk premium.

The edge (and it is a real, documented one): option implied volatility is
SYSTEMATICALLY overpriced going into an earnings report, then collapses ("IV
crush") right after. On average, the implied move priced into the options is
larger than the move that actually happens. So the disciplined play is to SELL
defined-risk premium when the implied move looks rich, and let the crush work.

We express it as an IRON CONDOR (defined risk, market-neutral): sell a put spread
below the expected move and a call spread above it, collect the credit, and exit
into the post-earnings IV collapse.

We deliberately DO NOT trade the other side ("buy a cheap straddle when implied
move is low"). That needs the realized move to beat the implied move AND is a
coin-flip on direction — it does not carry the same statistical edge, and an
honest system shouldn't pretend it does.

Strict filters (this fires rarely, only into a real, rich event):
  - earnings within [min_days, max_days] (be positioned BEFORE the report)
  - the chain expiry covers the event
  - implied move (from the ATM straddle) is RICH vs a realized-vol baseline
  - IV rank is elevated (a second confirmation that premium is fat)
  - liquidity on all four legs; credit/width worth the risk
"""

from __future__ import annotations

import math
from datetime import date

from core.brokers.base import OrderLeg
from core.options_math import MIN_IV_OBSERVATIONS, implied_move, iv_rank, realized_vol
from .base import Signal, Strategy, StrategyContext

def _resolve_iv_rank(ctx, atm_iv, min_obs):
    """IV rank + provenance. Prefers a real benchmark series supplied by execution
    (252-day, from core.data.cboe); otherwise ranks our own ATM IV against our own
    bootstrapped history. Never mixes the two — the current reading and the
    history always come from the same series."""
    if ctx.iv_rank_value is not None:
        return ctx.iv_rank_value, (ctx.iv_rank_source or "external"), ctx.iv_rank_observations
    return iv_rank(atm_iv, ctx.iv_history, min_obs), "bootstrap", len(ctx.iv_history or [])

from strategies.premium_harvest import _atm_iv


class EarningsVol(Strategy):
    name = "earnings_vol"

    def __init__(self, params: dict | None = None):
        p = params or {}
        self.min_days = int(p.get("min_days_to_earnings", 1))
        self.max_days = int(p.get("max_days_to_earnings", 7))
        self.rich_threshold = float(p.get("rich_threshold", 1.20))  # implied/realized expected move
        self.min_iv_rank = float(p.get("min_iv_rank", 0.50))
        self.min_iv_observations = int(p.get("min_iv_observations", MIN_IV_OBSERVATIONS))
        self.wing_width = float(p.get("wing_width", 5.0))
        self.min_open_interest = int(p.get("min_open_interest", 250))
        self.max_spread_pct = float(p.get("max_spread_pct", 0.18))
        self.min_credit_ratio = float(p.get("min_credit_ratio", 0.20))
        self.required_level = int(p.get("required_approval_level", 3))

    def generate(self, ctx: StrategyContext) -> list[Signal]:
        if not ctx.earnings_date or not ctx.asof or ctx.spot <= 0 or not ctx.option_chain:
            return []
        days_to = (date.fromisoformat(ctx.earnings_date) - date.fromisoformat(ctx.asof)).days
        if not (self.min_days <= days_to <= self.max_days):
            return []

        # The chosen expiry (sim returns one) must be AFTER the event.
        dte = ctx.option_chain[0].dte
        if dte < days_to:
            return []

        # Implied move from the ATM straddle.
        atm_call = _nearest(ctx.option_chain, "call", ctx.spot)
        atm_put = _nearest(ctx.option_chain, "put", ctx.spot)
        if not atm_call or not atm_put:
            return []
        im_pct = implied_move(atm_call.mid + atm_put.mid, ctx.spot)
        if im_pct is None or im_pct <= 0:
            return []

        # Realized-vol baseline over the same horizon (a sober reference move).
        rv = realized_vol(ctx.closes) if ctx.closes else None
        if rv is None:
            return []
        hist_move_pct = (rv / math.sqrt(252.0)) * math.sqrt(max(dte, 1))
        if hist_move_pct <= 0 or im_pct / hist_move_pct < self.rich_threshold:
            return []  # implied move is NOT rich enough vs realized — stand aside

        # Second confirmation: elevated IV rank.
        atm_iv = _atm_iv(ctx.option_chain, ctx.spot)
        if atm_iv is None or not ctx.iv_history:
            return []
        ivr, iv_src, iv_n = _resolve_iv_rank(atm_iv=atm_iv, ctx=ctx,
                                            min_obs=self.min_iv_observations)
        if ivr is None or ivr < self.min_iv_rank:
            return []

        return self._build_condor(ctx, im_pct, ivr, days_to, iv_src, iv_n)

    def _build_condor(self, ctx, im_pct, ivr, days_to,
                      iv_src="", iv_n=0) -> list[Signal]:
        spot, exp = ctx.spot, ctx.option_chain[0].expiration
        puts = sorted({c.strike for c in ctx.option_chain if c.option_type == "put"})
        calls = sorted({c.strike for c in ctx.option_chain if c.option_type == "call"})

        # Sell just OUTSIDE the expected move; buy the wings `wing_width` further out.
        sp = _le(puts, spot * (1 - im_pct))            # short put <= lower edge
        sc = _ge(calls, spot * (1 + im_pct))            # short call >= upper edge
        if sp is None or sc is None:
            return []
        lp = _le(puts, sp - self.wing_width)            # long put further OTM
        lc = _ge(calls, sc + self.wing_width)           # long call further OTM
        if lp is None or lc is None:
            return []

        legs_c = {c.strike: c for c in ctx.option_chain if c.option_type == "call"}
        legs_p = {c.strike: c for c in ctx.option_chain if c.option_type == "put"}
        try:
            SP, LP, SC, LC = legs_p[sp], legs_p[lp], legs_c[sc], legs_c[lc]
        except KeyError:
            return []

        # Every leg must have a real two-sided market.
        for leg in (SP, LP, SC, LC):
            if (leg.open_interest < self.min_open_interest or leg.spread_pct > self.max_spread_pct
                    or leg.bid <= 0 or leg.ask <= 0):
                return []

        credit = round((SP.mid - LP.mid) + (SC.mid - LC.mid), 2)
        width = round(max(sp - lp, lc - sc), 2)
        if credit <= 0 or width <= 0 or credit / width < self.min_credit_ratio:
            return []
        max_loss = round((width - credit) * 100, 2)
        if max_loss <= 0:
            return []

        legs = [
            OrderLeg(SP.symbol, "sell", 1, "option", "put", strike=sp, expiration=exp),
            OrderLeg(LP.symbol, "buy", 1, "option", "put", strike=lp, expiration=exp),
            OrderLeg(SC.symbol, "sell", 1, "option", "call", strike=sc, expiration=exp),
            OrderLeg(LC.symbol, "buy", 1, "option", "call", strike=lc, expiration=exp),
        ]
        rationale = (
            f"Iron condor {ctx.underlying} {lp:g}/{sp:g}p–{sc:g}/{lc:g}c {exp} "
            f"({ctx.option_chain[0].dte}DTE): earnings in {days_to}d, implied move "
            f"{im_pct:.1%} (rich vs realized), IVR {ivr:.0%}, "
            f"credit ${credit*100:.0f}, max loss ${max_loss:.0f}."
        )
        return [Signal(
            strategy="iron_condor", underlying=ctx.underlying, legs=legs,
            rationale=rationale, limit_price=credit, est_credit=round(credit * 100, 2),
            max_loss=max_loss, required_approval_level=self.required_level,
            is_day_trade=False,
            meta={"implied_move": im_pct, "iv_rank": ivr, "iv_rank_source": iv_src, "iv_obs": iv_n, "days_to_earnings": days_to,
                  "structure": "iron_condor", "width": width},
        )]


def _nearest(chain, right, spot):
    cands = [c for c in chain if c.option_type == right]
    return min(cands, key=lambda c: abs(c.strike - spot)) if cands else None


def _le(strikes, target):
    """Largest strike <= target."""
    below = [s for s in strikes if s <= target]
    return max(below) if below else None


def _ge(strikes, target):
    """Smallest strike >= target."""
    above = [s for s in strikes if s >= target]
    return min(above) if above else None
