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
from .fills import exit_slippage_ps, signed_credit_ps
from .exdiv import exdiv_risk
from .positions import ManageParams, dte_from, evaluate_exit
from .store import DEAD_ORDER_STATUSES, Store


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _usable_mid(c) -> Optional[float]:
    """A contract's mid, or None when the broker gave us NO usable price.

    Critical distinction: IB fields arrive as NaN when absent and the adapter
    collapses those to 0.0, so a quote-less contract presents as bid=ask=last=0
    and `OptionContract.mid` returns 0.0 — indistinguishable from a genuine
    "worthless" price. For a short credit spread a 0.0 mark reads as MAXIMUM
    PROFIT and fires the profit-target exit, closing a live position at a
    fabricated price on missing data. Absence must be None, never a number."""
    bid, ask, last = (c.bid or 0.0), (c.ask or 0.0), (c.last or 0.0)
    if bid > 0 and ask > 0:
        return round((bid + ask) / 2, 4)
    if last > 0:
        return last
    return None


def _marker(adapter: BrokerAdapter, underlying: str, expiration: str):
    """Return a mark(strike, right) -> per-share mid (or None).

    The chain is fetched ONCE per position and cached across the leg lookups:
    the adapter now streams with a settle delay per chunk, so calling it per leg
    would multiply an already ~6-minute cycle."""
    cache: dict = {}

    def _mark(strike: float, right: str) -> Optional[float]:
        if hasattr(adapter, "mark_option"):
            return adapter.mark_option(underlying, expiration, strike, right)
        if "chain" not in cache:
            cache["chain"] = adapter.get_option_chain(underlying, expiration)
        c = next((c for c in cache["chain"] if c.option_type == right
                  and abs(c.strike - strike) < 1e-6), None)
        return _usable_mid(c) if c else None
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
    return _sane_spread_value(value, width)


def _sane_spread_value(raw: float, width: float) -> Optional[float]:
    """Clamp a spread value into [0, width] — but REFUSE a value that was never
    plausible in the first place.

    A vertical spread cannot be worth less than 0 or more than its width; those are
    arbitrage bounds, not preferences. So a raw value outside them means the QUOTES
    are bad, not that the spread is extreme. Silently clamping hides that: it turns
    garbage into a confident-looking price at exactly the boundary.

    This is not hypothetical. On 2026-07-30, after hours, stale quotes on a 1.00-wide
    SPY debit spread produced a raw value >= 1.00, which clamped to exactly the width
    — the maximum possible — and read as "profit target hit, +$69". The system placed
    a close at a $1.00 limit on a spread actually worth 0.08. The order never filled,
    and the position was frozen for five days. An unusable mark must be None (hold),
    never a number at the boundary.

    A small tolerance is allowed because bid/ask noise on two legs can legitimately
    overshoot by a cent or two near expiry."""
    tol = max(0.02, 0.05 * width)
    if raw < -tol or raw > width + tol:
        return None
    return round(max(0.0, min(width, raw)), 4)


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


def _closing_order(pos: dict, value_ps: float, attempt: int = 0) -> OrderRequest:
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

    # ── ESCALATION LADDER (the chasing-stop fix) ────────────────────────────
    # A mid-price DAY limit re-priced once per cycle can chase a falling market
    # forever: each cycle honestly re-prices, never crosses, and the exit the
    # stop-loss ordered never happens. Each failed attempt therefore concedes
    # 15% more of the price (capped at 3 steps = 45%): when SELLING to close a
    # debit spread we accept less; when PAYING to close a credit spread we offer
    # more, capped at the width (the max the spread can be worth). Floor $0.01 —
    # a $0.00 combo limit is 'give it away at any price' and brokers commonly
    # reject it; one tick is the honest minimum. The BOOKED exit value stays the
    # mark (decision.exit_value_ps); only the working limit escalates, and the
    # realized fill is captured separately, so P&L never inherits the concession
    # as an assumption.
    esc = 0.15 * min(max(0, int(attempt)), 3)
    width = float(pos.get("width") or 0.0)
    if is_debit_close:                            # we RECEIVE: concede downward
        limit = round(max(0.01, value_ps * (1.0 - esc)), 2)
    else:                                         # we PAY: concede upward, <= width
        limit = round(value_ps * (1.0 + esc), 2)
        if width > 0:
            limit = min(limit, round(width, 2))
        limit = max(0.01, limit)

    est_credit = round(max(0.0, value_ps) * 100, 2) if is_debit_close else 0.0
    return OrderRequest(
        client_order_id=f"CLOSE-{pos['client_order_id']}",
        legs=legs, order_type="limit", limit_price=limit,
        strategy=f"close_{pos.get('structure', 'vertical')}", est_credit=est_credit,
        max_loss=0.0, underlying=pos["underlying"],
    )


