"""
THE RISK GATE — deterministic, non-bypassable, unit-tested.

Every order passes through `RiskGate.evaluate` before it can reach a broker. No
LLM touches this file's decisions. A rejection is final. The gate fails CLOSED:
any error or missing data => reject, never approve.

Enforces (per docs/05-risk-and-safety.md):
  - kill switch
  - paper is the default; LIVE is reachable ONLY via the five-gate go-live path
    (core.golive). When live, this gate adds the ramp notional/position caps as
    HARD limits and fails CLOSED if no ramp cap is present.
  - options approval level ceiling
  - defined-risk-only (no undefined/naked short options)
  - per-trade risk %, per-position notional %
  - portfolio heat, max concurrent + max daily new positions
  - max single-underlying %, gross leverage
  - daily loss limit -> trips the kill switch
  - PDT awareness for margin accounts under $25k

  - CLUSTER heat: correlated names (SPY/QQQ/DIA/XLK are one cluster) share a
    single defined-risk budget, so "6 positions" cannot become one leveraged bet.

Deferred (needs per-symbol GICS metadata; NOT silently treated as enforced):
  - max_sector_pct (needs GICS metadata)
  - max_overnight_risk_at_event_pct (needs an earnings calendar inside the gate;
    the earnings date exists in StrategyContext but is not passed to RiskContext)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .brokers.base import Account, OrderRequest, Position
from .clusters import cluster_exposure, cluster_of


@dataclass
class RiskLimits:
    per_trade_risk_pct: float = 0.005
    per_position_notional_pct: float = 0.05
    portfolio_heat_pct: float = 0.06
    max_concurrent_positions: int = 6
    max_daily_new_positions: int = 5
    max_single_underlying_pct: float = 0.08
    max_sector_pct: float = 0.25          # NOT enforced (needs GICS metadata)
    # Max DEFINED RISK in any one correlation cluster, as a fraction of equity.
    # Deliberately tighter than portfolio_heat_pct (0.06): heat bounds the whole
    # book, this bounds how much of it may be THE SAME BET. With 7 ETFs where
    # SPY/QQQ/DIA/XLK are one cluster, without this the heat cap alone would
    # happily allow the entire budget in a single beta exposure.
    max_cluster_risk_pct: float = 0.03
    # OVERNIGHT / GAP RISK (from the config's `overnight:` block). The daily-loss
    # kill switch is a continuous intraday trip and is ASLEEP across the session
    # boundary — it cannot bound a gap. This is the cap that can. Measured VRP
    # (cli.py vrp) puts the worst 21-day episode at RV 81% vs IV 14%: the tail is
    # the whole risk in short premium, and it arrives overnight.
    max_overnight_defined_risk_pct: float = 0.04
    max_gross_leverage: float = 1.0
    daily_loss_limit_pct: float = 0.03
    allow_naked_short_options: bool = False
    pdt_equity_threshold: float = 25000.0

    @classmethod
    def from_config(cls, risk: dict[str, Any],
                    overnight: Optional[dict[str, Any]] = None) -> "RiskLimits":
        """Build limits from the `risk:` block, plus the separate top-level
        `overnight:` block (whose keys were dead config until now). The tighter of
        max_overnight_defined_risk_pct / max_aggregate_gap_loss_pct wins: for a
        defined-risk-only book they measure the same quantity (see the gate)."""
        f = cls()
        merged = dict(risk or {})
        o = overnight or {}
        caps = [o.get(k) for k in
                ("max_overnight_defined_risk_pct", "max_aggregate_gap_loss_pct")
                if o.get(k) is not None]
        if caps:
            merged["max_overnight_defined_risk_pct"] = min(float(c) for c in caps)
        for k in vars(f):
            if k in merged and merged[k] is not None:
                setattr(f, k, type(getattr(f, k))(merged[k]))
        return f


@dataclass
class RiskContext:
    account: Account
    positions: list[Position]
    is_paper: bool
    kill_switch: bool
    positions_opened_today: int = 0
    day_trades_trailing_5: int = 0
    daily_pnl: float = 0.0  # realized+unrealized today, dollars (negative = loss)
    options_approval_level: int = 3
    region: str = "US"      # "US" -> FINRA PDT applies; "CA" -> no PDT (IBKR Canada)
    # LIVE ramp caps (None on paper). When live, the gate enforces these as HARD
    # additional limits and fails CLOSED if they are absent — a live run with no
    # ramp cap is rejected. Populated from core.golive.live_ramp_caps().
    live_ramp_max_notional: Optional[float] = None
    live_ramp_max_positions: Optional[int] = None


@dataclass
class Decision:
    approved: bool
    reasons: list[str] = field(default_factory=list)
    trip_kill_switch: bool = False

    def reject(self, msg: str) -> "Decision":
        self.approved = False
        self.reasons.append(msg)
        return self


class RiskGate:
    def __init__(self, limits: RiskLimits):
        self.limits = limits

    def evaluate(self, order: OrderRequest, ctx: RiskContext) -> Decision:
        d = Decision(approved=True)
        L = self.limits

        # 0. Fail-closed sanity.
        if ctx.account is None or ctx.account.equity <= 0:
            return d.reject("No account/equity available — fail closed.")

        equity = ctx.account.equity
        notional = _notional(order)  # capital footprint; reused by the caps below

        # 1. Kill switch.
        if ctx.kill_switch:
            return d.reject("Kill switch is ENGAGED — all orders rejected until cleared.")

        # 2. LIVE mode: permitted ONLY through the five-gate go-live ramp, and ONLY
        # with a binding ramp cap. The broker factory is the construction chokepoint
        # (a live adapter is built ONLY when core.golive.GoLiveGate passes); here the
        # gate adds the ramp notional/position caps as HARD additional limits. Fail
        # CLOSED: a live run with no ramp cap is rejected outright.
        if not ctx.is_paper:
            if not ctx.live_ramp_max_notional or ctx.live_ramp_max_notional <= 0:
                return d.reject(
                    "LIVE order without an active ramp cap — fail closed "
                    "(live requires go_live.ramp_tier >= 1 and the five-gate go-live path).")
            if notional > ctx.live_ramp_max_notional + 1e-6:
                d.reject(
                    f"LIVE ramp cap: position notional ${notional:.0f} exceeds tier "
                    f"cap ${ctx.live_ramp_max_notional:.0f}.")
            if (ctx.live_ramp_max_positions is not None
                    and len(ctx.positions) >= ctx.live_ramp_max_positions):
                d.reject(
                    f"LIVE ramp cap: at tier max {ctx.live_ramp_max_positions} "
                    "concurrent positions.")

        # 3. Daily loss limit -> trips kill switch.
        if ctx.daily_pnl <= -abs(L.daily_loss_limit_pct) * equity:
            d.trip_kill_switch = True
            return d.reject(
                f"Daily loss limit hit ({ctx.daily_pnl:.2f} <= "
                f"-{L.daily_loss_limit_pct*100:.1f}% of {equity:.0f}). Kill switch tripped."
            )

        # 4. Options approval ceiling.
        if order.required_approval_level > ctx.options_approval_level:
            d.reject(
                f"Strategy needs options level {order.required_approval_level}; "
                f"account is approved for {ctx.options_approval_level}."
            )

        # 5. Defined-risk only (no naked short options).
        if not L.allow_naked_short_options and _has_naked_short_option(order):
            d.reject("Undefined-risk (naked short option) blocked — defined-risk only.")
        if _is_short_premium(order) and order.max_loss <= 0:
            d.reject("Short-premium order has no defined max_loss — rejected (fail closed).")

        # 6. Per-trade risk.
        risk_dollars = order.max_loss if order.max_loss > 0 else notional
        if risk_dollars > L.per_trade_risk_pct * equity + 1e-6:
            d.reject(
                f"Per-trade risk ${risk_dollars:.0f} exceeds "
                f"{L.per_trade_risk_pct*100:.2f}% of equity (${L.per_trade_risk_pct*equity:.0f})."
            )

        # 7. Per-position notional.
        if notional > L.per_position_notional_pct * equity + 1e-6:
            d.reject(
                f"Position notional ${notional:.0f} exceeds "
                f"{L.per_position_notional_pct*100:.1f}% of equity."
            )

        # 7b. Portfolio heat — total DEFINED risk across open positions + this
        # order must stay under the cap. (Real broker positions imported without
        # a known max_loss contribute 0; intra-cycle synthesized positions carry
        # their max_loss, so accumulation within a run is enforced.)
        order_risk = order.max_loss if order.max_loss > 0 else notional
        existing_heat = sum(max(0.0, p.max_loss) for p in ctx.positions)
        if (existing_heat + order_risk) > L.portfolio_heat_pct * equity + 1e-6:
            d.reject(
                f"Portfolio heat ${existing_heat + order_risk:.0f} would exceed "
                f"{L.portfolio_heat_pct*100:.1f}% of equity "
                f"(${L.portfolio_heat_pct*equity:.0f})."
            )

        # 7c. CLUSTER heat — correlated names share ONE risk budget.
        # The single most important cap for this watchlist: SPY/QQQ/DIA/XLK are
        # largely the same bet, so six "diversified" spreads across them are close
        # to one leveraged position on US large-cap beta — the exact "silent
        # drift" failure docs/05 §0 names as design target #2. Risk inside a
        # cluster is SUMMED, never diversified away. Was configured and documented
        # as enforced for months while never being evaluated.
        cluster_risk = cluster_exposure(ctx.positions, order.underlying)
        if (cluster_risk + order_risk) > L.max_cluster_risk_pct * equity + 1e-6:
            d.reject(
                f"Cluster heat: {cluster_of(order.underlying)} would hold "
                f"${cluster_risk + order_risk:.0f} of defined risk "
                f"(> {L.max_cluster_risk_pct*100:.1f}% of equity, "
                f"${L.max_cluster_risk_pct*equity:.0f}). Correlated names share one budget."
            )

        # 7d. OVERNIGHT / GAP risk — the cap the daily-loss kill switch cannot be.
        # That switch is a continuous INTRADAY mark-to-market trip; it is asleep
        # across the session boundary, so it can never bound an overnight gap. The
        # `overnight:` config block promised this cap for months while no code
        # referenced it at all.
        # HONEST NOTE ON OVERLAP: every position this system holds is defined-risk
        # and multi-day (30-45 DTE), so "risk carried overnight" is the SAME set as
        # portfolio heat, and for defined-risk structures the worst-case aggregate
        # gap loss IS the sum of max_loss (the long wing caps it — that is the
        # point). So this check duplicates 7b's arithmetic with a TIGHTER bound,
        # and in practice becomes the binding heat constraint. It is kept separate
        # because the two limits answer different questions and would diverge the
        # moment an intraday or undefined-risk structure is ever added.
        overnight_risk = existing_heat + order_risk
        if overnight_risk > L.max_overnight_defined_risk_pct * equity + 1e-6:
            d.reject(
                f"Overnight gap risk ${overnight_risk:.0f} would exceed "
                f"{L.max_overnight_defined_risk_pct*100:.1f}% of equity "
                f"(${L.max_overnight_defined_risk_pct*equity:.0f}) — this is the "
                "worst case if every position gapped to max loss at once."
            )

        # NOTE — STILL NOT ENFORCED (needs per-symbol GICS metadata):
        #   max_sector_pct. A true sector cap differs from the cluster cap above
        #   (a sector can span clusters and vice versa). Documented in config but
        #   deliberately NOT silently treated as enforced. See docs/07 (Phase 2).

        # 8. Concurrency caps.
        if len(ctx.positions) >= L.max_concurrent_positions:
            d.reject(f"At max concurrent positions ({L.max_concurrent_positions}).")
        if ctx.positions_opened_today >= L.max_daily_new_positions:
            d.reject(f"At max new positions for today ({L.max_daily_new_positions}).")

        # 9. Single-underlying concentration (existing + this order).
        existing = _underlying_exposure(ctx.positions, order.underlying)
        if (existing + notional) > L.max_single_underlying_pct * equity + 1e-6:
            d.reject(
                f"Single-underlying exposure on {order.underlying} would be "
                f"${existing+notional:.0f} (> {L.max_single_underlying_pct*100:.1f}% of equity)."
            )

        # 10. Gross leverage (existing notional + this order vs equity).
        gross = sum(abs(p.market_value) for p in ctx.positions) + notional
        if gross > L.max_gross_leverage * equity + 1e-6:
            d.reject(
                f"Gross exposure ${gross:.0f} would exceed "
                f"{L.max_gross_leverage:.2f}x equity."
            )

        # 11. Buying-power guard (margin maintenance). An order's collateral must
        # fit available buying power. For a CA margin account this — not PDT — is
        # the binding regulatory constraint.
        if ctx.account.buying_power > 0 and notional > ctx.account.buying_power + 1e-6:
            d.reject(
                f"Order notional ${notional:.0f} exceeds available buying power "
                f"${ctx.account.buying_power:.0f}."
            )

        # 12. PDT awareness — US (FINRA) ONLY. Canada has no Pattern Day Trader
        # rule, so this is skipped for a CA account.
        if (ctx.region.upper() == "US"
                and ctx.account.account_type == "margin"
                and equity < L.pdt_equity_threshold
                and order.is_day_trade
                and ctx.day_trades_trailing_5 >= 3):
            d.reject(
                "PDT guard: US margin account under "
                f"${L.pdt_equity_threshold:.0f} already has {ctx.day_trades_trailing_5} "
                "day trades in 5 sessions; a 4th would flag Pattern Day Trader."
            )

        return d


# ── helpers ──────────────────────────────────────────────────────────────────

def _notional(order: OrderRequest) -> float:
    """Approx capital footprint (the buying-power a broker reserves)."""
    # Iron condor: collateral is the WIDER single-side wing, not the outer span
    # (only one side can be ITM at expiry). Span would overstate ~10x.
    if order.strategy == "iron_condor":
        puts = sorted(l.strike for l in order.legs
                      if l.option_type == "put" and l.strike is not None)
        calls = sorted(l.strike for l in order.legs
                       if l.option_type == "call" and l.strike is not None)
        put_w = (puts[-1] - puts[0]) if len(puts) >= 2 else 0.0
        call_w = (calls[-1] - calls[0]) if len(calls) >= 2 else 0.0
        qty = max((abs(l.qty) for l in order.legs), default=1)
        return max(put_w, call_w) * 100.0 * qty
    # Debit verticals: footprint is the single net DEBIT paid (== max loss), not
    # legs × debit. Falling through to the generic per-leg loop would double-count.
    if order.strategy in ("call_debit_spread", "put_debit_spread"):
        qty = max((abs(l.qty) for l in order.legs), default=1)
        if order.limit_price:
            return abs(order.limit_price) * 100.0 * qty
        return order.max_loss

    # Two-leg defined-risk credit spreads: collateral = strike width * 100 * qty.
    if order.strategy in ("put_credit_spread", "call_credit_spread"):
        strikes = [l.strike for l in order.legs if l.strike is not None]
        if len(strikes) >= 2:
            width = abs(max(strikes) - min(strikes))
            qty = max((abs(l.qty) for l in order.legs), default=1)
            return width * 100.0 * qty
        return order.max_loss
    # Cash-secured put: footprint is the cash securing it (strike * 100 * qty).
    if order.strategy == "cash_secured_put" and order.legs:
        leg = order.legs[0]
        if leg.strike:
            return abs(leg.qty) * 100.0 * leg.strike
    total = 0.0
    for leg in order.legs:
        mult = 100.0 if leg.asset_class == "option" else 1.0
        price = order.limit_price if order.limit_price else (leg.strike or 0.0)
        total += abs(leg.qty) * mult * (price or 0.0)
    return total


def _underlying_exposure(positions: list[Position], underlying: str) -> float:
    if not underlying:
        return 0.0
    return sum(
        abs(p.market_value) for p in positions
        if (p.underlying or p.symbol).upper() == underlying.upper()
    )


def _has_naked_short_option(order: OrderRequest) -> bool:
    """Structural naked-option detection — does NOT trust strategy-supplied
    max_loss (a naked short mislabeled with a fabricated max_loss must still be
    caught). Naked = a short option leg whose risk is not structurally bounded by
    (a) a same-type long protective leg, or (b) declared collateral (CSP/covered
    call)."""
    short_opts = [l for l in order.legs if l.asset_class == "option" and l.side == "sell"]
    if not short_opts:
        return False
    # Collateralized single-leg strategies are defined-risk by cash/shares.
    if order.strategy in ("cash_secured_put", "covered_call"):
        return False
    long_opts = [l for l in order.legs if l.asset_class == "option" and l.side == "buy"]
    # Each short option type must have at least as many long protectors of the
    # same type (a vertical/condor wing). Otherwise some short is uncovered.
    for ot in ("put", "call"):
        shorts = sum(1 for l in short_opts if l.option_type == ot)
        longs = sum(1 for l in long_opts if l.option_type == ot)
        if shorts > longs:
            return True
    return False


def _is_short_premium(order: OrderRequest) -> bool:
    return any(l.asset_class == "option" and l.side == "sell" for l in order.legs)
