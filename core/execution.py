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
import json
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Optional

from .brokers.base import BrokerAdapter, OrderRequest, Position
from .fills import entry_slippage_ps, net_debit_from_positions, signed_credit_ps
from .options_math import MIN_IV_OBSERVATIONS
from .manage import _is_filled, manage_open_positions
from .positions import order_to_position
from .reconcile import broker_legs, reconcile
from .risk import Decision, RiskContext, RiskGate, RiskLimits, _notional
from .sizing import SizeParams, size_contracts
from .store import DEAD_ORDER_STATUSES, Store
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
    advisor: Optional[Any] = None,
) -> dict[str, Any]:
    """Run one full paper cycle. Returns a summary dict for the CLI to print.

    `advisor` is the optional reasoning layer (agent.advisor.Advisor). It is
    READ-ONLY and VETO-ONLY: it runs AFTER the deterministic gate approves a
    candidate and can only DROP it (or lower its size) — never add a trade or
    bypass the gate. Disabled by default; opt-in via config `reasoning.enabled`
    + an ANTHROPIC_API_KEY."""
    asof = asof or datetime.now(timezone.utc).date().isoformat()
    ts = _now_iso()
    limits = RiskLimits.from_config(config.risk,
                                (config.raw.get("overnight", {}) or {}))
    gate = RiskGate(limits)

    account = adapter.get_account()

    # Persist a READ-ONLY account snapshot for the dashboard (store-only surface;
    # never read back by the gate). Best-effort — must never break the cycle.
    try:
        store.set_kv("equity", str(account.equity))
        store.set_kv("cash", str(account.cash))
        store.set_kv("buying_power", str(account.buying_power))
        store.set_kv("broker", adapter.name)
        store.set_kv("mode", "paper" if adapter.is_paper else "live")
    except Exception:
        pass

    # ── 0. Finalize working ENTRY orders: an accepted-but-unfilled entry tracked
    # last cycle becomes 'open' once the broker fills it (or is dropped if it
    # died). Until then it stays 'pending' — counted for risk, but NOT expected at
    # the broker by reconcile and NOT managed. ──
    _finalize_pending_entries(adapter, store)

    # ── 1. MANAGEMENT PASS — value & exit open positions before any new entry. ──
    manage_summary = manage_open_positions(adapter, store, config, asof)

    # ── 2. Daily P&L from our tracked positions (broker-agnostic, works in sim
    # AND live): ALL realized closes today (across every cycle this date, from the
    # store — not just this cycle) + unrealized mark on what's still open.
    daily_pnl = store.realized_pnl_on(asof) + manage_summary["unrealized_open"]

    # Proactively trip the kill switch on a daily-loss breach even if no new
    # order is evaluated this cycle.
    limit_dollars = limits.daily_loss_limit_pct * account.equity
    if account.equity > 0 and daily_pnl <= -abs(limit_dollars) and not store.kill_switch:
        store.set_kill_switch(True)
        store.append(_now_iso(), "kill_switch", {
            "engaged": True, "reason": "daily_loss_limit", "daily_pnl": daily_pnl})

    # ── 3. Gate's view of concurrent exposure = our OPEN tracked positions. ──
    positions: list[Position] = []
    for p in store.get_active_positions():   # open (filled) + pending (working) = committed risk
        # Capital footprint must match _notional: DEBIT verticals are worth the
        # net debit paid (== max_loss), NOT the strike width. Credit spreads and
        # condors are collateralized at the (wider) wing width.
        if p.get("structure") in ("call_debit_spread", "put_debit_spread"):
            mv = float(p["max_loss"] or 0.0)
        else:
            mv = float(p["width"]) * 100.0 * int(p["contracts"])
        positions.append(Position(
            symbol=p["underlying"], qty=p["contracts"], avg_price=0.0,
            market_value=mv, asset_class="option", underlying=p["underlying"],
            max_loss=float(p["max_loss"] or 0.0)))
    opened_today = _count_opened_today(store, asof)

    options_level = int(config.account.get("options_approval_level", 3))
    region = str(config.account.get("region", "US")).upper()
    size_params = SizeParams.from_config(config)

    # LIVE ramp caps (None on paper). When the adapter is live, the risk gate
    # enforces these as hard additional limits and fails closed if absent.
    from .golive import live_ramp_caps
    live_max_notional, live_max_positions = (
        live_ramp_caps(config) if not adapter.is_paper else (None, None))

    def _ctx() -> RiskContext:
        return RiskContext(
            account=account, positions=positions, is_paper=adapter.is_paper,
            kill_switch=store.kill_switch, positions_opened_today=opened_today,
            day_trades_trailing_5=account.pdt_day_trade_count,
            daily_pnl=daily_pnl, options_approval_level=options_level,
            region=region,
            live_ramp_max_notional=live_max_notional,
            live_ramp_max_positions=live_max_positions,
        )

    def _synthesize_position(order) -> None:
        """Add this order's exposure to the working position list so the next
        signal in the same cycle sees accumulated concentration/heat/leverage —
        AND decrement available buying power so the BP guard binds across multiple
        engines in one cycle (otherwise each order is checked against the full
        start-of-cycle BP and they can collectively over-commit margin)."""
        notional = _notional(order)
        positions.append(Position(
            symbol=order.underlying or (order.legs[0].symbol if order.legs else "?"),
            qty=1, avg_price=0.0, market_value=notional,
            asset_class="option", underlying=order.underlying,
            max_loss=order.max_loss,
        ))
        account.buying_power = max(0.0, account.buying_power - notional)

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

    warnings = capability_warnings(adapter, config)
    if warnings:
        summary["warnings"] = warnings
        store.append(_now_iso(), "capability_warning", {"warnings": warnings})

    # External data hub (earnings calendar / news / fundamentals). Built once; no
    # network unless a keyed provider is actually queried. Empty (keyless) hub ->
    # callers degrade gracefully (earnings_vol just stands aside).
    try:
        from .data.factory import build_data_hub
        data_hub = build_data_hub(config)
    except Exception:
        data_hub = None

    # Reasoning layer (opt-in, veto-only). Built only if enabled + a key exists;
    # otherwise None -> the cycle behaves exactly as before. Any failure to
    # construct degrades to None (no advisor, no behavior change).
    if advisor is None and _reasoning_enabled(config):
        try:
            from agent.advisor import Advisor
            advisor = Advisor()
        except Exception:
            advisor = None
    research_cache: dict[str, Any] = {}

    # Per-symbol decision trace, persisted for the read-only dashboard so the
    # operator can answer "why didn't it trade today?" without asking anyone. Five
    # separate bugs have produced an identical healthy-looking "0 signals", so the
    # REASON is as important as the count.
    decisions: list[dict[str, Any]] = []
    seen_ids: set = set()   # idempotency within this cycle (covers dry-run too)
    for symbol in config.watchlist:
        try:
            chain = adapter.get_option_chain(symbol)
            quote = adapter.get_quote(symbol)
            # Record today's ATM IV so a real broker BUILDS its own IV-rank history
            # over time (no paid feed). IV history is the broker's own series when
            # it has one (sim), else the bootstrapped store series.
            atm = _atm_iv_from_chain(chain, quote.mid)
            if atm:
                store.record_iv_snapshot(symbol, asof, atm)
            iv_hist = (adapter.iv_history(symbol) if hasattr(adapter, "iv_history")
                       else store.get_iv_history(symbol))
            bars = adapter.get_history(symbol, 120) if hasattr(adapter, "get_history") else []
            earnings = (adapter.get_earnings_date(symbol)
                        if hasattr(adapter, "get_earnings_date") else None)
            if earnings is None and data_hub is not None:
                e = data_hub.get_earnings(symbol)        # provider earnings calendar
                earnings = e.get("date") if isinstance(e, dict) else None
            # A real 252-day IV rank from the underlying's benchmark vol index,
            # when one exists (SPY/QQQ/IWM/DIA). Fails closed to the local
            # bootstrap on any error, stale feed, or unmapped symbol.
            proxy = None
            try:
                from .data.cboe import proxy_iv_rank
                proxy = proxy_iv_rank(symbol)
            except Exception:
                proxy = None
            if proxy:
                store.append(_now_iso(), "iv_rank_source", {
                    "symbol": symbol, "source": proxy["source"],
                    "rank": round(proxy["rank"], 4), "observations": proxy["observations"]})
            decisions.append({
                "symbol": symbol,
                "spot": round(quote.mid, 2) if quote.mid else None,
                "iv_rank": round(proxy["rank"], 4) if proxy else None,
                "iv_source": (proxy["source"] if proxy else "bootstrap"),
                "iv_obs": (proxy["observations"] if proxy else len(iv_hist or [])),
                "chain": len(chain), "signals": 0, "note": "",
            })

            sctx = StrategyContext(
                underlying=symbol, spot=quote.mid, option_chain=chain,
                iv_history=iv_hist,
                iv_rank_value=(proxy["rank"] if proxy else None),
                iv_rank_source=(proxy["source"] if proxy else ""),
                iv_rank_observations=(proxy["observations"] if proxy else 0),
                closes=[b["c"] for b in bars], highs=[b["h"] for b in bars],
                lows=[b["l"] for b in bars], asof=asof, earnings_date=earnings,
                config=config.strategies,
            )
        except Exception as e:  # data failure for one symbol must not kill the cycle
            store.append(_now_iso(), "data_error", {"symbol": symbol, "error": str(e)})
            continue

        for strat in strategies:
            for signal in strat.generate(sctx):
                summary["signals"].append({"symbol": symbol, "strategy": signal.strategy,
                                           "rationale": signal.rationale})
                for _d in decisions:
                    if _d["symbol"] == symbol:
                        _d["signals"] += 1
                base_order = signal_to_order(signal, asof)  # 1 contract (unit)

                # Dedup against both the persisted store AND orders already
                # handled earlier in THIS cycle (the store isn't written in a
                # dry-run, so a cycle-local set is needed to avoid double-counting).
                if (store.has_order(base_order.client_order_id)
                        or base_order.client_order_id in seen_ids):
                    summary["skipped_duplicates"].append(base_order.client_order_id)
                    continue
                seen_ids.add(base_order.client_order_id)

                # SIZE the order to the account (before the gate, so the gate
                # re-checks the SIZED order). 0 contracts => one contract already
                # exceeds the per-trade risk budget -> skip honestly.
                n = size_contracts(signal.max_loss, account.equity, size_params)
                if n < 1:
                    summary["rejected"].append({
                        "underlying": base_order.underlying, "strategy": base_order.strategy,
                        "reasons": [f"too small to size within per-trade risk "
                                    f"(${size_params.per_trade_risk_pct*account.equity:.0f}) "
                                    f"vs max loss ${signal.max_loss:.0f}/contract"]})
                    continue
                order = _scale_order(base_order, n)

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

                # ADVISOR VETO (opt-in, reduce-only). Runs ONLY on gate-approved
                # candidates and can ONLY drop them. The advisor never places an
                # order — placement still happens below, in deterministic code.
                if advisor is not None:
                    ann = _advisor_review(advisor, order, symbol, data_hub, research_cache)
                    store.append(_now_iso(), "advisor", {
                        "client_order_id": order.client_order_id, "keep": ann["keep"],
                        "allocation_mult": ann["allocation_mult"],
                        "thesis": ann["thesis"][:300]})
                    if not ann["keep"]:
                        summary.setdefault("vetoed", []).append({
                            "underlying": order.underlying, "strategy": order.strategy,
                            "thesis": ann["thesis"]})
                        continue
                    # The advisor may only scale DOWN (mult in [0,1]). Re-size the
                    # already-gate-approved order; a smaller order is still safe.
                    if size_params.enabled and ann["allocation_mult"] < 1.0 and n > 1:
                        n2 = max(1, int(n * ann["allocation_mult"]))
                        if n2 < n:
                            order = _scale_order(base_order, n2)   # re-scale from unit
                            n = n2

                if dry_run:
                    opened_today += 1
                    _synthesize_position(order)  # so caps accumulate in dry-run too
                    summary["placed"].append(
                        {"underlying": order.underlying, "strategy": order.strategy,
                         "contracts": n, "est_credit": round(order.est_credit * n, 2),
                         "dry_run": True, "client_order_id": order.client_order_id})
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
                    _synthesize_position(order)  # committed risk counts whether or not it filled yet
                    # Persist the position. FILLED -> 'open' (sim, marketable);
                    # accepted-but-unfilled -> 'pending' (finalized on fill next
                    # cycle). A pending entry is NOT yet expected at the broker by
                    # reconcile, so an unfilled limit order won't show false drift.
                    try:
                        pos_row = order_to_position(order, asof, _now_iso())
                        filled_now = _is_filled(result, 1)
                        pos_row["status"] = "open" if filled_now else "pending"
                        pid = store.open_position(pos_row)
                        if filled_now:
                            # Immediate fill — capture realized entry economics.
                            store.record_entry_fill(
                                pid, signed_credit_ps(result.filled_avg_price),
                                entry_slippage_ps(pos_row.get("entry_credit_ps"),
                                                  result.filled_avg_price))
                    except ValueError:
                        pass  # non-spread orders aren't tracked as managed positions yet
                    summary["placed"].append({
                        "underlying": order.underlying, "strategy": order.strategy,
                        "contracts": n, "status": result.status,
                        "est_credit": round(order.est_credit * n, 2),
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

    # Read-only last-cycle marker for the dashboard. Best-effort.
    try:
        for _d in decisions:
            if _d["signals"]:
                _d["note"] = "signal generated"
            elif _d["iv_rank"] is None:
                _d["note"] = f"no trusted IV rank yet ({_d['iv_obs']} obs)"
            elif not _d["chain"]:
                _d["note"] = "no option chain returned"
            else:
                _d["note"] = "IV/chain ok — no candidate cleared the filters"
        store.set_kv("cycle_decisions", json.dumps(decisions))
    except Exception:
        pass
    try:
        store.set_kv("last_cycle", json.dumps({
            "asof": asof, "ts": _now_iso(), "dry_run": bool(dry_run),
            "signals": len(summary["signals"]), "placed": len(summary["placed"]),
            "rejected": len(summary["rejected"]), "reconcile_ok": bool(rec["ok"]),
        }))
    except Exception:
        pass
    return summary


def _scale_order(base: OrderRequest, n: int) -> OrderRequest:
    """Scale a 1-contract order to n contracts: leg quantities and TOTAL max_loss
    scale by n; est_credit stays PER-CONTRACT (order_to_position derives per-share
    credit from it and reads contract count from the leg qty), and limit_price is
    a per-share net price (unchanged)."""
    import dataclasses
    n = max(1, int(n))
    if n == 1:
        return base
    new_legs = [dataclasses.replace(l, qty=l.qty * n) for l in base.legs]
    return dataclasses.replace(base, legs=new_legs, max_loss=base.max_loss * n)


def _reasoning_enabled(config) -> bool:
    import os
    r = (config.raw.get("reasoning", {}) if hasattr(config, "raw") else {}) or {}
    return bool(r.get("enabled", False)) and bool(os.environ.get("ANTHROPIC_API_KEY"))


def _advisor_review(advisor, order, symbol, data_hub, research_cache) -> dict:
    """Run the veto-only advisor on ONE gate-approved candidate. Fails safe to a
    keep=pass-through annotation if anything goes wrong."""
    try:
        if symbol not in research_cache:
            if data_hub is not None:
                from .data.research import build_research_brief
                research_cache[symbol] = build_research_brief(symbol, data_hub)
            else:
                research_cache[symbol] = None
        candidate = {
            "client_order_id": order.client_order_id, "underlying": order.underlying,
            "strategy": order.strategy, "rationale": order.rationale,
            "max_loss": order.max_loss, "est_credit": order.est_credit,
        }
        anns = advisor.annotate([candidate], research_cache[symbol])
        if anns and isinstance(anns[0], dict):
            a = anns[0]
            return {"keep": bool(a.get("keep", True)),
                    "allocation_mult": float(a.get("allocation_mult", 1.0)),
                    "thesis": str(a.get("thesis", ""))}
    except Exception:
        pass
    return {"keep": True, "allocation_mult": 1.0, "thesis": ""}


def _atm_iv_from_chain(chain, spot: float):
    """Nearest-strike implied vol from a chain (the ATM IV snapshot)."""
    cands = [c for c in chain if c.implied_vol and c.implied_vol > 0]
    if not cands or spot <= 0:
        return None
    return min(cands, key=lambda c: abs(c.strike - spot)).implied_vol


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _legs_for_position(pos_row: dict, broker_positions: list) -> list:
    """The broker Position objects belonging to this tracked structure."""
    u = str(pos_row.get("underlying") or "").upper()
    exp = str(pos_row.get("expiration") or "")
    strikes = {float(pos_row.get("short_strike") or 0), float(pos_row.get("long_strike") or 0)}
    out = []
    for bp in broker_positions or []:
        if (str(bp.underlying or bp.symbol).upper() == u
                and str(bp.option_expiration or "") == exp
                and bp.option_strike is not None
                and float(bp.option_strike) in strikes):
            out.append(bp)
    return out


def _held_units(pos_row: dict, held: dict) -> int:
    """How many COMPLETE units of this structure the broker actually holds.

    A partial fill is the normal outcome of a multi-contract limit combo: we asked
    for 10, one filled, the rest expired with the DAY order. The broker then holds
    a real 1-contract spread while our row says 10 and the order says Cancelled.
    Returns the number of whole structures supported by the held legs (0 if the
    structure is incomplete in either direction)."""
    import json as _json
    from .reconcile import _leg_key
    n = int(pos_row.get("contracts") or 0)
    if n <= 0:
        return 0
    u, exp = pos_row.get("underlying"), pos_row.get("expiration")
    want: list[tuple] = []
    if pos_row.get("structure") == "iron_condor":
        raw = pos_row.get("legs_json")
        if not raw:
            return 0
        j = _json.loads(raw)
        want = [(_leg_key(u, exp, j["sp"], "P"), -n), (_leg_key(u, exp, j["lp"], "P"), +n),
                (_leg_key(u, exp, j["sc"], "C"), -n), (_leg_key(u, exp, j["lc"], "C"), +n)]
    else:
        fam = pos_row.get("family") or ("call" if "call" in (pos_row.get("structure") or "") else "put")
        r = "C" if fam == "call" else "P"
        want = [(_leg_key(u, exp, pos_row["short_strike"], r), -n),
                (_leg_key(u, exp, pos_row["long_strike"], r), +n)]
    units = None
    for key, qty in want:
        per_unit = qty / n                      # +1 or -1 per structure
        have = float(held.get(key, 0.0))
        if per_unit < 0:
            u_leg = int(max(0.0, -have))        # need short
        else:
            u_leg = int(max(0.0, have))         # need long
        units = u_leg if units is None else min(units, u_leg)
    return int(units or 0)


def _position_is_held(pos_row: dict, held: dict) -> tuple[bool, bool]:
    """(all_legs_held, any_leg_held) for this tracked position at the broker.

    Compares against reconcile's signed leg map. ALL legs in the correct direction
    means the entry filled. ANY leg means the broker holds real exposure we must
    NOT delete — a partial fill (or a single leg that filled) is a genuine position
    needing attention, and dropping the row would leave it untracked and unmanaged
    with nothing in the system aware of it."""
    import json as _json
    from .reconcile import _leg_key
    n = int(pos_row.get("contracts") or 0)
    if n <= 0:
        return False
    u, exp = pos_row.get("underlying"), pos_row.get("expiration")
    want: list[tuple] = []
    if pos_row.get("structure") == "iron_condor":
        raw = pos_row.get("legs_json")
        if not raw:
            return False
        j = _json.loads(raw)
        want = [(_leg_key(u, exp, j["sp"], "P"), -n), (_leg_key(u, exp, j["lp"], "P"), +n),
                (_leg_key(u, exp, j["sc"], "C"), -n), (_leg_key(u, exp, j["lc"], "C"), +n)]
    else:
        fam = pos_row.get("family") or ("call" if "call" in (pos_row.get("structure") or "") else "put")
        r = "C" if fam == "call" else "P"
        want = [(_leg_key(u, exp, pos_row["short_strike"], r), -n),
                (_leg_key(u, exp, pos_row["long_strike"], r), +n)]
    all_held, any_held = True, False
    for key, qty in want:
        have = float(held.get(key, 0.0))
        if abs(have) > 1e-9:
            any_held = True
        if qty < 0 and have > qty + 1e-9:      # need at least this short
            all_held = False
        if qty > 0 and have < qty - 1e-9:      # need at least this long
            all_held = False
    return all_held, any_held


def _finalize_pending_entries(adapter: BrokerAdapter, store: Store) -> None:
    """Flip 'pending' entry positions to 'open' once the broker confirms the fill,
    or drop them if the order died. Mirrors the pending-close finalizer."""
    pend = store.get_pending_positions()
    if not pend:
        return
    # Same rule as the close finalizer: a broker that cannot testify resolves
    # nothing. On a failed IB connect the factory falls back to SIM, whose empty
    # positions feed would read as "the day order died unfilled" and DELETE a live
    # position's row from one bad connect.
    if getattr(adapter, "name", "") == "sim":
        return
    try:
        bro = {o.client_order_id: o for o in adapter.list_orders()}
    except Exception:
        return  # can't confirm -> leave pending (still counted for risk)
    # Broker POSITIONS are the durable truth: unlike the session-scoped order
    # feed they are repopulated on every connect. Used below to resolve entries
    # the order feed can no longer see.
    try:
        held = broker_legs(adapter)
    except Exception:
        held = None

    for p in pend:
        o = bro.get(p["client_order_id"])
        if o is None:
            # The order is GONE from the broker's view. With a one-process-per-run
            # cron this is the COMMON case, not an exception: an order that filled
            # (or was killed as a DAY order) between cycles simply is not in the
            # feed. Treating that as "still working" strands the row as immortally
            # 'pending' — never managed, never exited, yet still consuming the
            # concurrency/heat/cluster budget, with a real position possibly live
            # at the broker. So resolve it against positions instead.
            if held is None:
                continue                      # can't confirm -> leave pending
            all_held, any_held = _position_is_held(p, held)
            if all_held:
                store.set_position_status(p["id"], "open")
                store.append(_now_iso(), "entry_filled", {
                    "position_id": p["id"], "underlying": p["underlying"],
                    "via": "broker_positions", "entry_slip_ps": None,
                    "note": "order feed no longer had it; legs confirmed held"})
            elif any_held:
                # PARTIAL: the broker holds some legs but not the full structure.
                # Never delete — that would leave real, unhedged exposure with
                # nothing tracking it. Keep it pending and say so loudly; this
                # needs a human, because a half-filled vertical is not defined-risk.
                store.append(_now_iso(), "partial_fill_detected", {
                    "position_id": p["id"], "underlying": p["underlying"],
                    "severity": "high",
                    "note": "broker holds SOME legs of this spread but not all — "
                            "the structure may be unhedged. Investigate before trading."})
            elif str(p.get("opened_asof") or "") < _today_iso():
                # Not held, and it is no longer today: the DAY order died unfilled.
                store.set_order_status(p["client_order_id"], "canceled")
                store.delete_position(p["id"])
                store.append(_now_iso(), "entry_dropped", {
                    "position_id": p["id"], "underlying": p["underlying"],
                    "via": "stale_pending", "note": "day order expired unfilled"})
            continue
        st = (o.status or "").lower()
        if _is_filled(o, 1):
            store.set_position_status(p["id"], "open")
            # Capture realized ENTRY economics now — IB's fill feed is session-
            # scoped, so this price is unrecoverable after today.
            slip = entry_slippage_ps(p.get("entry_credit_ps"), o.filled_avg_price)
            store.record_entry_fill(p["id"], signed_credit_ps(o.filled_avg_price), slip)
            store.append(_now_iso(), "entry_filled", {"position_id": p["id"],
                                                       "underlying": p["underlying"],
                                                       "entry_slip_ps": slip})
        elif st in DEAD_ORDER_STATUSES:
            # Reconcile the orders table too, or has_order keeps the deterministic
            # client_order_id dedup-blocked all day and the position can never be
            # re-entered.
            store.set_order_status(p["client_order_id"], "canceled")
            # BEFORE deleting: does the broker actually hold anything? A dead order
            # does NOT mean nothing filled. A multi-contract limit combo routinely
            # fills PARTIALLY and the remainder is cancelled at the close, leaving a
            # real position behind. Deleting the row then makes that position
            # permanently invisible — never managed, never exited, riding to expiry
            # with nothing in the system aware of it. (Observed live on the very
            # first trade: 1 of 10 contracts filled, order reported Cancelled with
            # filled_qty=0.0.) Adopt broker truth instead.
            units = _held_units(p, held) if held is not None else 0
            if units > 0:
                ordered = int(p.get("contracts") or 0)
                store.adopt_partial_fill(p["id"], units)
                # The ORDER feed gave no fill price (IB reports avgFillPrice=0.0 on
                # a partially-filled order that then died), but the POSITIONS feed
                # still carries each leg's average cost. Reconstruct the realized
                # net price from it — otherwise the slippage for this trade, the
                # one number that decides whether edge survives costs, is lost.
                try:
                    fill_ps = net_debit_from_positions(
                        _legs_for_position(p, adapter.get_positions()), units)
                    intended = p.get("entry_credit_ps")
                    if fill_ps is not None and intended is not None:
                        signed_intent = (float(intended) if p.get("is_credit")
                                         else -float(intended))
                        store.record_entry_fill(
                            p["id"], fill_ps, round(signed_intent - fill_ps, 4))
                except Exception:
                    pass
                store.append(_now_iso(), "partial_fill_adopted", {
                    "position_id": p["id"], "underlying": p["underlying"],
                    "ordered": ordered, "filled": units, "severity": "high",
                    "note": "order died after a PARTIAL fill; adopted the broker's "
                            "actual size so the position is managed and exited"})
                continue
            store.delete_position(p["id"])
            store.append(_now_iso(), "entry_dropped", {"position_id": p["id"],
                                                       "underlying": p["underlying"]})


def capability_warnings(adapter: BrokerAdapter, config) -> list[str]:
    """Loudly flag when a REAL broker can't supply the data an enabled engine
    needs — otherwise the system is silently inert (places nothing, no error)."""
    if adapter.name == "sim":
        return []
    s = config.strategies or {}
    def on(name):
        blk = s.get(name)
        return blk is not None and blk.get("enabled", True)
    warns = []
    # IV history is bootstrapped from daily ATM-IV snapshots (no broker method
    # needed), but it takes time to accrue a meaningful rank.
    if (on("premium_harvest") or on("earnings_vol")) and not hasattr(adapter, "iv_history"):
        warns.append("IV-rank history is bootstrapping from daily ATM-IV snapshots — "
                     f"premium_harvest / earnings_vol stand aside until "
                     f"{MIN_IV_OBSERVATIONS}+ sessions accrue — a shorter window makes "
                     "IV rank actively misleading, not merely noisy.")
    if on("volatility_breakout") and not hasattr(adapter, "get_history"):
        warns.append("price history not available — volatility_breakout will NOT signal.")
    if on("earnings_vol") and not hasattr(adapter, "get_earnings_date"):
        warns.append("earnings calendar not available — earnings_vol will NOT signal "
                     "(wire a data provider; see docs/11-data-providers.md).")
    return warns


def _count_opened_today(store: Store, asof: str) -> int:
    """Count only OPENING orders placed today. Closing orders (CLOSE-*) reduce
    risk and must not consume the daily new-position budget."""
    return sum(
        1 for o in store.all_orders(limit=400)
        if str(o.get("ts", "")).startswith(asof)
        and (o.get("status") or "").lower() not in DEAD_ORDER_STATUSES
        and not str(o.get("client_order_id", "")).startswith("CLOSE-")
        and not str(o.get("strategy", "")).startswith("close_")
    )
