"""
Assignment defense-in-depth — the Tier 1 scenario-hunt findings.

These lock the only failure family that can silently breach invariant #2
(defined-risk only): American-style exercise turning a spread into shares.
Three layers, each tested:
  1. DETECT   — reconcile reports stray equity as critical drift; the cycle
                freezes the underlying.
  2. REFUSE   — a close is never placed for legs the broker does not confirm
                (buying back an assigned-away short would OPEN a position).
  3. AVOID    — expiry books only what the broker confirms, and short ITM calls
                are exited before an (estimated) ex-dividend date.
All offline; broker behavior is injected.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import types
from pathlib import Path

from core.brokers.base import OrderResult, Position
from core.store import Store


# ─────────────────────────── ex-div estimator (pure) ───────────────────────────

def test_third_friday_math():
    from core.exdiv import third_friday
    assert third_friday(2026, 9) == dt.date(2026, 9, 18)
    assert third_friday(2026, 8) == dt.date(2026, 8, 21)
    assert third_friday(2027, 3) == dt.date(2027, 3, 19)


def test_next_estimated_ex_div_patterns():
    from core.exdiv import next_estimated_ex_div
    # SPY quarterly: from mid-July the next estimate is September's 3rd Friday.
    assert next_estimated_ex_div("SPY", dt.date(2026, 7, 15)) == dt.date(2026, 9, 18)
    # DIA monthly: from early August, August's 3rd Friday.
    assert next_estimated_ex_div("DIA", dt.date(2026, 8, 3)) == dt.date(2026, 8, 21)
    # On the estimated date itself, it still counts (assignment risk is NOW).
    assert next_estimated_ex_div("SPY", dt.date(2026, 9, 18)) == dt.date(2026, 9, 18)
    # Unknown symbol -> no estimate, no protection (documented behavior).
    assert next_estimated_ex_div("ZZZZ", dt.date(2026, 7, 15)) is None


def _call_credit(underlying="SPY", short=540.0, long_=545.0, exp="2026-10-30"):
    return {"structure": "call_credit_spread", "family": "call", "is_credit": 1,
            "underlying": underlying, "short_strike": short, "long_strike": long_,
            "expiration": exp, "contracts": 1, "entry_credit_ps": 1.0}


def test_exdiv_risk_fires_only_for_short_itm_calls_near_exdiv():
    from core.exdiv import exdiv_risk
    pos = _call_credit()
    # Short 540C, spot 545 (ITM), 3 days before Sep's estimated ex-div -> risk.
    assert exdiv_risk(pos, 545.0, "2026-09-15") == "2026-09-18"
    # Same but OTM (spot below the short strike) -> no exercise motive, no risk.
    assert exdiv_risk(pos, 535.0, "2026-09-15") is None
    # ITM but ex-div is a month away -> no risk yet.
    assert exdiv_risk(pos, 545.0, "2026-08-01") is None
    # ITM and near ex-div, but the position EXPIRES before it -> no risk.
    early = dict(pos, expiration="2026-09-17")
    assert exdiv_risk(early, 545.0, "2026-09-15") is None
    # Put structures never trigger (no dividend motive to exercise puts early).
    put = {"structure": "put_credit_spread", "family": "put", "is_credit": 1,
           "underlying": "SPY", "short_strike": 540.0, "long_strike": 539.0,
           "expiration": "2026-10-30", "contracts": 1}
    assert exdiv_risk(put, 500.0, "2026-09-15") is None


# ───────────────────────── layer 1: detect + freeze ─────────────────────────

def _shares(sym, qty):
    return Position(symbol=sym, qty=qty, avg_price=735.0, market_value=qty * 735.0,
                    asset_class="us_equity", underlying=sym)


def test_reconcile_flags_stray_equity_as_critical():
    """An assigned short leg becomes ±100 SHARES — an asset class the option-leg
    diff is structurally blind to. This account only ever trades option spreads,
    so ANY equity at the broker is unexplained by construction."""
    from core.reconcile import reconcile

    class A:
        name = "ibkr_paper"
        def get_positions(self):
            return [_shares("SPY", 100.0)]

    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "p.db")
        rep = reconcile(A(), st)
        st.close()
    eq = [d for d in rep["drift"] if d["kind"] == "untracked_equity_at_broker"]
    assert len(eq) == 1
    assert eq[0]["severity"] == "critical" and eq[0]["underlying"] == "SPY"
    assert rep["ok"] is False


def test_freeze_registry_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "p.db")
        assert st.frozen_underlyings() == {}
        st.freeze_underlying("spy", "test reason")
        assert "SPY" in st.frozen_underlyings()
        st.freeze_underlying("SPY", "different reason")   # idempotent, keeps first
        assert st.frozen_underlyings()["SPY"]["reason"] == "test reason"
        assert st.unfreeze_underlying("SPY") is True
        assert st.unfreeze_underlying("SPY") is False
        assert st.frozen_underlyings() == {}
        st.close()


# ─────────────── layers 2+3: the management pass, end to end ───────────────

def _seed_debit(store, underlying="SPY", exp="2026-08-31", contracts=1):
    return store.open_position({
        "client_order_id": "AIS-t", "strategy": "volatility_breakout",
        "structure": "put_debit_spread", "family": "put", "is_credit": 0,
        "underlying": underlying, "status": "open", "opened_asof": "2026-07-29",
        "opened_ts": "t", "expiration": exp, "contracts": contracts,
        "short_strike": 735.0, "long_strike": 736.0, "width": 1.0,
        "legs_json": "[]", "entry_credit_ps": 0.31, "max_loss": 31.0})


def _leg(strike, qty, exp="2026-08-31", right="P", sym="SPY"):
    return Position(symbol=sym, qty=qty, avg_price=100.0, market_value=0.0,
                    asset_class="option", underlying=sym, option_expiration=exp,
                    option_strike=strike, option_right=right)


class _Broker:
    """Injectable real-looking broker for manage_open_positions."""
    name = "ibkr_paper"

    def __init__(self, held, marks, spot=700.0):
        self._held, self._marks, self._spot = held, marks, spot
        self.placed = []

    def get_positions(self):
        return self._held

    def mark_option(self, u, exp, strike, right, asof=None):
        return self._marks.get(strike)

    def get_quote(self, symbol):
        return types.SimpleNamespace(mid=self._spot)

    def place_order(self, order):
        self.placed.append(order)
        return OrderResult(accepted=True, client_order_id=order.client_order_id,
                           broker_order_id="B1", status="Submitted", filled_qty=0)

    def list_orders(self):
        return []


def _manage(adapter, store, asof):
    from core.manage import manage_open_positions
    return manage_open_positions(adapter, store,
                                 types.SimpleNamespace(strategies={}), asof)


def test_close_blocked_when_broker_does_not_hold_the_legs():
    """The core refusal: value says stop-loss, but the broker holds NEITHER leg.
    A blind close would trade legs we don't hold; instead: no order, underlying
    frozen, critical journal."""
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "p.db")
        _seed_debit(st)
        # Marks give value 0.08 (deep stop-loss for a 0.31 debit). Broker book:
        # holds a DIFFERENT underlying's leg only (feed alive, our legs gone).
        b = _Broker(held=[_leg(600.0, -1, exp="2026-09-18", sym="QQQ")],
                    marks={736.0: 0.10, 735.0: 0.02})
        m = _manage(b, st, "2026-08-05")
        assert b.placed == [], "no close may be placed for unconfirmed legs"
        assert any(h.get("reason") == "assignment_review" for h in m["held"])
        assert "SPY" in st.frozen_underlyings()
        n = st.conn.execute("SELECT COUNT(*) FROM journal "
                            "WHERE kind='close_blocked_legs_diverged'").fetchone()[0]
        assert n == 1
        st.close()


def test_frozen_underlying_is_total_lockdown():
    """Once frozen, the management pass places NO orders for that name — even a
    screaming stop-loss waits for the operator."""
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "p.db")
        _seed_debit(st)
        st.freeze_underlying("SPY", "test freeze")
        b = _Broker(held=[_leg(735.0, -1), _leg(736.0, 1)],
                    marks={736.0: 0.10, 735.0: 0.02})
        m = _manage(b, st, "2026-08-05")
        assert b.placed == []
        assert any(h.get("reason") == "frozen_operator_review" for h in m["held"])
        st.close()


def test_expiry_places_a_real_close_when_broker_still_holds_legs():
    """Expiration morning, broker still holds the spread: the old code booked a
    settlement at the 10:00 spot while the real legs lived until 16:00 + OCC
    exercise. Now: place a REAL close and let the fill machinery finish it."""
    import json
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "p.db")
        pid = _seed_debit(st, exp="2026-08-31")
        b = _Broker(held=[_leg(735.0, -1), _leg(736.0, 1)],
                    marks={736.0: 2.40, 735.0: 2.31}, spot=735.5)
        _manage(b, st, "2026-08-31")            # expiration day
        assert len(b.placed) == 1 and b.placed[0].client_order_id.startswith("CLOSE-")
        assert len(st.get_closed_positions()) == 0, "must NOT book a local settlement"
        pend = json.loads(st.get_kv("pending_closes") or "{}")
        assert pend[str(pid)]["reason"] == "expiry_close"
        st.close()


def test_expiry_books_locally_only_when_broker_confirms_legs_gone():
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "p.db")
        _seed_debit(st, exp="2026-08-31")
        # Our legs are gone; ANOTHER name's leg proves the feed is alive.
        b = _Broker(held=[_leg(600.0, -1, exp="2026-09-18", sym="QQQ")],
                    marks={736.0: 1.00, 735.0: 0.50}, spot=735.5)
        _manage(b, st, "2026-08-31")
        assert b.placed == [], "contracts are finished; no order to place"
        assert len(st.get_closed_positions()) == 1
        st.close()


def test_short_itm_call_is_closed_before_estimated_exdiv():
    """A short ITM call 3 days from SPY's estimated ex-div would otherwise be a
    near-certain assignment + dividend liability. The hold decision is overridden
    into a deliberate early exit."""
    import json
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "p.db")
        pid = st.open_position({
            "client_order_id": "AIS-c", "strategy": "earnings_vol",
            "structure": "call_credit_spread", "family": "call", "is_credit": 1,
            "underlying": "SPY", "status": "open", "opened_asof": "2026-08-20",
            "opened_ts": "t", "expiration": "2026-10-30", "contracts": 1,
            "short_strike": 540.0, "long_strike": 545.0, "width": 5.0,
            "legs_json": "[]", "entry_credit_ps": 1.0, "max_loss": 400.0})
        # Value == entry (no profit/stop trigger), 45 DTE (no dte trigger),
        # spot 545 => short 540C is ITM, and asof is 3 days before 2026-09-18.
        b = _Broker(held=[_leg(540.0, -1, exp="2026-10-30", right="C"),
                          _leg(545.0, 1, exp="2026-10-30", right="C")],
                    marks={540.0: 8.0, 545.0: 7.0}, spot=545.0)
        _manage(b, st, "2026-09-15")
        assert len(b.placed) == 1, "must exit ahead of the ex-div trap"
        pend = json.loads(st.get_kv("pending_closes") or "{}")
        assert pend[str(pid)]["reason"] == "exdiv_assignment_risk"
        n = st.conn.execute("SELECT COUNT(*) FROM journal "
                            "WHERE kind='exdiv_risk_exit'").fetchone()[0]
        assert n == 1
        st.close()


def test_put_spread_never_triggers_exdiv_exit():
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "p.db")
        _seed_debit(st, exp="2026-10-30")       # put debit spread
        b = _Broker(held=[_leg(735.0, -1, exp="2026-10-30"),
                          _leg(736.0, 1, exp="2026-10-30")],
                    marks={736.0: 0.66, 735.0: 0.35}, spot=700.0)  # value=entry: hold
        m = _manage(b, st, "2026-09-15")        # 3 days before estimated ex-div
        assert b.placed == [], "puts carry no dividend-assignment risk"
        assert any(h.get("reason") != "frozen_operator_review" for h in m["held"])
        st.close()


def test_frozen_symbol_gets_no_new_entries():
    """The entry side of the freeze: a frozen underlying is skipped before any
    signal generation, with the reason visible in the decision trace."""
    from core.brokers.sim import SimAdapter
    from core.config import Config
    from core.execution import run_cycle
    from strategies.premium_harvest import PremiumHarvest

    cfg = Config(
        raw={}, mode="paper",
        account={"type": "margin", "region": "CA", "starting_equity_usd": 30000,
                 "options_approval_level": 3},
        brokers={"execution": "sim"},
        risk={"max_daily_new_positions": 20, "max_concurrent_positions": 20},
        strategies={"premium_harvest": {"min_iv_rank": 0.0}},
        watchlist=["SPY"])
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "p.db")
        st.freeze_underlying("SPY", "test")
        import json
        s = run_cycle(SimAdapter(asof="2026-01-15"),
                      [PremiumHarvest({"min_iv_rank": 0.0})], st, cfg,
                      asof="2026-01-15")
        assert s["signals"] == [] and s["placed"] == []
        dec = json.loads(st.get_kv("cycle_decisions") or "[]")
        assert dec and dec[0]["symbol"] == "SPY" and "FROZEN" in dec[0]["note"]
        st.close()
