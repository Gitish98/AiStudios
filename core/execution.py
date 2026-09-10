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
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Optional

from .brokers.base import BrokerAdapter, OrderRequest, Position
from .fills import (_usable, combo_fill_from_executions, entry_slippage_ps,
                    net_debit_from_positions, signed_credit_ps, signed_intent_ps)
from .options_math import MIN_IV_OBSERVATIONS
from .manage import _executions_for, _is_filled, _orders_by_attempt, manage_open_positions
from .positions import order_to_position
from .reconcile import broker_legs, reconcile
from .risk import Decision, RiskContext, RiskGate, RiskLimits, _notional
from .sizing import SizeParams, size_contracts
from .store import DEAD_ORDER_STATUSES, Store
from strategies.base import Signal, StrategyContext


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def degraded_sim(store: Store, adapter) -> bool:
    """True when a SIM adapter is about to run against a store that has REAL
    broker history. Such a cycle must not run: it would bank synthetic IV
    snapshots into the real IV series and could open fictional sim-filled
    positions in the real book. A fresh dev store is unaffected.

    Checks the STICKY 'ever_real_broker' flag (write-once, set by any real-broker
    cycle), falling back to the rolling 'broker' kv. The rolling kv alone is not
    enough: it is overwritten every cycle, so one sim-fallback cycle would blind
    the check exactly when it matters."""
    return (getattr(adapter, "name", "") == "sim"
            and ((store.get_kv("ever_real_broker") == "1")
                 or (store.get_kv("broker") or "sim") != "sim"))


