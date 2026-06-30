"""
Execution orchestrator — the one paper cycle.

Flow (deterministic, journaled at every step):
  fetch account+positions -> for each watchlist symbol: build StrategyContext,
  run strategies -> Signals -> build OrderRequest -> RISK GATE -> (if approved)
  place via broker adapter -> record order + journal.

Claude is NOT in this loop in Phase 0. When the reasoning layer is added it can
only RANK/annotate signals before the gate; it can never place an order or
bypass the gate.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Optional

from .brokers.base import BrokerAdapter, OrderRequest, Position
from .manage import manage_open_positions
from .positions import order_to_position
from .reconcile import reconcile
from .risk import Decision, RiskContext, RiskGate, RiskLimits, _notional
from .store import Store
from strategies.base import Signal, StrategyContext


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _client_order_id(signal: Signal, asof: str) -> str:
    """Deterministic idempotency key: same signal on same day never double-places."""
    basis = f"{signal.strategy}|{signal.underlying}|{asof}|" + "|".join(
        f"{l.symbol}:{l.side}:{l.qty}" for l in signal.legs
    )
    return "AIS-" + hashlib.sha256(basis.encode()).hexdigest()[:20]


def signal_to_order(signal: Signal, asof: str) -> OrderRequest:
    return OrderRequest(
        client_order_id=_client_order_id(signal, asof),
        legs=signal.legs,
        order_type="limit",
        limit_price=signal.limit_price,
        time_in_force="day",
        strategy=signal.strategy,
        rationale=signal.rationale,
        max_loss=signal.max_loss,
        est_credit=signal.est_credit,
        underlying=signal.underlying,
        required_approval_level=signal.required_approval_level,
        is_day_trade=signal.is_day_trade,
    )


def run_cycle(
    adapter: BrokerAdapter,
    strategies: list,
    store: Store,
    config,
    asof: Optional[str] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run one full paper cycle. Returns a summary dict for the CLI to print."""
    asof = asof or datetime.now(timezone.utc).date().isoformat()
    ts = _now_iso()
    limits = RiskLimits.from_config(config.risk)
    gate = RiskGate(limits)

    account = adapter.get_account()

    # ── 1. MANAGEMENT PASS — value & exit open positions before any new entry. ──
    manage_summary = manage_open_positions(adapter, store, config, asof)

    # ── 2. Daily P&L from our tracked positions (broker-agnostic, works in sim
    # AND live): realized closes today + unrealized mark on what's still open.
    daily_pnl = manage_summary["realized_today"] + manage_summary["unrealized_open"]

    # Proactively trip the kill switch on a daily-loss breach even if no new
    # order is evaluated this cycle.
    limit_dollars = limits.daily_loss_limit_pct * account.equity
    if account.equity > 0 and daily_pnl <= -abs(limit_dollars) and not store.kill_switch:
        store.set_kill_switch(True)
        store.append(_now_iso(), "kill_switch", {
            "engaged": True, "reason": "daily_loss_limit", "daily_pnl": daily_pnl})

    # ── 3. Gate's view of concurrent exposure = our OPEN tracked positions. ──
    positions: list[Position] = []
    for p in store.get_open_positions():
        positions.append(Position(
            symbol=p["underlying"], qty=p["contracts"], avg_price=0.0,
            market_value=float(p["width"]) * 100.0 * int(p["contracts"]),
            asset_class="option", underlying=p["underlying"],
            max_loss=float(p["max_loss"] or 0.0)))
    opened_today = _count_opened_today(store, asof)

    options_level = int(config.account.get("options_approval_level", 3))
    region = str(config.account.get("region", "US")).upper()

    def _ctx() -> RiskContext:
        return RiskContext(
            account=account, positions=positions, is_paper=adapter.is_paper,
            kill_switch=store.kill_switch, positions_opened_today=opened_today,
            day_trades_trailing_5=account.pdt_day_trade_count,
            daily_pnl=daily_pnl, options_approval_level=options_level,
            region=region,
        )

    def _synthesize_position(order) -> None:
        """Add this order's exposure to the working position list so the next
        signal in the same cycle sees accumulated concentration/heat/leverage."""
        positions.append(Position(
            symbol=order.underlying or (order.legs[0].symbol if order.legs else "?"),
            qty=1, avg_price=0.0, market_value=_notional(order),
            asset_class="option", underlying=order.underlying,
            max_loss=order.max_loss,
        ))

    summary: dict[str, Any] = {
        "asof": asof, "mode": "paper" if adapter.is_paper else "LIVE",
        "broker": adapter.name, "kill_switch": store.kill_switch,
        "signals": [], "placed": [], "rejected": [], "skipped_duplicates": [],
        "manage": manage_summary, "daily_pnl": round(daily_pnl, 2),
    }
    store.append(ts, "cycle_start", {"asof": asof, "broker": adapter.name,
                                     "equity": account.equity})

    if store.kill_switch:
        summary["note"] = "Kill switch engaged — no orders will be placed."

    for symbol in config.watchlist:
        try:
            chain = adapter.get_option_chain(symbol)
            quote = adapter.get_quote(symbol)
            iv_hist = getattr(adapter, "iv_history", lambda s: [])(symbol)
            bars = adapter.get_history(symbol, 120) if hasattr(adapter, "get_history") else []
            sctx = StrategyContext(
                underlying=symbol, spot=quote.mid, option_chain=chain,
                iv_history=iv_hist,
                closes=[b["c"] for b in bars], highs=[b["h"] for b in bars],
                lows=[b["l"] for b in bars], config=config.strategies,
            )
        except Exception as e:  # data failure for one symbol must not kill the cycle
            store.append(_now_iso(), "data_error", {"symbol": symbol, "error": str(e)})
            continue

        for strat in strategies:
            for signal in strat.generate(sctx):
                summary["signals"].append({"symbol": symbol, "strategy": signal.strategy,
                                           "rationale": signal.rationale})
                order = signal_to_order(signal, asof)

                if store.has_order(order.client_order_id):
                    summary["skipped_duplicates"].append(order.client_order_id)
                    continue

                decision = gate.evaluate(order, _ctx())
                store.append(_now_iso(), "risk_decision", {
                    "client_order_id": order.client_order_id,
                    "approved": decision.approved, "reasons": decision.reasons,
                    "strategy": order.strategy, "underlying": order.underlying,
                })

                if decision.trip_kill_switch:
                    store.set_kill_switch(True)
                    summary["kill_switch"] = True

                if not decision.approved:
                    summary["rejected"].append(
                        {"underlying": order.underlying, "strategy": order.strategy,
                         "reasons": decision.reasons})
                    continue

                if dry_run:
                    opened_today += 1
                    _synthesize_position(order)  # so caps accumulate in dry-run too
                    summary["placed"].append(
                        {"underlying": order.underlying, "strategy": order.strategy,
                         "est_credit": order.est_credit, "dry_run": True,
                         "client_order_id": order.client_order_id})
                    continue

                result = adapter.place_order(order)
                store.record_order(_now_iso(), order, result)
                store.append(_now_iso(), "order_placed", {
                    "client_order_id": order.client_order_id,
                    "broker_order_id": result.broker_order_id,
                    "status": result.status, "accepted": result.accepted,
                    "rationale": order.rationale,
                })
                if result.accepted:
                    opened_today += 1
                    _synthesize_position(order)
                    # Persist the open position so future cycles can manage it.
                    try:
                        store.open_position(order_to_position(order, asof, _now_iso()))
                    except ValueError:
                        pass  # non-spread orders aren't tracked as managed positions yet
                    summary["placed"].append({
                        "underlying": order.underlying, "strategy": order.strategy,
                        "status": result.status, "est_credit": order.est_credit,
                        "client_order_id": order.client_order_id})
                else:
                    summary["rejected"].append(
                        {"underlying": order.underlying, "strategy": order.strategy,
                         "reasons": [result.reason]})

    # ── EOD reconcile: our tracked book vs broker truth. ──
    rec = reconcile(adapter, store)
    summary["reconcile"] = rec
    if not rec["ok"]:
        store.append(_now_iso(), "reconcile_drift", {"drift": rec["drift"]})

    store.append(_now_iso(), "cycle_end", {
        "placed": len(summary["placed"]), "rejected": len(summary["rejected"]),
        "reconcile_ok": rec["ok"]})
    return summary


def _count_opened_today(store: Store, asof: str) -> int:
    return sum(
        1 for o in store.all_orders(limit=200)
        if str(o.get("ts", "")).startswith(asof) and o.get("status") not in ("rejected",)
    )
