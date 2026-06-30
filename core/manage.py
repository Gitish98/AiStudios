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
    """Current per-share market value of the vertical, in [0, width]:
       put family:  P(higher) − P(lower);  call family: C(lower) − C(higher).
    Uses the adapter's mark_option when available (sim), else the live chain."""
    u, exp = pos["underlying"], pos["expiration"]
    family = pos.get("family") or ("call" if "call" in (pos.get("structure") or "") else "put")
    higher = max(pos["short_strike"], pos["long_strike"])
    lower = min(pos["short_strike"], pos["long_strike"])
    right = "call" if family == "call" else "put"

    def _mark(strike: float) -> Optional[float]:
        if hasattr(adapter, "mark_option"):
            return adapter.mark_option(u, exp, strike, right)
        chain = adapter.get_option_chain(u, exp)
        c = next((c for c in chain if c.option_type == right
                  and abs(c.strike - strike) < 1e-6), None)
        return c.mid if c else None

    hi, lo = _mark(higher), _mark(lower)
    if hi is None or lo is None:
        return None
    value = (hi - lo) if family == "put" else (lo - hi)
    width = abs(pos["short_strike"] - pos["long_strike"])
    return round(max(0.0, min(width, value)), 4)  # a vertical is worth [0, width]


def _spot(adapter: BrokerAdapter, underlying: str) -> Optional[float]:
    """Current spot, or None if unavailable. NEVER returns a fake 0.0 — a quote
    failure must be distinguishable from a real price (a 0 spot would settle an
    expiring spread at full max-loss/profit, which would be catastrophically wrong)."""
    try:
        m = adapter.get_quote(underlying).mid
        return m if m and m > 0 else None
    except Exception:
        return None


def _closing_order(pos: dict, value_ps: float) -> OrderRequest:
    """Close the vertical by reversing both legs (sell the long, buy back the short)."""
    contracts = int(pos["contracts"])
    family = pos.get("family") or ("call" if "call" in (pos.get("structure") or "") else "put")
    legs = [
        OrderLeg(symbol=f"{pos['underlying']}_close_short", side="buy", qty=contracts,
                 asset_class="option", option_type=family,
                 strike=pos["short_strike"], expiration=pos["expiration"]),
        OrderLeg(symbol=f"{pos['underlying']}_close_long", side="sell", qty=contracts,
                 asset_class="option", option_type=family,
                 strike=pos["long_strike"], expiration=pos["expiration"]),
    ]
    return OrderRequest(
        client_order_id=f"CLOSE-{pos['client_order_id']}",
        legs=legs, order_type="limit", limit_price=round(max(0.0, value_ps), 2),
        strategy=f"close_{pos.get('structure', 'vertical')}", est_credit=0.0,
        max_loss=0.0, underlying=pos["underlying"],
    )


def manage_open_positions(adapter: BrokerAdapter, store: Store, config,
                          asof: str) -> dict[str, Any]:
    strategies = config.strategies or {}
    credit_params = ManageParams.from_config(strategies.get("premium_harvest", {}))
    debit_params = ManageParams.from_config(strategies.get("volatility_breakout", {}))
    open_positions = store.get_open_positions()

    summary: dict[str, Any] = {
        "evaluated": len(open_positions), "closed": [], "held": [],
        "realized_today": 0.0, "unrealized_open": 0.0,
    }

    for pos in open_positions:
      try:
        value_ps = mark_spread_value_ps(adapter, pos)
        if value_ps is None:
            summary["held"].append({"underlying": pos["underlying"], "reason": "no_mark"})
            continue
        spot = _spot(adapter, pos["underlying"])
        # At expiry we MUST have a real spot to settle intrinsic; missing spot ->
        # hold (don't persist a fabricated settlement). Non-expiry exits use the
        # mark, not spot, so spot being absent there is harmless.
        if dte_from(pos["expiration"], asof) <= 0 and spot is None:
            summary["held"].append({"underlying": pos["underlying"], "reason": "no_spot_at_expiry"})
            continue
        params = credit_params if bool(pos.get("is_credit", 1)) else debit_params
        decision = evaluate_exit(pos, value_ps, spot if spot is not None else 0.0,
                                 asof, params)

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
      except Exception as e:
        # One bad position must never prevent valuing/exiting the rest.
        store.append(_now_iso(), "manage_error", {
            "position_id": pos.get("id"), "underlying": pos.get("underlying"),
            "error": str(e)})
        summary["held"].append({"underlying": pos.get("underlying"),
                                "reason": "mark_error", "error": str(e)})

    summary["realized_today"] = round(summary["realized_today"], 2)
    summary["unrealized_open"] = round(summary["unrealized_open"], 2)
    return summary
