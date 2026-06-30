"""
Position lifecycle + exit logic for DEFINED-RISK VERTICAL spreads — pure and
unit-tested. Supports four structures behind one model:

  put_credit_spread   (bullish, credit)   premium harvest
  call_credit_spread  (bearish, credit)
  call_debit_spread   (bullish, debit)     volatility breakout (own premium)
  put_debit_spread    (bearish, debit)

A vertical's market "value" is always a positive number in [0, width]:
  put family:  value = P(higher_strike) − P(lower_strike)
  call family: value = C(lower_strike)  − C(higher_strike)

For a CREDIT spread we received `entry` and the value is the cost to close, so
P&L = entry − value. For a DEBIT spread we paid `entry` and the value is what we
can sell for, so P&L = value − entry. (entry and value are per-share; × 100 ×
contracts for dollars.)

Exit rules (config strategies.<name>.manage):
  CREDIT: take profit at value ≤ entry·(1−profit_target_pct);
          stop at value ≥ entry·(1+stop_loss_mult).
  DEBIT:  take profit at value ≥ entry·(1+debit_profit_gain);
          stop at value ≤ entry·(1−debit_stop_frac).
  Both:   time exit at manage_at_dte; expiry settlement at dte ≤ 0.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

from .brokers.base import OrderRequest

CREDIT_STRUCTURES = {"put_credit_spread", "call_credit_spread", "iron_condor"}
DEBIT_STRUCTURES = {"call_debit_spread", "put_debit_spread"}
VERTICAL_STRUCTURES = {"put_credit_spread", "call_credit_spread",
                       "call_debit_spread", "put_debit_spread"}


@dataclass
class ManageParams:
    profit_target_pct: float = 0.50   # credit: capture this fraction of the credit
    stop_loss_mult: float = 2.0       # credit: stop when loss = this × credit
    debit_profit_gain: float = 1.0    # debit: take profit at +100% of the debit
    debit_stop_frac: float = 0.50     # debit: stop after losing this fraction of the debit
    manage_at_dte: int = 21

    @classmethod
    def from_config(cls, params: dict[str, Any]) -> "ManageParams":
        m = (params or {}).get("manage", {}) or {}
        f = cls()
        for k in vars(f):
            if k in m and m[k] is not None:
                setattr(f, k, type(getattr(f, k))(m[k]))
        return f


def family_of(structure: str) -> str:
    return "call" if "call" in structure else "put"


def is_credit_structure(structure: str) -> bool:
    return structure in CREDIT_STRUCTURES


def order_to_position(order: OrderRequest, asof: str, opened_ts: str) -> dict:
    """Derive a persisted position row from an accepted spread order. Verticals
    (4 structures) and the 4-leg iron condor are supported."""
    if order.strategy == "iron_condor":
        return _condor_to_position(order, asof, opened_ts)
    return _vertical_to_position(order, asof, opened_ts)


def _condor_to_position(order: OrderRequest, asof: str, opened_ts: str) -> dict:
    puts = [l for l in order.legs if l.option_type == "put"]
    calls = [l for l in order.legs if l.option_type == "call"]
    sp = next((l for l in puts if l.side == "sell"), None)
    lp = next((l for l in puts if l.side == "buy"), None)
    sc = next((l for l in calls if l.side == "sell"), None)
    lc = next((l for l in calls if l.side == "buy"), None)
    if not (sp and lp and sc and lc):
        raise ValueError("iron_condor expects short+long put AND short+long call")
    width = round(max(sp.strike - lp.strike, lc.strike - sc.strike), 2)
    legs = {"sp": sp.strike, "lp": lp.strike, "sc": sc.strike, "lc": lc.strike, "width": width}
    return {
        "client_order_id": order.client_order_id, "strategy": order.strategy,
        "structure": "iron_condor", "family": "mixed", "is_credit": 1,
        "underlying": order.underlying, "status": "open", "opened_asof": asof,
        "opened_ts": opened_ts, "expiration": sp.expiration,
        "contracts": max(1, int(round(abs(sp.qty)))),
        "short_strike": sp.strike, "long_strike": lp.strike, "width": width,
        "legs_json": json.dumps(legs),
        "entry_credit_ps": round(abs(order.est_credit) / 100.0, 4),
        "max_loss": order.max_loss,
    }


def _vertical_to_position(order: OrderRequest, asof: str, opened_ts: str) -> dict:
    """The SELL leg is the short, the BUY leg the long; family from the option
    type; credit/debit from est_credit's sign (>0 = credit, ≤0 = debit)."""
    short = next((l for l in order.legs if l.side == "sell" and l.asset_class == "option"), None)
    long = next((l for l in order.legs if l.side == "buy" and l.asset_class == "option"), None)
    if short is None or long is None or short.option_type != long.option_type:
        raise ValueError("order_to_position expects a 2-leg vertical (one short, one long, same type)")
    structure = order.strategy
    if structure not in (CREDIT_STRUCTURES | DEBIT_STRUCTURES):
        # Infer from sign + family if a generic strategy name was used.
        fam = short.option_type
        cred = order.est_credit > 0
        structure = f"{fam}_{'credit' if cred else 'debit'}_spread"
    return {
        "client_order_id": order.client_order_id,
        "strategy": order.strategy,
        "structure": structure,
        "family": short.option_type,
        "is_credit": 1 if is_credit_structure(structure) else 0,
        "underlying": order.underlying,
        "status": "open",
        "opened_asof": asof,
        "opened_ts": opened_ts,
        "expiration": short.expiration,
        "contracts": max(1, int(round(abs(short.qty)))),
        "short_strike": short.strike,
        "long_strike": long.strike,
        "width": round(abs(short.strike - long.strike), 2),
        "entry_credit_ps": round(abs(order.est_credit) / 100.0, 4),  # net premium magnitude / share
        "max_loss": order.max_loss,
    }