def _order_overlaps_book(order: OrderRequest, store: Store):
    """The leg (as 'SYM exp strike right') this order shares with any tracked
    ACTIVE position, or None.

    Two positions holding the same contract make the broker's per-key leg map
    ambiguous: fills, closes and assignment checks can no longer attribute a leg
    to a position (the resolvers read NET quantities per contract key). Rather
    than make every resolver quantity-aware for a rare case, refuse to create
    the ambiguity: one tracked position per option contract, ever."""
    import json as _json
    from .reconcile import _leg_key
    held: set = set()
    blocked_underlyings: set = set()
    for p in store.get_active_positions():
        try:
            u, exp = p.get("underlying"), p.get("expiration")
            if p.get("structure") == "iron_condor":
                j = _json.loads(p["legs_json"])         # raises if absent/corrupt
                for k, r in (("sp", "P"), ("lp", "P"), ("sc", "C"), ("lc", "C")):
                    held.add(_leg_key(u, exp, j[k], r))
            else:
                fam = p.get("family") or ("call" if "call" in (p.get("structure") or "") else "put")
                r = "C" if fam == "call" else "P"
                held.add(_leg_key(u, exp, p["short_strike"], r))
                held.add(_leg_key(u, exp, p["long_strike"], r))
        except Exception:
            # FAIL CLOSED PER ROW: a row whose legs cannot be parsed might hold
            # anything on its underlying — block that underlying rather than
            # either crashing every future entry or silently ignoring the row.
            blocked_underlyings.add(str(p.get("underlying") or "").upper())
    if (order.underlying or "").upper() in blocked_underlyings:
        return f"{order.underlying} (unparseable tracked position — failing closed)"
    for leg in order.legs:
        if leg.strike is None or not leg.expiration:
            continue
        key = _leg_key(order.underlying or leg.symbol, leg.expiration,
                       leg.strike, "C" if leg.option_type == "call" else "P")
        if key in held:
            return f"{key[0]} {key[2]:g}{key[3]} {key[1]}"
    return None


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
    _t0 = time.monotonic()
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
        rate = float(getattr(adapter, "usd_rate", 0.0) or 0.0)
        if rate > 0:
            store.record_fx_rate(asof, rate)   # feeds the dashboard's CAD view
        if adapter.name != "sim":
            # WRITE-ONCE, never cleared: "this store has traded against a real
            # broker". The rolling 'broker' kv is overwritten every cycle, so a
            # single pre-guard sim-fallback cycle would blind a guard that read
            # it — the sticky flag cannot be blinded by the thing it guards
            # against. (Preflight review finding, before this ever shipped.)
            store.set_kv("ever_real_broker", "1")
    except Exception:
        pass

    # ── IB entitlement-notice census. Under delayed data every option request
    # emits Error 10091 (and each ARCA-listed stock 10089) — ~2,300 lines a cycle
    # that all say the same true thing: "delayed market data is available."
    # Count them, print the first of each so the fact is never silent, drop the
    # rest, and journal the counts with cycle_end. See core/ib_noise.py. ──
    from .ib_noise import install_notice_filter
    _notices = install_notice_filter()
    _notices.reset()

    # ── 0. Finalize working ENTRY orders: an accepted-but-unfilled entry tracked
    # last cycle becomes 'open' once the broker fills it (or is dropped if it
    # died). Until then it stays 'pending' — counted for risk, but NOT expected at
    # the broker by reconcile and NOT managed. ──
    _finalize_pending_entries(adapter, store)

    # ── 0b. Recover any entry price that was never captured. The capture is
    # one-shot at fill time, so a single miss used to be permanent — and a missing
    # measurement is honestly stored as NULL, which is indistinguishable at a
    # glance from a cell nothing has written yet. While the position is open the
    # broker's positions feed still holds the answer, so ask again. ──
    _backfill_missing_entry_fills(adapter, store)

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
    frozen_syms = store.frozen_underlyings()
    seen_ids: set = set()   # idempotency within this cycle (covers dry-run too)
    for symbol in config.watchlist:
        try:
            if symbol in frozen_syms:
                # Frozen = a human must look before ANY new risk on this name.
                decisions.append({
                    "symbol": symbol, "spot": None, "iv_rank": None,
                    "iv_source": "", "iv_obs": 0, "chain": 0, "signals": 0,
                    "note": "FROZEN pending operator review — no new entries"})
                continue
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

                clash = _order_overlaps_book(base_order, store)
                if clash:
                    summary["rejected"].append({
                        "underlying": base_order.underlying,
                        "strategy": base_order.strategy,
                        "reasons": [f"legs overlap a tracked position ({clash}) — "
                                    "one position per contract, or fills/closes "
                                    "become unattributable"]})
                    continue

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
                                                  result.filled_avg_price,
                                                  bool(pos_row.get("is_credit", 1))))
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
        # Stray SHARES in a tracked account = the assignment signature. Freeze the
        # underlying immediately: no new entries, no blind closes, until a human
        # clears it (cli.py unfreeze). Journaled at critical.
        for d in rec["drift"]:
            if d.get("kind") == "untracked_equity_at_broker" and d.get("underlying"):
                store.freeze_underlying(
                    d["underlying"], "unexpected shares at broker — possible assignment")
                store.append(_now_iso(), "underlying_frozen", {
                    "underlying": d["underlying"], "severity": "critical",
                    "leg": d.get("leg"),
                    "note": "equity position with no tracked origin; trading frozen "
                            "pending operator review (cli.py unfreeze)"})

    summary["duration_secs"] = round(time.monotonic() - _t0, 1)
    # Journal today's execution report so the finalizers keep a durable copy
    # past IB's nightly reset. The report is day-scoped; the journal is not.
    # Never on sim, which has no report; never fatal.
    try:
        if getattr(adapter, "name", "") != "sim":
            _ex = adapter.get_executions() or []
            if _ex:
                store.append(_now_iso(), "executions_snapshot", {
                    "source": "get_executions", "asof": asof, "count": len(_ex),
                    "fills": [dict(vars(e)) for e in _ex]})
    except Exception:
        pass
    summary["ib_notices"] = _notices.census()
    summary["ib_market_data_type"] = getattr(adapter, "market_data_type", None)
    store.append(_now_iso(), "cycle_end", {
        "placed": len(summary["placed"]), "rejected": len(summary["rejected"]),
        "reconcile_ok": rec["ok"], "duration_secs": summary["duration_secs"],
        "ib_notices": summary["ib_notices"]})

    # ── SELF-AUDIT. Assert what we believe about our own book and journal the
    # answer — including when it is clean, because an alarm that only writes on
    # failure is indistinguishable from an alarm that is switched off. Runs AFTER
    # cycle_end so a check can see this cycle, and never raises: it is a smoke
    # detector, not load-bearing wiring. ──
    try:
        from .selfaudit import journal_self_audit
        _audit = journal_self_audit(store)
        if _audit:
            summary["audit"] = [f.code for f in _audit]
    except Exception:
        pass

    # Read-only last-cycle marker for the dashboard. Best-effort.
    try:
        for _d in decisions:
            if _d.get("note"):
                continue        # a pre-set note (e.g. FROZEN) must not be clobbered
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


