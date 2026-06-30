"""
Management pass — value every open position, apply the exit rules, and place
closing orders (paper). Runs at the START of each cycle, before new entries, so
exits free up risk budget and realize P&L first.

Closing/reducing-risk orders are intentionally NOT blocked by the opening risk
gate or the kill switch — you must always be able to exit. They are journaled.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from .brokers.base import BrokerAdapter, OrderLeg, OrderRequest
from .positions import ManageParams, dte_from, evaluate_exit
from .store import Store


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def mark_spread_value_ps(adapter: BrokerAdapter, pos: dict) -> Optional[float]:
    """Current per-share cost to close the spread (short put mid − long put mid).
    Uses the adapter's mark_option when available (sim), else the live chain."""
    u, exp = pos["underlying"], pos["expiration"]
    if hasattr(adapter, "mark_option"):
        s = adapter.mark_option(u, exp, pos["short_strike"], "put")
        l = adapter.mark_option(u, exp, pos["long_strike"], "put")
        return round(s - l, 4)
    chain = adapter.get_option_chain(u, exp)
    short = next((c for c in chain if c.option_type == "put"
                  and abs(c.strike - pos["short_strike"]) < 1e-6), None)
    long = next((c for c in chain if c.option_type == "put"
                 and abs(c.strike - pos["long_strike"]) < 1e-6), None)
    if not short or not long:
        return None
    return round(short.mid - long.mid, 4)


def _spot(adapter: BrokerAdapter, underlying: str) -> float:
    try:
        return adapter.get_quote(underlying).mid
    except Exception:
        return 0.0


def _closing_order(pos: dict, value_ps: float) -> OrderRequest:
    """Buy-to-close the credit spread (reverse the legs)."""
    contracts = int(pos["contracts"])
    legs = [
        OrderLeg(symbol=f"{pos['underlying']}_close_short", side="buy", qty=contracts,
                 asset_class="option", option_type="put",
                 strike=pos["short_strike"], expiration=pos["expiration"]),
        OrderLeg(symbol=f"{pos['underlying']}_close_long", side="sell", qty=contracts,
                 asset_class="option", option_type="put",
                 strike=pos["long_strike"], expiration=pos["expiration"]),
    ]
    return OrderRequest(
        client_order_id=f"CLOSE-{pos['client_order_id']}",
        legs=legs, order_type="limit", limit_price=round(max(0.0, value_ps), 2),
        strategy="close_put_credit_spread", est_credit=0.0,
        max_loss=0.0, underlying=pos["underlying"],
    )


def manage_open_positions(adapter: BrokerAdapter, store: Store, config,
                          asof: str) -> dict[str, Any]:
    params = ManageParams.from_config(
        (config.strategies or {}).get("premium_harvest", {}))
    open_positions = store.get_open_positions()

    summary: dict[str, Any] = {
        "evaluated": len(open_positions), "closed": [], "held": [],
        "realized_today": 0.0, "unrealized_open": 0.0,
    }

    for pos in open_positions:
        value_ps = mark_spread_value_ps(adapter, pos)
        if value_ps is None:
            summary["held"].append({"underlying": pos["underlying"], "reason": "no_mark"})
            continue
        spot = _spot(adapter, pos["underlying"])
        decision = evaluate_exit(pos, value_ps, spot, asof, params)

        if decision.action == "hold":
            summary["unrealized_open"] += decision.realized_pnl  # here = unrealized mark
            summary["held"].append({
                "underlying": pos["underlying"],
                "dte": dte_from(pos["expiration"], asof),
                "value_ps": value_ps, "unrealized": decision.realized_pnl})
            continue

        # Close (or settle at expiry). Expiry needs no order; a managed close does.
        if decision.action == "close":
            order = _closing_order(pos, decision.exit_value_ps)
            result = adapter.place_order(order)
            store.record_order(_now_iso(), order, result)

        store.close_position(pos["id"], asof, _now_iso(), decision.reason,
                             decision.exit_value_ps, decision.realized_pnl)
        store.append(_now_iso(), "position_closed", {
            "underlying": pos["underlying"], "reason": decision.reason,
            "exit_value_ps": decision.exit_value_ps, "realized_pnl": decision.realized_pnl})
        summary["realized_today"] += decision.realized_pnl
        summary["closed"].append({
            "underlying": pos["underlying"], "reason": decision.reason,
            "realized_pnl": decision.realized_pnl})

    summary["realized_today"] = round(summary["realized_today"], 2)
    summary["unrealized_open"] = round(summary["unrealized_open"], 2)
    return summary
