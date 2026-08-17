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

def structure_of(p: dict) -> str:
    return p.get("structure") or "put_credit_spread"


LEGS_BY_STRUCTURE = {
    "put_credit_spread": 2, "call_credit_spread": 2,
    "call_debit_spread": 2, "put_debit_spread": 2,
    "iron_condor": 4,
}


@dataclass
class CostModel:
    per_contract: float = 0.65      # options commission per contract
    min_per_order: float = 1.0      # minimum commission per order
    # MODELED half-spread, $ per contract per LEG per fill — used ONLY when a
    # position has no measured slippage. The old default of $0.01 was ~70x below
    # what this system actually measured on its first two fills ($1.39 and $1.60
    # per contract on the ENTRY alone), which mattered because this number sits
    # directly under the graduation gate's "positive net expectancy" test: a
    # 70x-understated cost makes a losing book read as eligible to go live —
    # precisely the failure the cost model exists to prevent. 0.75/leg/contract
    # is one tick of a penny-wide-ish option market and roughly halves the pair
    # of measurements we have; it is still a GUESS, and every real fill replaces
    # it with measurement.
    slippage_per_contract: float = 0.75

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

    def order_cost(self, num_legs: int, contracts: int,
                   measured_slip_ps: Any = None) -> float:
        """Cost of ONE order (open OR close): commission (>= min) + slippage.

        `measured_slip_ps` is the REALIZED adverse slippage per share for this
        fill (core.fills). When present it REPLACES the model: slippage stops
        being an assumption and becomes evidence. Per share x 100 x contracts —
        it is a whole-spread figure already (the net of both legs), so it is NOT
        multiplied by leg count the way the modeled term is."""
        contracts = max(1, int(contracts))
        num_legs = max(1, int(num_legs))
        commission = max(self.min_per_order, self.per_contract * num_legs * contracts)
        if measured_slip_ps is None:
            slippage = self.slippage_per_contract * num_legs * contracts
        else:
            # Measured slippage is a WHOLE-SPREAD per-share figure (the net across
            # legs), so it is NOT multiplied by leg count the way the modeled
            # per-leg term is. Callers must reject implausible values BEFORE
            # reaching here (see fills.plausible_slip_ps) — this function trusts
            # what it is given and does not clamp, because a clamp turns garbage
            # into a confident-looking number.
            slippage = float(measured_slip_ps) * 100.0 * contracts
        return round(commission + slippage, 2)

    def legs_for(self, structure: str) -> int:
        return LEGS_BY_STRUCTURE.get(structure, 2)

    def position_cost(self, structure: str, contracts: int, exit_reason: str,
                      entry_slip_ps: Any = None, exit_slip_ps: Any = None,
                      width: Any = None) -> float:
        """Round-trip cost for a position. A worthless EXPIRY incurs only the
        opening cost (nothing is traded to close); a managed close pays both.

        Each side independently uses its MEASURED slippage when the broker gave
        us one, and the model otherwise — so a position with a measured entry and
        an unreported exit is half evidence, half estimate, and says so via
        `measured_sides`."""
        from .fills import plausible_slip_ps
        legs = self.legs_for(structure)
        e = entry_slip_ps if plausible_slip_ps(entry_slip_ps, width) else None
        x = exit_slip_ps if plausible_slip_ps(exit_slip_ps, width) else None
        opened = self.order_cost(legs, contracts, e)
        closed = (0.0 if exit_reason == "expired"
                  else self.order_cost(legs, contracts, x))
        return round(opened + closed, 2)

    def measured_dollars(self, p: dict, exit_reason: str = "") -> tuple[float, float]:
        """(cost_dollars_from_measurement, total_cost_dollars) for one position.

        Coverage is reported in DOLLARS, not side counts: a measured entry beside
        a modeled exit is not "50% evidence" if the two sides cost different
        amounts, and the number is meant to tell the reader how much of the
        expectancy rests on evidence. Implausible slips count as MODELED — they
        were rejected, so they are not evidence."""
        from .fills import plausible_slip_ps
        legs = self.legs_for(structure_of(p))
        contracts = int(p.get("contracts") or 1)
        width = p.get("width")
        total = measured = 0.0
        e_ok = plausible_slip_ps(p.get("entry_slip_ps"), width)
        c = self.order_cost(legs, contracts, p.get("entry_slip_ps") if e_ok else None)
        total += c
        if e_ok:
            measured += c
        if exit_reason != "expired":
            x_ok = plausible_slip_ps(p.get("exit_slip_ps"), width)
            c = self.order_cost(legs, contracts, p.get("exit_slip_ps") if x_ok else None)
            total += c
            if x_ok:
                measured += c
        return round(measured, 2), round(total, 2)