def _legs_for_position(pos_row: dict, broker_positions: list,
                       units: int = 1) -> Optional[list]:
    """The broker Position objects forming this structure — or None.

    STRICT by design. This feeds a price reconstruction, and a price is a number
    the cost model and the go-live gate will believe, so it must be all-or-
    nothing: either the broker holds the WHOLE structure at the size we are
    asking about, or we have no measurement. Three ways the loose version lied:

      * it ignored the option RIGHT, so a 745P/750P spread and a 745C/750C spread
        on the same expiry matched each other's legs;
      * it read strikes out of short_strike/long_strike, which for an iron condor
        hold only the PUT wing — so it netted 2 legs of 4 and called it the fill;
      * it required no completeness at all, so one surviving leg after an
        assignment reconstructed a $921/share "fill" on a $1-wide spread.

    Every one of those produces a plausible-looking number that passes
    plausible_slip_ps and is then promoted from assumption to MEASURED evidence.
    Standing lesson #2 in its sharpest form: we would rather have no measurement
    than a confident wrong one."""
    spec = _expected_legs(pos_row)
    if not spec or units <= 0:
        return None
    u = str(pos_row.get("underlying") or "").upper()
    exp = str(pos_row.get("expiration") or "")
    by_leg: dict[tuple, Any] = {}
    for bp in broker_positions or []:
        if str(bp.underlying or bp.symbol).upper() != u:
            continue
        if str(bp.option_expiration or "") != exp:
            continue
        if bp.option_strike is None or bp.option_right is None:
            continue
        by_leg[(round(float(bp.option_strike), 4),
                str(bp.option_right).upper()[:1])] = bp
    out = []
    for _key, per_unit, strike, right in spec:
        bp = by_leg.get((round(float(strike), 4), str(right).upper()[:1]))
        if bp is None:
            return None                       # structure incomplete at the broker
        qty = float(getattr(bp, "qty", 0.0) or 0.0)
        # Direction AND size must both hold: a short leg must be short, and the
        # broker must hold at least the number of units we are pricing.
        if per_unit < 0 and qty > -units + 1e-9:
            return None
        if per_unit > 0 and qty < units - 1e-9:
            return None
        out.append(bp)
    return out


def _expected_legs(pos_row: dict) -> Optional[list[tuple]]:
    """The legs this structure IS: [(leg_key, per_unit_qty, strike, right), ...],
    or None when the row cannot describe itself.

    Extracted because three callers each re-derived it and one of them got it
    WRONG in a way that mattered. _legs_for_position matched broker legs on
    underlying + expiration + `strike in {short_strike, long_strike}` — no option
    right, no structure awareness. For an iron condor only the PUT wing lives in
    those two columns (the calls are in legs_json), so it matched 2 legs of 4 and
    happily reconstructed "the fill" from half the position. A preflight review
    reproduced it: a condor with zero real slippage produced a fabricated 0.60/sh
    adverse fill that PASSED plausible_slip_ps and would have been promoted from
    assumption to MEASURED evidence in the go-live gate.

    Returning None — rather than a best guess — is the whole point: a structure we
    cannot describe must produce no number at all."""
    import json as _json
    from .reconcile import _leg_key
    n = int(pos_row.get("contracts") or 0)
    if n <= 0:
        return None
    u, exp = pos_row.get("underlying"), pos_row.get("expiration")
    if pos_row.get("structure") == "iron_condor":
        raw = pos_row.get("legs_json")
        if not raw:
            return None
        try:
            j = _json.loads(raw)
            return [(_leg_key(u, exp, j["sp"], "P"), -1, float(j["sp"]), "P"),
                    (_leg_key(u, exp, j["lp"], "P"), +1, float(j["lp"]), "P"),
                    (_leg_key(u, exp, j["sc"], "C"), -1, float(j["sc"]), "C"),
                    (_leg_key(u, exp, j["lc"], "C"), +1, float(j["lc"]), "C")]
        except (ValueError, KeyError, TypeError):
            return None
    fam = pos_row.get("family") or ("call" if "call" in (pos_row.get("structure") or "") else "put")
    r = "C" if fam == "call" else "P"
    try:
        short_k, long_k = float(pos_row["short_strike"]), float(pos_row["long_strike"])
    except (KeyError, TypeError, ValueError):
        return None
    return [(_leg_key(u, exp, short_k, r), -1, short_k, r),
            (_leg_key(u, exp, long_k, r), +1, long_k, r)]


