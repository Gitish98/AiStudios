"""
Management pass — value every open position, apply the exit rules, and place
closing orders (paper). Runs at the START of each cycle, before new entries, so
exits free up risk budget and realize P&L first.

Closing/reducing-risk orders are intentionally NOT blocked by the opening risk
gate or the kill switch — you must always be able to exit. They are journaled.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from .brokers.base import BrokerAdapter, OrderLeg, OrderRequest
from .positions import ManageParams, dte_from, evaluate_exit
from .store import DEAD_ORDER_STATUSES, Store


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _marker(adapter: BrokerAdapter, underlying: str, expiration: str):
    """Return a mark(strike, right) -> per-share mid (or None)."""
    def _mark(strike: float, right: str) -> Optional[float]:
        if hasattr(adapter, "mark_option"):
            return adapter.mark_option(underlying, expiration, strike, right)
        chain = adapter.get_option_chain(underlying, expiration)
        c = next((c for c in chain if c.option_type == right
                  and abs(c.strike - strike) < 1e-6), None)
        return c.mid if c else None
    return _mark


def mark_spread_value_ps(adapter: BrokerAdapter, pos: dict) -> Optional[float]:
    """Current per-share market value, clamped to [0, width].
       vertical put:  P(higher) − P(lower);  vertical call: C(lower) − C(higher);
       iron condor:   put-side spread value + call-side spread value."""
    u, exp = pos["underlying"], pos["expiration"]
    mark = _marker(adapter, u, exp)

    if pos.get("structure") == "iron_condor":
        legs = json.loads(pos["legs_json"])
        put_wing = legs["sp"] - legs["lp"]
        call_wing = legs["lc"] - legs["sc"]
        sp, lp = mark(legs["sp"], "put"), mark(legs["lp"], "put")
        sc, lc = mark(legs["sc"], "call"), mark(legs["lc"], "call")
        if None in (sp, lp, sc, lc):
            return None
        # Each side bounded by its OWN wing (correct for asymmetric condors).
        put_side = max(0.0, min(put_wing, sp - lp))
        call_side = max(0.0, min(call_wing, sc - lc))
        return round(put_side + call_side, 4)

    family = pos.get("family") or ("call" if "call" in (pos.get("structure") or "") else "put")
    higher = max(pos["short_strike"], pos["long_strike"])
    lower = min(pos["short_strike"], pos["long_strike"])
    right = "call" if family == "call" else "put"
    hi, lo = mark(higher, right), mark(lower, right)
    if hi is None or lo is None:
        return None
    value = (hi - lo) if family == "put" else (lo - hi)
    width = abs(pos["short_strike"] - pos["long_strike"])
    return round(max(0.0, min(width, value)), 4)


def _spot(adapter: BrokerAdapter, underlying: str) -> Optional[float]:
    """Current spot, or None if unavailable. NEVER returns a fake 0.0 — a quote
    failure must be distinguishable from a real price (a 0 spot would settle an
    expiring spread at full max-loss/profit, which would be catastrophically wrong)."""
    try:
        m = adapter.get_quote(underlying).mid
        return m if m and m > 0 else None
    except Exception:
        return None


def _occ(underlying: str, expiration: str, right: str, strike: float) -> str:
    """Build a real OCC option symbol, e.g. SPY260219P00540000. Real brokers
    (Alpaca) submit the leg `symbol` verbatim, so it must be valid."""
    yymmdd = expiration[2:4] + expiration[5:7] + expiration[8:10]
    cp = "C" if right == "call" else "P"
    return f"{underlying}{yymmdd}{cp}{int(round(strike * 1000)):08d}"


def _closing_order(pos: dict, value_ps: float) -> OrderRequest:
    """Close the spread by reversing every leg (sell longs, buy back shorts)."""
    contracts = int(pos["contracts"])
    exp, u = pos["expiration"], pos["underlying"]

    def leg(side, otype, strike):
        return OrderLeg(symbol=_occ(u, exp, otype, strike), side=side, qty=contracts,
                        asset_class="option", option_type=otype, strike=strike,
                        expiration=exp, intent="close")

    if pos.get("structure") == "iron_condor":
        j = json.loads(pos["legs_json"])
        legs = [leg("buy", "put", j["sp"]), leg("sell", "put", j["lp"]),
                leg("buy", "call", j["sc"]), leg("sell", "call", j["lc"])]
    else:
        family = pos.get("family") or ("call" if "call" in (pos.get("structure") or "") else "put")
        legs = [leg("buy", family, pos["short_strike"]),
                leg("sell", family, pos["long_strike"])]

    # Sign convention: closing a DEBIT spread (sell the more valuable long leg)
    # nets a CREDIT of value_ps -> est_credit must be POSITIVE so the adapters
    # submit a negative (credit) combo price. Closing a CREDIT spread or condor
    # is genuinely a debit -> est_credit 0 -> positive (debit) price. (Magnitude
    # is irrelevant to the adapters; only the sign drives the price sign.)
    is_debit_close = not bool(pos.get("is_credit", 1))
    est_credit = round(max(0.0, value_ps) * 100, 2) if is_debit_close else 0.0
    return OrderRequest(
        client_order_id=f"CLOSE-{pos['client_order_id']}",
        legs=legs, order_type="limit", limit_price=round(max(0.0, value_ps), 2),
        strategy=f"close_{pos.get('structure', 'vertical')}", est_credit=est_credit,
        max_loss=0.0, underlying=pos["underlying"],
    )


def _is_filled(result, contracts: int) -> bool:
    return (result.status or "").lower() == "filled" and float(result.filled_qty or 0) > 0


def _load_pending(store: Store) -> dict:
    raw = store.get_kv("pending_closes")
    return json.loads(raw) if raw else {}


def _save_pending(store: Store, d: dict) -> None:
    store.set_kv("pending_closes", json.dumps(d))


def _finalize_pending_closes(adapter: BrokerAdapter, store: Store, asof: str) -> dict:
    """A close accepted-but-not-yet-filled in a prior cycle is finalized here once
    the broker reports it filled — so we never mark a position closed (dropping its
    risk from the book) before the close actually executes."""
    pend = _load_pending(store)
    if not pend:
        return pend
    try:
        bro = {o.client_order_id: o for o in adapter.list_orders()}
    except Exception:
        return pend  # can't confirm -> leave pending, the position stays tracked
    changed = False
    for pid, info in list(pend.items()):
        o = bro.get(info["coid"])
        if o is None:
            continue
        st = (o.status or "").lower()
        if st == "filled" and float(o.filled_qty or 0) > 0:
            store.close_position(int(pid), asof, _now_iso(), info["reason"],
                                 info["exit_value_ps"], info["realized_pnl"])
            store.append(_now_iso(), "position_closed", {
                "position_id": int(pid), "reason": info["reason"],
                "realized_pnl": info["realized_pnl"], "via": "pending_fill"})
            del pend[pid]; changed = True
        elif st in DEAD_ORDER_STATUSES:
            store.set_order_status(info["coid"], "canceled")  # canonicalize the close order
            del pend[pid]; changed = True  # close died -> leave open, re-evaluate
    if changed:
        _save_pending(store, pend)
    return pend


def manage_open_positions(adapter: BrokerAdapter, store: Store, config,
                          asof: str) -> dict[str, Any]:
    strategies = config.strategies or {}
    credit_params = ManageParams.from_config(strategies.get("premium_harvest", {}))
    debit_params = ManageParams.from_config(strategies.get("volatility_breakout", {}))
    condor_params = ManageParams.from_config(strategies.get("earnings_vol", {}))

    # Finalize closes that were accepted-but-unfilled in a prior cycle, then load
    # the still-open book.
    pending = _finalize_pending_closes(adapter, store, asof)
    open_positions = store.get_open_positions()

    def _params_for(pos):
        if pos.get("structure") == "iron_condor":
            return condor_params
        return credit_params if bool(pos.get("is_credit", 1)) else debit_params

    summary: dict[str, Any] = {
        "evaluated": len(open_positions), "closed": [], "held": [],
        "realized_today": 0.0, "unrealized_open": 0.0,
    }

    for pos in open_positions:
      try:
        if str(pos.get("id")) in pending:   # awaiting a fill on a prior close
            summary["held"].append({"underlying": pos["underlying"], "reason": "close_pending"})
            continue
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
        params = _params_for(pos)
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
            if not result.accepted:
                # Broker rejected the close — do NOT mark it closed locally, or our
                # book desyncs from the live account. Leave open; retry next cycle.
                store.append(_now_iso(), "close_failed", {
                    "underlying": pos["underlying"], "position_id": pos.get("id"),
                    "reason": result.reason})
                summary["held"].append({"underlying": pos["underlying"],
                                        "reason": "close_rejected", "detail": result.reason})
                continue
            if not _is_filled(result, pos["contracts"]):
                # Accepted but not yet filled (real brokers ack before fill). Keep
                # the position OPEN and tracked; finalize when it fills next cycle.
                pending[str(pos["id"])] = {
                    "coid": order.client_order_id, "reason": decision.reason,
                    "exit_value_ps": decision.exit_value_ps,
                    "realized_pnl": decision.realized_pnl}
                _save_pending(store, pending)
                store.append(_now_iso(), "close_pending", {
                    "underlying": pos["underlying"], "position_id": pos["id"],
                    "coid": order.client_order_id})
                summary["held"].append({"underlying": pos["underlying"], "reason": "close_pending"})
                continue

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
