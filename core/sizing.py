"""
Position sizing — how many contracts, given the account and the trade's risk.

Fixed-fractional: target risking `per_trade_risk_pct` of equity per trade, sized
off the trade's DEFINED max loss per contract. Floors to a whole number, so if a
single contract already exceeds the per-trade risk budget, the size is ZERO and
the trade is skipped — the honest answer for a small account ("this trade is too
big for your risk budget"), not a silent over-bet.

The gate is still the ceiling: sizing proposes, the deterministic risk gate
disposes (it re-checks the SIZED order against every cap). The advisor can only
scale the result DOWN (allocation_mult in [0,1]).

Disabled by default (size = 1 contract) so existing behavior is unchanged unless
`sizing.enabled` is set.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class SizeParams:
    enabled: bool = False
    per_trade_risk_pct: float = 0.005   # fraction of equity to risk per trade
    max_contracts: int = 10             # hard cap on contracts per position

    @classmethod
    def from_config(cls, config: Any) -> "SizeParams":
        raw = (getattr(config, "raw", None) or {})
        sizing = raw.get("sizing", {}) or {}
        risk = raw.get("risk", {}) or {}
        f = cls()
        f.enabled = bool(sizing.get("enabled", False))
        # Size to the SAME per-trade risk the gate enforces, unless overridden.
        f.per_trade_risk_pct = float(sizing.get("per_trade_risk_pct",
                                                risk.get("per_trade_risk_pct", f.per_trade_risk_pct)))
        f.max_contracts = int(sizing.get("max_contracts", f.max_contracts))
        return f


def size_contracts(max_loss_per_contract: float, equity: float,
                   params: SizeParams, allocation_mult: float = 1.0) -> int:
    """Contracts to trade. Returns 1 when sizing is disabled (legacy behavior),
    0 when even one contract exceeds the per-trade risk budget."""
    if not params.enabled:
        return 1
    if max_loss_per_contract <= 0 or equity <= 0:
        return 1
    target_risk = params.per_trade_risk_pct * equity
    n = int(target_risk // max_loss_per_contract)          # floor -> 0 if too big
    n = max(0, min(params.max_contracts, n))
    mult = max(0.0, min(1.0, allocation_mult))
    if mult < 1.0:
        n = int(n * mult)
    return n