def _is_filled(result, contracts: int) -> bool:
    """A broker-reported terminal 'filled' status IS the fill signal.

    We deliberately do NOT also require filled_qty > 0. IB reports status='Filled'
    with filled=0.0 whenever an order is reconstructed from completedOrders in a
    NEW process — which is exactly our deployment (one cron process per run). The
    old `and filled_qty > 0` meant a real fill was never recognised: the entry
    stayed 'pending' forever (invisible to management, never exited, yet still
    consuming a concurrency slot and heat budget), and a filled CLOSE left a
    phantom position the account no longer held. Trust the terminal status."""
    return (result.status or "").lower() == "filled"


def _close_leg_state(store: Store, pid: int, held: dict) -> str:
    """Where do THIS position's legs stand at the broker: 'all_held' | 'none_held'
    | 'partial' | 'unknown'.

    The predecessor checked whether the broker held ANY leg of ANY position — it
    only worked because the book held exactly one position. With a second position,
    a filled close would read as "did not fill" (someone else's legs are held),
    releasing it and placing a duplicate close for legs the account no longer
    holds. Three independent reviewers found this within hours of it shipping.

    'partial' matters on its own: it is the assignment/partial-close signature (one
    leg gone, one alive) and must be neither booked as a fill nor blindly re-closed
    — a lone surviving leg is not the structure we priced."""
    import json as _json
    from .reconcile import _leg_key
    row = store.conn.execute("SELECT * FROM positions WHERE id = ?", (int(pid),)).fetchone()
    if row is None:
        return "unknown"
    p = dict(row)
    n = int(p.get("contracts") or 0)
    u, exp = p.get("underlying"), p.get("expiration")
    want: list = []
    if p.get("structure") == "iron_condor":
        raw = p.get("legs_json")
        if not raw:
            return "unknown"
        j = _json.loads(raw)
        want = [(_leg_key(u, exp, j["sp"], "P"), -n), (_leg_key(u, exp, j["lp"], "P"), +n),
                (_leg_key(u, exp, j["sc"], "C"), -n), (_leg_key(u, exp, j["lc"], "C"), +n)]
    else:
        fam = p.get("family") or ("call" if "call" in (p.get("structure") or "") else "put")
        r = "C" if fam == "call" else "P"
        want = [(_leg_key(u, exp, p["short_strike"], r), -n),
                (_leg_key(u, exp, p["long_strike"], r), +n)]
    if not want or n <= 0:
        return "unknown"

    # Count COMPLETE structures held, plus whether any leg survives at all. The
    # difference is what distinguishes a partially-filled CLOSE from an
    # ASSIGNMENT, and the predecessor collapsed both into the wrong answer: it
    # only asked "is the FULL quantity present per leg", so 3-of-5 contracts
    # still held scored zero on both legs and returned 'none_held' — booking a
    # complete close and ORPHANING three real contracts at the broker.
    # Observed live 2026-08-18 (reconcile logged broker_qty 3 vs expected 5); it
    # filled the rest before the vulnerable path ran, so the orphan never
    # happened. Same species as the entry-side partial-fill orphan.
    units = None
    any_leg = False
    for key, qty in want:
        have = float((held or {}).get(key, 0.0))
        if abs(have) > 1e-9 and (have > 0) == (qty > 0):
            any_leg = True                       # a leg in the RIGHT direction
        per_unit = qty / n                       # +1 or -1 per structure
        u_leg = int(max(0.0, -have if per_unit < 0 else have))
        units = u_leg if units is None else min(units, u_leg)
    units = int(units or 0)

    if units >= n:
        return "all_held"
    if units > 0:
        return "partial_units"                   # a partly-filled close: N-units gone
    if not any_leg:
        return "none_held"
    return "partial"                             # lopsided legs: assignment signature


