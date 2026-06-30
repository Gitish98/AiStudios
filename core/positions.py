"""
Position lifecycle + exit logic for defined-risk credit spreads — pure and
unit-tested. The deterministic core decides when to exit; no LLM here.

Management rules (defaults; see config strategies.premium_harvest.manage):
  - TAKE PROFIT at `profit_target_pct` of the credit captured. A put credit
    spread is "worth" its cost-to-close; when that cost falls to (1 - target) of
    the entry credit, we've captured `target` of the max profit. Default 50%.
  - STOP LOSS at `stop_loss_mult` x the credit received. When the cost-to-close
    rises to (1 + mult) x entry credit, the open loss equals `mult` x credit.
    Default 2x.
  - TIME EXIT at `manage_at_dte` days to expiry (default 21) — close whatever's
    left rather than carry gamma/pin risk into expiration week.
  - EXPIRY settlement at dte <= 0: value = the spread's intrinsic, clamped to
    [0, width].

P&L is in dollars: per-share figures * 100 * contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from .brokers.base import OrderRequest


@dataclass
class ManageParams:
    profit_target_pct: float = 0.50
    stop_loss_mult: float = 2.0
    manage_at_dte: int = 21

    @classmethod
    def from_config(cls, params: dict[str, Any]) -> "ManageParams":
        m = (params or {}).get("manage", {}) or {}
        f = cls()
        for k in vars(f):
            if k in m and m[k] is not None:
                setattr(f, k, type(getattr(f, k))(m[k]))
        return f


def order_to_position(order: OrderRequest, asof: str, opened_ts: str) -> dict:
    """Derive a persisted position row from an accepted opening order
    (put credit spread: a short put + a long put below it)."""
    short = next((l for l in order.legs if l.side == "sell" and l.option_type == "put"), None)
    long = next((l for l in order.legs if l.side == "buy" and l.option_type == "put"), None)
    if short is None or long is None:
        raise ValueError("order_to_position expects a put credit spread (short+long put)")
    width = round(abs(short.strike - long.strike), 2)
    contracts = max(1, int(round(abs(short.qty))))
    return {
        "client_order_id": order.client_order_id,
        "strategy": order.strategy,
        "underlying": order.underlying,
        "status": "open",
        "opened_asof": asof,
        "opened_ts": opened_ts,
        "expiration": short.expiration,
        "contracts": contracts,
        "short_strike": short.strike,
        "long_strike": long.strike,
        "width": width,
        "entry_credit_ps": round(order.est_credit / 100.0, 4),  # est_credit is $ (ps*100)
        "max_loss": order.max_loss,
    }


def dte_from(expiration: str, asof: str) -> int:
    return (date.fromisoformat(expiration) - date.fromisoformat(asof)).days


def intrinsic_value_ps(short_strike: float, long_strike: float, spot: float) -> float:
    """Put-credit-spread value at/after expiry, per share, clamped to [0, width]."""
    width = abs(short_strike - long_strike)
    return max(0.0, min(width, short_strike - spot))


def unrealized_pnl(entry_credit_ps: float, current_value_ps: float,
                   contracts: int) -> float:
    """Dollars. Positive = profit (spread is cheaper to close than the credit)."""
    return round((entry_credit_ps - current_value_ps) * 100.0 * contracts, 2)


@dataclass
class ExitDecision:
    action: str            # "hold" | "close" | "expire"
    reason: str            # profit_target | stop_loss | dte_manage | expired | hold
    exit_value_ps: float
    realized_pnl: float    # dollars (only meaningful when action != hold)


def evaluate_exit(pos: dict, current_value_ps: float, spot: float, asof: str,
                  params: ManageParams) -> ExitDecision:
    entry = float(pos["entry_credit_ps"])
    contracts = int(pos["contracts"])
    dte = dte_from(pos["expiration"], asof)

    # Expiry settlement.
    if dte <= 0:
        iv = intrinsic_value_ps(pos["short_strike"], pos["long_strike"], spot)
        return ExitDecision("expire", "expired", iv,
                            unrealized_pnl(entry, iv, contracts))

    take_at = entry * (1.0 - params.profit_target_pct)   # value falls to this -> profit
    stop_at = entry * (1.0 + params.stop_loss_mult)       # value rises to this -> stop

    if current_value_ps <= take_at:
        return ExitDecision("close", "profit_target", current_value_ps,
                            unrealized_pnl(entry, current_value_ps, contracts))
    if current_value_ps >= stop_at:
        return ExitDecision("close", "stop_loss", current_value_ps,
                            unrealized_pnl(entry, current_value_ps, contracts))
    if dte <= params.manage_at_dte:
        return ExitDecision("close", "dte_manage", current_value_ps,
                            unrealized_pnl(entry, current_value_ps, contracts))
    return ExitDecision("hold", "hold", current_value_ps,
                        unrealized_pnl(entry, current_value_ps, contracts))