def dte_from(expiration: str, asof: str) -> int:
    return (date.fromisoformat(expiration) - date.fromisoformat(asof)).days


def intrinsic_vertical_ps(family: str, short_strike: float, long_strike: float,
                          spot: float) -> float:
    """Vertical value at/after expiry, per share, clamped to [0, width]."""
    width = abs(short_strike - long_strike)
    higher, lower = max(short_strike, long_strike), min(short_strike, long_strike)
    if family == "put":
        return max(0.0, min(width, higher - spot))
    return max(0.0, min(width, spot - lower))


def condor_intrinsic_ps(legs: dict, spot: float) -> float:
    """Iron-condor value at/after expiry: each side's intrinsic clamped to ITS
    OWN wing width (the put spread can't exceed sp−lp; the call spread can't
    exceed lc−sc). The two sides are mutually exclusive at a single settlement
    price, so they simply sum. (A shared max-wing clamp would over-value the
    narrower side of an ASYMMETRIC condor and fabricate a loss.)"""
    put_wing = legs["sp"] - legs["lp"]
    call_wing = legs["lc"] - legs["sc"]
    put_side = max(0.0, min(put_wing, legs["sp"] - spot))   # short put breached below
    call_side = max(0.0, min(call_wing, spot - legs["sc"]))  # short call breached above
    return put_side + call_side


def position_pnl(is_credit: bool, entry_ps: float, value_ps: float,
                 contracts: int) -> float:
    """Dollars. Credit: profit as value falls. Debit: profit as value rises."""
    per_share = (entry_ps - value_ps) if is_credit else (value_ps - entry_ps)
    return round(per_share * 100.0 * contracts, 2)


@dataclass
class ExitDecision:
    action: str            # "hold" | "close" | "expire"
    reason: str            # profit_target | stop_loss | dte_manage | expired | hold
    exit_value_ps: float
    realized_pnl: float


def evaluate_exit(pos: dict, current_value_ps: float, spot: float, asof: str,
                  params: ManageParams) -> ExitDecision:
    entry = float(pos["entry_credit_ps"])          # net premium magnitude / share
    contracts = int(pos["contracts"])
    is_credit = bool(pos.get("is_credit", 1))
    family = pos.get("family") or family_of(pos.get("structure", "put_credit_spread"))
    dte = dte_from(pos["expiration"], asof)

    structure = pos.get("structure", "")
    if dte <= 0:
        if structure == "iron_condor":
            iv = condor_intrinsic_ps(json.loads(pos["legs_json"]), spot)
        else:
            iv = intrinsic_vertical_ps(family, pos["short_strike"], pos["long_strike"], spot)
        return ExitDecision("expire", "expired", iv,
                            position_pnl(is_credit, entry, iv, contracts))

    pnl = position_pnl(is_credit, entry, current_value_ps, contracts)

    if is_credit:
        if current_value_ps <= entry * (1.0 - params.profit_target_pct):
            return ExitDecision("close", "profit_target", current_value_ps, pnl)
        if current_value_ps >= entry * (1.0 + params.stop_loss_mult):
            return ExitDecision("close", "stop_loss", current_value_ps, pnl)
    else:
        if current_value_ps >= entry * (1.0 + params.debit_profit_gain):
            return ExitDecision("close", "profit_target", current_value_ps, pnl)
        if current_value_ps <= entry * (1.0 - params.debit_stop_frac):
            return ExitDecision("close", "stop_loss", current_value_ps, pnl)

    if dte <= params.manage_at_dte:
        return ExitDecision("close", "dte_manage", current_value_ps, pnl)
    return ExitDecision("hold", "hold", current_value_ps, pnl)