def _held_units(pos_row: dict, held: dict) -> int:
    """How many COMPLETE units of this structure the broker actually holds.

    A partial fill is the normal outcome of a multi-contract limit combo: we asked
    for 10, one filled, the rest expired with the DAY order. The broker then holds
    a real 1-contract spread while our row says 10 and the order says Cancelled.
    Returns the number of whole structures supported by the held legs (0 if the
    structure is incomplete in either direction)."""
    spec = _expected_legs(pos_row)
    if not spec:
        return 0
    units = None
    for key, per_unit, _strike, _right in spec:
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
    # A row that cannot describe its own legs is NOT evidence that nothing is
    # held. Both early exits used to `return False` from a function whose
    # signature promises a tuple, so the caller's `all_held, any_held = ...`
    # raised TypeError and took the whole cycle down with it — every open
    # position left unmarked and unmanaged for that run.
    n = int(pos_row.get("contracts") or 0)
    spec = _expected_legs(pos_row)
    if not spec:
        return False, False
    all_held, any_held = True, False
    for key, per_unit, _strike, _right in spec:
        qty = per_unit * n
        have = float(held.get(key, 0.0))
        if abs(have) > 1e-9:
            any_held = True
        if qty < 0 and have > qty + 1e-9:      # need at least this short
            all_held = False
        if qty > 0 and have < qty - 1e-9:      # need at least this long
            all_held = False
    return all_held, any_held


def _capture_entry_fill_from_book(p: dict, units: int, store: Store,
                                  adapter) -> Optional[float]:
    """Reconstruct and persist the realized ENTRY price from the broker's POSITIONS
    feed. Returns the adverse slippage per share, or None if unrecoverable.

    IB's fill feed is session-scoped, so a cron that runs one process per cycle
    almost never sees the execution that filled its own order — the order is
    simply gone from the feed by the next run. The POSITIONS feed is the durable
    record: it carries each leg's average cost and is repopulated on every
    connect. That makes it the ONLY surviving evidence of what we actually paid.

    This lives in one function because it was previously inlined in exactly one
    of the two resolution branches: a PARTIALLY filled entry got its price
    reconstructed, while a FULLY filled one — the common case — was promoted to
    open with the price silently dropped. Observed live on trade #3 (SPY 763/762
    x10, 2026-08-31): the fill was real, the broker had the numbers the whole
    time, and we recorded `entry_slip_ps: null`. Nothing was corrupted (the
    _usable() guard refuses IB's 0.0), but the measurement was lost, and the
    go-live gate's cost_measured_pct is built out of exactly these."""
    try:
        legs = _legs_for_position(p, adapter.get_positions(), units)
    except Exception:
        return None
    if legs is None:
        return None            # incomplete/ambiguous at the broker -> no number
    fill_ps = net_debit_from_positions(legs, units)
    intended = signed_intent_ps(p.get("entry_credit_ps"),
                                bool(p.get("is_credit", 1)), closing=False)
    if fill_ps is None or intended is None:
        return None
    slip = round(intended - fill_ps, 4)
    store.record_entry_fill(p["id"], fill_ps, slip)
    return slip


def _capture_entry_fill_from_executions(p: dict, store: Store, adapter) -> Optional[float]:
    """The ENTRY price from the execution report — the exact fill, not a
    reconstruction from average costs. Returns the adverse slippage per share,
    or None when the report does not carry this order (day-scoped; the
    positions-feed reconstruction is the fallback for that)."""
    coid = str(p.get("client_order_id") or "")
    if not coid:
        return None
    try:
        cf = combo_fill_from_executions(_executions_for(adapter, store, coid), coid)
    except Exception:
        return None
    if not cf:
        return None
    fill_ps = signed_credit_ps(cf[1])
    intended = signed_intent_ps(p.get("entry_credit_ps"),
                                bool(p.get("is_credit", 1)), closing=False)
    if fill_ps is None or intended is None:
        return None
    slip = round(intended - fill_ps, 4)
    store.record_entry_fill(p["id"], fill_ps, slip)
    return slip


