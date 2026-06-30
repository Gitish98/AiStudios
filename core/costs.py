"""
Cost model — commissions + modeled slippage. Pure, deterministic, tested.

This exists because frictionless backtests lie. On small accounts the per-trade
cost (commissions + half the bid-ask) often eats 10-25% of a credit-spread's
premium, which is the single biggest reason small-account options trading fails
to profit. Every honest evaluation must net it out.

Defaults are IBKR-ish (~$0.65/contract, $1 order minimum) plus a slippage term
that models paying ~half the bid-ask on each fill. Tune in config `costs:`.
Verify real broker pricing — it changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

LEGS_BY_STRUCTURE = {
    "put_credit_spread": 2, "call_credit_spread": 2,
    "call_debit_spread": 2, "put_debit_spread": 2,
    "iron_condor": 4,
}


@dataclass
class CostModel:
    per_contract: float = 0.65      # options commission per contract
    min_per_order: float = 1.0      # minimum commission per order
    slippage_per_contract: float = 0.01  # $ per contract per fill (modeled half-spread)

    @classmethod
    def from_config(cls, config: Any) -> "CostModel":
        raw = {}
        if config is not None:
            raw = (getattr(config, "raw", None) or {}).get("costs", {}) or {}
            if not raw and isinstance(config, dict):
                raw = config.get("costs", {}) or {}
        f = cls()
        for k in vars(f):
            if k in raw and raw[k] is not None:
                setattr(f, k, float(raw[k]))
        return f

    def order_cost(self, num_legs: int, contracts: int) -> float:
        """Cost of ONE order (open OR close): commission (>= min) + slippage."""
        contracts = max(1, int(contracts))
        num_legs = max(1, int(num_legs))
        commission = max(self.min_per_order, self.per_contract * num_legs * contracts)
        slippage = self.slippage_per_contract * num_legs * contracts
        return round(commission + slippage, 2)

    def legs_for(self, structure: str) -> int:
        return LEGS_BY_STRUCTURE.get(structure, 2)

    def position_cost(self, structure: str, contracts: int, exit_reason: str) -> float:
        """Round-trip cost for a position. A worthless EXPIRY incurs only the
        opening cost (nothing is traded to close); a managed close pays both."""
        legs = self.legs_for(structure)
        opened = self.order_cost(legs, contracts)
        closed = 0.0 if exit_reason == "expired" else self.order_cost(legs, contracts)
        return round(opened + closed, 2)