def _held_close_units(store: Store, pid: int, held: dict) -> int:
    """How many COMPLETE units of this position the broker still holds — the
    size a partly-filled close leaves behind."""
    import json as _json
    from .reconcile import _leg_key
    row = store.conn.execute("SELECT * FROM positions WHERE id = ?", (int(pid),)).fetchone()
    if row is None:
        return 0
    p = dict(row)
    n = int(p.get("contracts") or 0)
    if n <= 0:
        return 0
    u, exp = p.get("underlying"), p.get("expiration")
    if p.get("structure") == "iron_condor":
        raw = p.get("legs_json")
        if not raw:
            return 0
        j = _json.loads(raw)
        want = [(_leg_key(u, exp, j["sp"], "P"), -n), (_leg_key(u, exp, j["lp"], "P"), +n),
                (_leg_key(u, exp, j["sc"], "C"), -n), (_leg_key(u, exp, j["lc"], "C"), +n)]
    else:
        fam = p.get("family") or ("call" if "call" in (p.get("structure") or "") else "put")
        r = "C" if fam == "call" else "P"
        want = [(_leg_key(u, exp, p["short_strike"], r), -n),
                (_leg_key(u, exp, p["long_strike"], r), +n)]
    units = None
    for key, qty in want:
        have = float((held or {}).get(key, 0.0))
        per_unit = qty / n
        u_leg = int(max(0.0, -have if per_unit < 0 else have))
        units = u_leg if units is None else min(units, u_leg)
    return int(units or 0)


def _close_attempts(store: Store, pos_id: int) -> int:
    """How many times a close has already been placed for this position.

    Counted from the journal (close_pending is written at every placement)
    because the orders table keys on client_order_id and INSERT OR REPLACEs —
    re-placements are invisible there. Drives the escalation ladder below."""
    try:
        return int(store.conn.execute(
            "SELECT COUNT(*) FROM journal WHERE kind='close_pending' "
            "AND json_extract(payload,'$.position_id') = ?", (int(pos_id),)
        ).fetchone()[0] or 0)
    except Exception:
        return 0