def _backfill_missing_entry_fills(adapter: BrokerAdapter, store: Store) -> None:
    """Self-heal: any OPEN position whose entry price was never captured, while
    the broker still holds its legs.

    The capture is one-shot at fill time by design, which means a single missed
    capture used to be permanent — and the miss is invisible, because the honest
    representation of a missing measurement is NULL, which looks like every other
    empty cell. This pass turns 'lost forever' into 'recovered on the next cycle'
    for as long as the position is open. It only ever writes where the value is
    absent (record_entry_fill is not called when reconstruction fails), so it
    cannot overwrite a real fill-time measurement with a later reconstruction."""
    if getattr(adapter, "name", "") == "sim":
        return                                # sim's book cannot testify
    try:
        rows = [dict(r) for r in store.conn.execute(
            "SELECT * FROM positions WHERE status = 'open' "
            "AND entry_fill_ps IS NULL ORDER BY id")]
    except Exception:
        return
    if not rows:
        return
    for p in rows:
        units = int(p.get("contracts") or 0)
        if units <= 0:
            continue
        slip = _capture_entry_fill_from_executions(p, store, adapter)
        if slip is None:
            slip = _capture_entry_fill_from_book(p, units, store, adapter)
        if slip is not None:
            store.append(_now_iso(), "entry_fill_backfilled", {
                "position_id": p["id"], "underlying": p["underlying"],
                "entry_slip_ps": slip,
                "note": "entry price recovered from the broker positions feed; "
                        "it was not captured when the fill was first observed"})


def _adopt_partial(p: dict, units: int, store: Store, adapter) -> None:
    """Adopt a partially-filled entry at the broker's ACTUAL size: resize the row
    (contracts + rescaled max_loss), promote to open, and reconstruct the fill
    price from the positions feed's per-leg average costs (the order feed reports
    avgFillPrice=0.0 for a partially-filled order that died). One helper, used by
    BOTH resolution branches — the dead-status one and the vanished-order one —
    because they drifted once already: trade #2's 5/10 fill sat flagged for two
    days when the session-scoped feed forgot its order, so only the dead-status
    branch (which never fired) knew how to adopt."""
    ordered = int(p.get("contracts") or 0)
    store.adopt_partial_fill(p["id"], units)
    _capture_entry_fill_from_book(p, units, store, adapter)
    store.append(_now_iso(), "partial_fill_adopted", {
        "position_id": p["id"], "underlying": p["underlying"],
        "ordered": ordered, "filled": units, "severity": "high",
        "note": "partial fill adopted at the broker's actual size; "
                "position is now open and managed"})


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
        bro = _orders_by_attempt(store, adapter.list_orders())
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
                # The order feed is gone, but the POSITIONS feed still carries
                # what we paid. Reconstruct it HERE: this branch is the common
                # case for a cron, and it used to journal entry_slip_ps: null
                # while the numbers sat available at the broker.
                # The execution report has the exact fill; the positions feed's
                # average cost is the reconstruction we fall back to.
                slip = _capture_entry_fill_from_executions(p, store, adapter)
                via = "executions"
                if slip is None:
                    slip = _capture_entry_fill_from_book(
                        p, int(p.get("contracts") or 0), store, adapter)
                    via = "broker_positions"
                store.append(_now_iso(), "entry_filled", {
                    "position_id": p["id"], "underlying": p["underlying"],
                    "via": via, "entry_slip_ps": slip,
                    "note": "order feed no longer had it; legs confirmed held"})
            elif any_held:
                # Some legs held. FIRST ask whether they form COMPLETE units of
                # the structure: a partially filled combo (5 of 10) leaves whole
                # defined-risk spreads behind, and those must be ADOPTED and
                # managed — observed live when trade #2's 5/10 fill sat flagged
                # for two days because its order vanished from the session-scoped
                # feed and only the (never-firing) dead-status branch knew how to
                # adopt. Only a LOPSIDED remainder — legs that do not form whole
                # units — is the assignment signature that needs a human.
                units = _held_units(p, held)
                if units > 0:
                    _adopt_partial(p, units, store, adapter)
                else:
                    store.append(_now_iso(), "partial_fill_detected", {
                        "position_id": p["id"], "underlying": p["underlying"],
                        "severity": "high",
                        "note": "broker holds SOME legs of this spread but they do "
                                "not form complete units — possibly assigned/"
                                "unhedged. Investigate before trading."})
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
            # IB reports Filled with avgFillPrice=0.0 for an order reconstructed
            # in a new process. The execution report still has the real price.
            fill_px, via = o.filled_avg_price, "order_feed"
            if _usable(fill_px) is None:
                cf = combo_fill_from_executions(
                    _executions_for(adapter, store, p["client_order_id"]),
                    p["client_order_id"])
                if cf:
                    fill_px, via = cf[1], "order_feed+executions"
            slip = entry_slippage_ps(p.get("entry_credit_ps"), fill_px,
                                     bool(p.get("is_credit", 1)))
            store.record_entry_fill(p["id"], signed_credit_ps(fill_px), slip)
            store.append(_now_iso(), "entry_filled", {"position_id": p["id"],
                                                       "underlying": p["underlying"],
                                                       "via": via,
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
                _adopt_partial(p, units, store, adapter)
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