def _pnl_at(pos: dict, value_ps: float) -> float:
    """Realized P&L if this position exits at `value_ps` (per share)."""
    n = int(pos.get("contracts") or 0)
    entry = float(pos.get("entry_credit_ps") or 0.0)
    if bool(pos.get("is_credit", 1)):
        return round((entry - value_ps) * 100 * n, 2)
    return round((value_ps - entry) * 100 * n, 2)


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
    # A broker that cannot testify must not resolve anything. When the IB connect
    # fails, the factory silently falls back to SIM — whose empty book would read
    # as "all legs gone => every pending close FILLED", fabricating realized P&L on
    # the live store from one bad connect. (Reconcile already no-ops on sim for the
    # same reason.)
    if getattr(adapter, "name", "") == "sim":
        return pend
    try:
        bro = {o.client_order_id: o for o in adapter.list_orders()}
    except Exception:
        return pend  # can't confirm -> leave pending, the position stays tracked
    # Broker POSITIONS are durable across sessions; the order feed is not.
    try:
        from .reconcile import broker_legs
        held = broker_legs(adapter)
    except Exception:
        held = None

    changed = False
    for pid, info in list(pend.items()):
        o = bro.get(info["coid"])
        if o is None:
            # The close order has VANISHED from the broker (a DAY order that
            # expired unfilled, or a session roll). Skipping meant the position was
            # frozen in close_pending FOREVER: never marked, never re-evaluated,
            # never exited — observed live, frozen for five days while its real
            # value moved from a fabricated profit to a genuine stop-loss.
            # Resolve against positions instead.
            if held is None:
                continue                       # genuinely cannot confirm
            if not held:
                # The broker book is COMPLETELY empty while we track an open
                # position. That is the signature of a paper-account reset (or a
                # broken positions feed), not of a routine fill — booking "all my
                # closes filled at their intended prices" off it would fabricate
                # P&L with zero alarms. Refuse to resolve; scream instead.
                store.append(_now_iso(), "broker_book_empty", {
                    "position_id": int(pid), "severity": "critical",
                    "note": "broker reports NO option positions while we track an "
                            "open one — possible paper-account reset. Not resolving "
                            "pending closes; investigate."})
                continue
            state = _close_leg_state(store, int(pid), held)
            if state == "all_held":
                # Still fully held => the close did NOT fill. Best-effort cancel of
                # the old broker order first (it may be live but absent from our
                # feed snapshot — a surviving twin would sell legs twice), then
                # release so the position is re-priced from scratch next pass.
                try:
                    row = store.conn.execute(
                        "SELECT broker_order_id FROM orders WHERE client_order_id = ?",
                        (info["coid"],)).fetchone()
                    if row and row["broker_order_id"]:
                        adapter.cancel_order(str(row["broker_order_id"]))
                except Exception:
                    pass
                del pend[pid]; changed = True
                store.set_order_status(info["coid"], "canceled")
                store.append(_now_iso(), "close_expired", {
                    "position_id": int(pid), "coid": info["coid"],
                    "note": "close order gone from broker but legs still held — "
                            "releasing so the position is re-evaluated"})
            elif state == "partial_units":
                # The close filled PART of the position and then vanished from
                # the feed. The remainder is a real, complete, defined-risk
                # structure — resize to it and keep managing, exactly as the
                # entry side adopts a partial fill. Booking a full close here
                # would orphan the remainder; booking the filled part needs a
                # fill price we do not have, so it is journaled rather than
                # invented.
                units = _held_close_units(store, int(pid), held)
                was = int((store.conn.execute(
                    "SELECT contracts FROM positions WHERE id = ?", (int(pid),)
                ).fetchone() or {"contracts": 0})["contracts"] or 0)
                store.adopt_partial_fill(int(pid), units)
                del pend[pid]; changed = True
                store.set_order_status(info["coid"], "canceled")
                store.append(_now_iso(), "partial_close_adopted", {
                    "position_id": int(pid), "was": was, "remaining": units,
                    "severity": "high",
                    "note": "close filled partially then vanished from the feed; "
                            "resized to the units still held so the remainder stays "
                            "managed. The closed portion has no recoverable fill "
                            "price and is NOT booked as realized P&L."})
            elif state == "none_held":
                # THIS position's legs are gone (others may remain) => the close
                # filled and the feed forgot. Price is the INTENDED one, unverified.
                store.close_position(int(pid), asof, _now_iso(), info["reason"],
                                     info["exit_value_ps"], info["realized_pnl"])
                store.append(_now_iso(), "position_closed", {
                    "position_id": int(pid), "reason": info["reason"],
                    "realized_pnl": info["realized_pnl"], "via": "positions_confirm",
                    "note": "fill price unverified (intended values; feed had no fill)"})
                del pend[pid]; changed = True
            else:
                # 'partial' / 'unknown': one leg alive, one gone — the signature of
                # ASSIGNMENT or a partial close. Neither a fill nor a release: a
                # lone surviving leg is not the structure we priced, and blindly
                # re-closing would trade legs we no longer hold. Needs a human.
                _row = store.conn.execute(
                    "SELECT underlying FROM positions WHERE id = ?",
                    (int(pid),)).fetchone()
                if _row:
                    store.freeze_underlying(
                        _row["underlying"],
                        "pending close partially held at broker — possible assignment")
                store.append(_now_iso(), "close_ambiguous_assignment", {
                    "position_id": int(pid), "coid": info["coid"], "state": state,
                    "severity": "high",
                    "note": "this position's legs are PARTIALLY held at the broker "
                            "— possible assignment/partial close. Holding pending; "
                            "investigate before trading this underlying."})
            continue
        st = (o.status or "").lower()   # NEEDED by the dead-order branch below
        if _is_filled(o, 1):
            store.close_position(int(pid), asof, _now_iso(), info["reason"],
                                 info["exit_value_ps"], info["realized_pnl"])
            # Capture realized exit economics NOW — this feed is session-scoped.
            _ic = bool((store.conn.execute(
                "SELECT is_credit FROM positions WHERE id = ?", (int(pid),)
            ).fetchone() or {"is_credit": 1})["is_credit"])
            store.record_exit_fill(
                int(pid), signed_credit_ps(o.filled_avg_price),
                exit_slippage_ps(info["exit_value_ps"], o.filled_avg_price, _ic))
            store.append(_now_iso(), "position_closed", {
                "position_id": int(pid), "reason": info["reason"],
                "realized_pnl": info["realized_pnl"], "via": "pending_fill",
                "exit_slip_ps": exit_slippage_ps(info["exit_value_ps"],
                                                 o.filled_avg_price, _ic)})
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

    # The broker's leg map, fetched ONCE per pass. Every order this pass places
    # is checked against it: a close must never blindly reverse legs the broker
    # does not confirm holding — buying back an assigned-away short OPENS a new
    # position instead of closing one. None => broker unverifiable => proceed
    # without the check (you must always be able to exit; we only block on
    # POSITIVE evidence of divergence, never on ignorance).
    held_map = None
    if getattr(adapter, "name", "") != "sim" and open_positions:
        try:
            from .reconcile import broker_legs
            held_map = broker_legs(adapter)
        except Exception:
            held_map = None
    frozen_map = store.frozen_underlyings()

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
        if pos["underlying"] in frozen_map:
            # Assignment-review freeze: total lockdown on this name until the
            # operator clears it. No marks, no orders — eyes first.
            summary["held"].append({"underlying": pos["underlying"],
                                    "reason": "frozen_operator_review"})
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

        # ── EXPIRY REALISM ──────────────────────────────────────────────────
        # 'expire' used to book the position closed at the MORNING spot — pure
        # local fiction: the real options trade until 16:00 and OCC then
        # auto-exercises anything ITM by $0.01, potentially delivering ±100
        # shares over a weekend (pin risk lands exactly between the strikes).
        # Against a real broker we only book what the broker confirms:
        #   still holds the legs -> place a REAL close now (get out, don't
        #     pretend), via the normal pending/fill machinery;
        #   legs gone -> the contracts are truly finished; book locally
        #     (any shares they left behind trip the assignment freeze);
        #   can't tell -> hold and say so; never fabricate a settlement.
        if decision.action == "expire" and getattr(adapter, "name", "") != "sim":
            state = (_close_leg_state(store, pos["id"], held_map)
                     if held_map is not None else "unknown")
            if state == "all_held":
                decision.action, decision.reason = "close", "expiry_close"
                decision.exit_value_ps = value_ps
                decision.realized_pnl = _pnl_at(pos, value_ps)
            elif state != "none_held":
                store.append(_now_iso(), "expiry_unverified", {
                    "position_id": pos["id"], "underlying": pos["underlying"],
                    "state": state, "severity": "high",
                    "note": "expiration reached but broker holdings unverifiable/"
                            "partial — refusing to fabricate a settlement"})
                summary["held"].append({"underlying": pos["underlying"],
                                        "reason": "expiry_unverified"})
                continue

        # ── EX-DIVIDEND ASSIGNMENT RISK ─────────────────────────────────────
        # A short ITM call within days of an (estimated) ex-div is a near-certain
        # early assignment: shares + the dividend liability, overnight. Exit
        # deliberately before the trap springs. Puts never trigger this.
        if decision.action == "hold" and spot:
            exd = exdiv_risk(pos, spot, asof)
            if exd:
                decision.action = "close"
                decision.reason = "exdiv_assignment_risk"
                decision.exit_value_ps = value_ps
                decision.realized_pnl = _pnl_at(pos, value_ps)
                store.append(_now_iso(), "exdiv_risk_exit", {
                    "position_id": pos["id"], "underlying": pos["underlying"],
                    "estimated_ex_div": exd,
                    "note": "short ITM call near estimated ex-div; closing early "
                            "rather than risking assignment + dividend liability"})

        # Persist the valuation for the read-only dashboard on EVERY pass, not just
        # on hold: a position with a close in flight was showing a blank mark, which
        # is exactly when you most want to see what it is worth.
        store.record_mark(pos["id"], value_ps, round(decision.realized_pnl, 2), _now_iso())

        if decision.action == "hold":
            summary["unrealized_open"] += decision.realized_pnl  # here = unrealized mark
            summary["held"].append({
                "underlying": pos["underlying"],
                "dte": dte_from(pos["expiration"], asof),
                "value_ps": value_ps, "unrealized": decision.realized_pnl})
            continue

        # Close (or settle at expiry). Expiry needs no order; a managed close does.
        if decision.action == "close":
            # NEVER blindly reverse legs the broker does not confirm holding.
            # none_held/partial here is the assignment/divergence signature: a
            # buy-to-close for an assigned-away short would OPEN a new long.
            # Freeze the name and wait for a human. (held_map None = broker
            # unverifiable = proceed; we block on evidence, not ignorance.)
            if getattr(adapter, "name", "") != "sim" and held_map is not None:
                lstate = _close_leg_state(store, pos["id"], held_map)
                if lstate in ("none_held", "partial"):
                    store.freeze_underlying(
                        pos["underlying"],
                        f"close blocked: legs {lstate} at broker — possible assignment")
                    store.append(_now_iso(), "close_blocked_legs_diverged", {
                        "position_id": pos["id"], "underlying": pos["underlying"],
                        "state": lstate, "severity": "critical",
                        "note": "refusing to place a close for legs the broker does "
                                "not confirm; underlying frozen for operator review"})
                    summary["held"].append({"underlying": pos["underlying"],
                                            "reason": "assignment_review"})
                    continue
            attempt = _close_attempts(store, pos["id"])
            order = _closing_order(pos, decision.exit_value_ps, attempt)
            if attempt > 0:
                store.append(_now_iso(), "close_escalated", {
                    "position_id": pos["id"], "underlying": pos["underlying"],
                    "attempt": attempt, "limit": order.limit_price,
                    "mark": decision.exit_value_ps,
                    "note": "prior close(s) never filled; conceding price to get out"})
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
        # An immediate (same-call) fill — capture its realized economics too. An
        # expiry settlement has no order/fill, so `result` may not exist.
        if decision.action == "close":
            store.record_exit_fill(
                pos["id"], signed_credit_ps(result.filled_avg_price),
                exit_slippage_ps(decision.exit_value_ps, result.filled_avg_price,
                                 bool(pos.get("is_credit", 1))))
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
