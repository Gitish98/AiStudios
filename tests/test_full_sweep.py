"""Regression tests for the 4 cross-cutting bugs from the full-system sweep."""

import re
import tempfile
import types
from pathlib import Path

from core.brokers.base import OrderLeg, OrderRequest, OrderResult
from core.brokers.ibkr import build_order_plan
from core.brokers.sim import SimAdapter
from core.config import Config
from core.execution import run_cycle
from core.manage import _closing_order, manage_open_positions
from core.positions import order_to_position
from core.risk import _notional
from core.store import Store
from strategies.premium_harvest import PremiumHarvest


# ── #1: closing a DEBIT spread must submit as a CREDIT (negative IB price) ────

def _debit_position():
    o = OrderRequest(
        client_order_id="d",
        legs=[OrderLeg("C100", "buy", 1, "option", "call", strike=100, expiration="2026-02-19"),
              OrderLeg("C105", "sell", 1, "option", "call", strike=105, expiration="2026-02-19")],
        limit_price=1.00, strategy="call_debit_spread", est_credit=-100.0,
        max_loss=100.0, underlying="SPY")
    return order_to_position(o, "2026-01-15", "t")


def test_debit_close_is_priced_as_credit():
    closing = _closing_order(_debit_position(), value_ps=1.80)
    assert closing.est_credit > 0                     # closing a debit nets a credit
    plan = build_order_plan(closing)
    assert plan["limit_price"] < 0                     # -> negative IB combo price (credit)


def test_credit_close_stays_a_debit():
    # Closing a put credit spread is genuinely a debit -> positive price.
    o = OrderRequest(
        client_order_id="c",
        legs=[OrderLeg("P540", "sell", 1, "option", "put", strike=540, expiration="2026-02-19"),
              OrderLeg("P539", "buy", 1, "option", "put", strike=539, expiration="2026-02-19")],
        limit_price=0.26, strategy="put_credit_spread", est_credit=26.0,
        max_loss=74.0, underlying="SPY")
    closing = _closing_order(order_to_position(o, "2026-01-15", "t"), value_ps=0.13)
    assert closing.est_credit == 0.0
    assert build_order_plan(closing)["limit_price"] > 0


# ── #3: closing legs carry REAL OCC symbols, not fabricated placeholders ─────

def test_closing_legs_use_real_occ_symbols():
    closing = _closing_order(_debit_position(), value_ps=1.80)
    for leg in closing.legs:
        assert "_close_" not in leg.symbol
        assert re.match(r"^[A-Z]+\d{6}[CP]\d{8}$", leg.symbol), leg.symbol


def test_rejected_close_keeps_position_open():
    class RejectClose(SimAdapter):
        def place_order(self, order):
            if order.client_order_id.startswith("CLOSE-"):
                return OrderResult(accepted=False, client_order_id=order.client_order_id,
                                   status="rejected", reason="no liquidity")
            return super().place_order(order)

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        # A position 10 DTE from asof -> triggers dte_manage close.
        store.open_position({
            "client_order_id": "o", "strategy": "put_credit_spread",
            "structure": "put_credit_spread", "family": "put", "is_credit": 1,
            "underlying": "SPY", "status": "open", "opened_asof": "2026-01-15",
            "opened_ts": "t", "expiration": "2026-01-25", "contracts": 1,
            "short_strike": 540, "long_strike": 539, "width": 1, "legs_json": None,
            "entry_credit_ps": 0.26, "max_loss": 74})
        m = manage_open_positions(RejectClose(asof="2026-01-15"), store,
                                  types.SimpleNamespace(strategies={}), "2026-01-15")
        assert any(h.get("reason") == "close_rejected" for h in m["held"])
        assert len(store.get_open_positions()) == 1   # still open after a rejected close
        store.close()


# ── #2: buying power binds across multiple same-cycle orders ─────────────────

def test_buying_power_accumulates_within_cycle():
    class SmallBP(SimAdapter):
        def get_account(self):
            a = super().get_account()
            a.buying_power = 250.0     # room for ~2 spreads (~$100 collateral each)
            return a

    cfg = Config(
        raw={}, mode="paper",
        account={"type": "margin", "region": "CA", "starting_equity_usd": 30000,
                 "options_approval_level": 3},
        brokers={"execution": "sim"},
        risk={"per_position_notional_pct": 0.9, "per_trade_risk_pct": 0.9,
              "max_single_underlying_pct": 0.9, "max_gross_leverage": 50.0,
              "portfolio_heat_pct": 0.9, "max_daily_new_positions": 20,
              "max_concurrent_positions": 20},
        strategies={"premium_harvest": {"min_iv_rank": 0.0}},
        watchlist=["SPY", "QQQ", "IWM", "DIA", "XLK", "XLE"])
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        s = run_cycle(SmallBP(asof="2026-01-15"), [PremiumHarvest({"min_iv_rank": 0.0})],
                      store, cfg, asof="2026-01-15")
        # BP=$250 can't fund all the ~$100 spreads -> at least one rejected on BP.
        assert any("buying power" in r.lower()
                   for rej in s["rejected"] for r in rej["reasons"])
        store.close()


# ── #4: a debit vertical's notional is the single net debit, not 2×debit ─────

def test_closing_legs_carry_close_intent_on_alpaca():
    from core.brokers.alpaca import build_order_body
    closing = _closing_order(_debit_position(), value_ps=1.80)
    body = build_order_body(closing)
    intents = {leg["position_intent"] for leg in body["legs"]}
    # Every leg of a managed close must be *_to_close, never *_to_open.
    assert intents <= {"buy_to_close", "sell_to_close"}
    assert all("_to_open" not in i for i in intents)


def test_closing_legs_carry_close_intent_on_ibkr():
    plan = build_order_plan(_closing_order(_debit_position(), value_ps=1.80))
    assert all(leg["intent"] == "close" for leg in plan["legs"])


def test_zero_price_close_keeps_limit_price_on_both_adapters():
    from core.brokers.alpaca import build_order_body
    # A worthless spread closes at value 0.00 — the limit must still be present.
    closing = _closing_order(_debit_position(), value_ps=0.0)
    a = build_order_body(closing)
    assert a["type"] == "limit" and a["limit_price"] is not None   # not dropped
    p = build_order_plan(closing)
    assert p["order_type"] == "LMT" and p["limit_price"] is not None


def test_opening_orders_still_carry_open_intent():
    from core.brokers.alpaca import build_order_body
    opener = OrderRequest(
        client_order_id="o",
        legs=[OrderLeg("P540", "sell", 1, "option", "put", strike=540, expiration="2026-02-19"),
              OrderLeg("P539", "buy", 1, "option", "put", strike=539, expiration="2026-02-19")],
        limit_price=0.26, strategy="put_credit_spread", est_credit=26.0,
        max_loss=74.0, underlying="SPY")
    intents = {leg["position_intent"] for leg in build_order_body(opener)["legs"]}
    assert intents <= {"buy_to_open", "sell_to_open"}


def test_alpaca_option_position_parsed_for_reconcile():
    from core.brokers.alpaca import position_from_alpaca_row
    p = position_from_alpaca_row({
        "symbol": "SPY260718P00450000", "qty": "-1", "avg_entry_price": "1.20",
        "market_value": "-120", "asset_class": "us_option"})
    assert p.asset_class == "option"            # normalized, not 'us_option'
    assert p.option_strike == 450.0 and p.option_right == "P"
    assert p.option_expiration == "2026-07-18" and p.underlying == "SPY"
    # equity rows still pass through
    eq = position_from_alpaca_row({"symbol": "SPY", "qty": "10",
                                   "avg_entry_price": "550", "market_value": "5500",
                                   "asset_class": "us_equity"})
    assert eq.asset_class == "us_equity"


def test_managed_close_waits_for_fill_not_accept():
    """A close that is ACCEPTED but not yet FILLED must NOT mark the position
    closed — it stays open and tracked until the fill is confirmed."""
    from core.brokers.base import OrderResult

    class PreSubmitClose(SimAdapter):
        def place_order(self, order):
            if order.client_order_id.startswith("CLOSE-"):
                return OrderResult(accepted=True, client_order_id=order.client_order_id,
                                   broker_order_id="B1", status="PreSubmitted", filled_qty=0)
            return super().place_order(order)
        def list_orders(self):
            return []   # nothing filled yet

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        store.open_position({
            "client_order_id": "o", "strategy": "put_credit_spread",
            "structure": "put_credit_spread", "family": "put", "is_credit": 1,
            "underlying": "SPY", "status": "open", "opened_asof": "2026-01-15",
            "opened_ts": "t", "expiration": "2026-01-25", "contracts": 1,
            "short_strike": 540, "long_strike": 539, "width": 1, "legs_json": None,
            "entry_credit_ps": 0.26, "max_loss": 74})
        m = manage_open_positions(PreSubmitClose(asof="2026-01-15"), store,
                                  types.SimpleNamespace(strategies={}), "2026-01-15")
        assert any(h.get("reason") == "close_pending" for h in m["held"])
        assert len(store.get_open_positions()) == 1   # still open — close not yet filled
        # And it is NOT booked as realized P&L yet.
        assert store.realized_pnl_on("2026-01-15") == 0.0
        store.close()


def test_pending_close_finalizes_when_filled():
    from core.brokers.base import OrderResult

    class FillsLater(SimAdapter):
        def list_orders(self):
            return [OrderResult(accepted=True, client_order_id="CLOSE-o",
                                status="filled", filled_qty=2)]

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        store.open_position({
            "client_order_id": "o", "strategy": "put_credit_spread",
            "structure": "put_credit_spread", "family": "put", "is_credit": 1,
            "underlying": "SPY", "status": "open", "opened_asof": "2026-01-15",
            "opened_ts": "t", "expiration": "2026-02-19", "contracts": 1,
            "short_strike": 540, "long_strike": 539, "width": 1, "legs_json": None,
            "entry_credit_ps": 0.26, "max_loss": 74})
        pid = store.get_open_positions()[0]["id"]
        # Simulate a prior cycle having recorded a pending close.
        import json as _json
        store.set_kv("pending_closes", _json.dumps({str(pid): {
            "coid": "CLOSE-o", "reason": "profit_target",
            "exit_value_ps": 0.13, "realized_pnl": 13.0}}))
        manage_open_positions(FillsLater(asof="2026-02-25"), store,
                              types.SimpleNamespace(strategies={}), "2026-02-25")
        assert len(store.get_open_positions()) == 0           # now finalized closed
        assert store.realized_pnl_on("2026-02-25") == 13.0
        store.close()


def test_unfilled_entry_is_pending_not_open():
    """An accepted-but-unfilled entry is tracked 'pending' (counted for risk) but
    NOT 'open' (so reconcile won't expect it at the broker yet)."""
    from core.brokers.base import OrderResult

    class SubmitOnly(SimAdapter):
        def place_order(self, order):
            return OrderResult(accepted=True, client_order_id=order.client_order_id,
                               broker_order_id="B", status="Submitted", filled_qty=0)

    cfg = Config(
        raw={}, mode="paper",
        account={"type": "margin", "region": "CA", "starting_equity_usd": 30000,
                 "options_approval_level": 3},
        brokers={"execution": "sim"},
        risk={"per_position_notional_pct": 0.5, "per_trade_risk_pct": 0.5,
              "max_single_underlying_pct": 0.5, "max_gross_leverage": 50.0,
              "max_daily_new_positions": 20, "max_concurrent_positions": 20},
        strategies={"premium_harvest": {"min_iv_rank": 0.0}},
        watchlist=["SPY", "IWM"])
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        run_cycle(SubmitOnly(asof="2026-01-15"), [PremiumHarvest({"min_iv_rank": 0.0})],
                  store, cfg, asof="2026-01-15")
        assert len(store.get_open_positions()) == 0      # nothing filled
        assert len(store.get_pending_positions()) >= 1    # but working orders are tracked
        assert len(store.get_active_positions()) >= 1     # and counted as committed risk
        store.close()


def test_pending_entry_finalizes_on_fill_and_drops_on_reject():
    from core.brokers.base import OrderResult
    from core.execution import _finalize_pending_entries

    def _pending_pos(coid):
        return {"client_order_id": coid, "strategy": "put_credit_spread",
                "structure": "put_credit_spread", "family": "put", "is_credit": 1,
                "underlying": "SPY", "status": "pending", "opened_asof": "2026-01-15",
                "opened_ts": "t", "expiration": "2026-02-19", "contracts": 1,
                "short_strike": 540, "long_strike": 539, "width": 1, "legs_json": None,
                "entry_credit_ps": 0.26, "max_loss": 74}

    class Orders(SimAdapter):
        def list_orders(self):
            return [OrderResult(accepted=True, client_order_id="fill", status="filled", filled_qty=2),
                    OrderResult(accepted=True, client_order_id="dead", status="canceled", filled_qty=0)]

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        store.open_position(_pending_pos("fill"))
        store.open_position(_pending_pos("dead"))
        _finalize_pending_entries(Orders(asof="2026-01-15"), store)
        opens = store.get_open_positions()
        assert len(opens) == 1 and opens[0]["client_order_id"] == "fill"   # filled -> open
        assert len(store.get_pending_positions()) == 0                      # dead -> deleted
        store.close()


def test_capability_warnings_loud_on_real_adapter():
    import types as _t
    from core.execution import capability_warnings
    fake = _t.SimpleNamespace(name="ibkr_paper")   # lacks iv_history/get_earnings_date
    cfg = _t.SimpleNamespace(strategies={"premium_harvest": {}, "earnings_vol": {},
                                         "volatility_breakout": {}})
    warns = capability_warnings(fake, cfg)
    assert any(("IV-rank" in w or "bootstrap" in w.lower()) for w in warns)
    assert any("earnings" in w for w in warns)
    # sim never warns
    assert capability_warnings(SimAdapter(asof="2026-01-15"), cfg) == []


def test_sim_earnings_date_consistent_with_asof():
    sim = SimAdapter(asof="2026-01-15")
    from datetime import date
    for sym in ("SPY", "QQQ", "AAPL", "XLF"):
        d = sim.get_earnings_date(sym, asof="2026-03-10")
        if d:   # when present, it must be anchored to the PASSED asof, 1-9 days out
            delta = (date.fromisoformat(d) - date(2026, 3, 10)).days
            assert 1 <= delta <= 9
        # deterministic
        assert sim.get_earnings_date(sym, asof="2026-03-10") == d


def test_has_order_treats_ibkr_terminal_statuses_as_dead():
    from core.brokers.base import OrderResult
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")

        def rec(coid, status):
            o = OrderRequest(client_order_id=coid, legs=[], strategy="put_credit_spread",
                             underlying="SPY")
            store.record_order("2026-05-01T10:00:00", o,
                               OrderResult(accepted=True, client_order_id=coid, status=status))

        # IBKR-style terminal statuses (mixed case / British spelling) must NOT
        # block re-entry; live/working ones must.
        rec("dead1", "Cancelled")
        rec("dead2", "Inactive")
        rec("dead3", "ApiCancelled")
        rec("live1", "Submitted")
        rec("live2", "PreSubmitted")
        rec("filled1", "filled")
        assert store.has_order("dead1") is False
        assert store.has_order("dead2") is False
        assert store.has_order("dead3") is False
        assert store.has_order("live1") is True
        assert store.has_order("live2") is True
        assert store.has_order("filled1") is True
        store.close()


def test_dropped_entry_unblocks_same_day_reentry():
    """A broker-canceled entry: the finalizer must reconcile the orders table so
    the deterministic client_order_id can be re-entered the same day."""
    from core.brokers.base import OrderResult, OrderRequest as OR
    from core.execution import _finalize_pending_entries

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        coid = "AIS-deadentry"
        # Recorded at placement as 'Submitted' (would block re-entry)...
        store.record_order("2026-05-01T10:00:00",
                           OR(client_order_id=coid, legs=[], strategy="put_credit_spread",
                              underlying="SPY"),
                           OrderResult(accepted=True, client_order_id=coid, status="Submitted"))
        store.open_position({
            "client_order_id": coid, "strategy": "put_credit_spread",
            "structure": "put_credit_spread", "family": "put", "is_credit": 1,
            "underlying": "SPY", "status": "pending", "opened_asof": "2026-05-01",
            "opened_ts": "t", "expiration": "2026-06-19", "contracts": 1,
            "short_strike": 540, "long_strike": 539, "width": 1, "legs_json": None,
            "entry_credit_ps": 0.26, "max_loss": 74})
        assert store.has_order(coid) is True   # blocked before finalize

        class Canceled(SimAdapter):
            def list_orders(self):
                return [OrderResult(accepted=True, client_order_id=coid,
                                    status="Cancelled", filled_qty=0)]
        _finalize_pending_entries(Canceled(asof="2026-05-01"), store)
        assert len(store.get_pending_positions()) == 0   # position dropped
        assert store.has_order(coid) is False             # AND re-entry unblocked
        store.close()


def test_debit_vertical_notional_not_double_counted():
    o = OrderRequest(
        client_order_id="d",
        legs=[OrderLeg("C100", "buy", 1, "option", "call", strike=100),
              OrderLeg("C105", "sell", 1, "option", "call", strike=105)],
        limit_price=1.00, strategy="call_debit_spread", est_credit=-100.0,
        max_loss=100.0, underlying="SPY")
    assert _notional(o) == 100.0      # net debit ×100, NOT 2×100×1.00 = 200


def test_pending_close_handles_a_live_unfilled_order_without_crashing():
    """REGRESSION (critical): _finalize_pending_closes referenced `st` after the
    assignment was removed, so the FIRST time a close order was seen alive-but-
    unfilled — the NORMAL case for a limit close — it raised NameError, which
    escaped manage -> run_cycle -> cmd_run_cycle and aborted the whole cycle.
    The bot would be bricked holding a live short-premium position it could never
    manage or exit, with a nonzero exit code as the only symptom."""
    import json, tempfile
    from pathlib import Path
    from core.manage import _finalize_pending_closes
    from core.store import Store

    class _O:
        def __init__(self, status):
            self.client_order_id = "CLOSE-x"
            self.status = status
            self.filled_qty = 0.0
            self.filled_avg_price = 0.0

    class _A:
        def __init__(self, status):
            self._s = status
        def list_orders(self):
            return [_O(self._s)]

    for status in ("Submitted", "PreSubmitted", "Cancelled", "Inactive"):
        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "p.db")
            store.set_kv("pending_closes", json.dumps({
                "1": {"coid": "CLOSE-x", "reason": "profit_target",
                      "exit_value_ps": 0.10, "realized_pnl": 50.0}}))
            # Must not raise for ANY status — the bug hit every non-filled branch.
            pend = _finalize_pending_closes(_A(status), store, "2026-07-29")
            if status in ("Cancelled", "Inactive"):
                assert "1" not in pend, f"{status} should clear the pending entry"
            else:
                assert "1" in pend, f"{status} should stay pending"
            store.close()


def test_missing_quote_yields_no_mark_not_a_fabricated_zero():
    """REGRESSION (critical): IB sends NaN for an absent quote and the adapter
    collapses NaN to 0.0, so a quote-less contract looked like bid=ask=last=0 and
    OptionContract.mid returned 0.0. For a SHORT credit spread a 0.0 mark reads as
    MAXIMUM PROFIT and fires the profit-target exit — closing a live position at a
    fabricated price on missing data. Absence must be None."""
    from core.brokers.base import OptionContract
    from core.manage import _usable_mid, mark_spread_value_ps

    def _c(strike, bid, ask, last=0.0):
        return OptionContract(symbol="X", underlying="QQQ", expiration="2026-08-28",
                              strike=strike, option_type="put", bid=bid, ask=ask,
                              last=last, dte=30)

    assert _usable_mid(_c(645, 0.0, 0.0)) is None      # no data at all
    assert _usable_mid(_c(645, 1.0, 1.2)) == 1.1       # real quote
    assert _usable_mid(_c(645, 0.0, 0.0, 0.95)) == 0.95  # last-only fallback

    class _A:
        name = "fake"
        def get_option_chain(self, u, exp=None):
            return [_c(645, 0.0, 0.0), _c(640, 0.0, 0.0)]   # both quote-less

    pos = {"underlying": "QQQ", "expiration": "2026-08-28", "structure": "put_credit_spread",
           "family": "put", "short_strike": 645.0, "long_strike": 640.0, "contracts": 1}
    # Must be None (hold), NOT 0.0 (which evaluate_exit would read as max profit).
    assert mark_spread_value_ps(_A(), pos) is None


def test_stranded_pending_entry_is_resolved_against_broker_positions():
    """REGRESSION (critical): with one cron process per run, an order that filled
    between cycles is simply ABSENT from the session-scoped order feed. Treating
    that as 'still working' stranded the row as immortally pending — never managed,
    never exited, still consuming risk budget, with a real position live at the
    broker. Resolve against POSITIONS, which are repopulated on every connect."""
    import tempfile
    from pathlib import Path
    from core.brokers.base import Position
    from core.execution import _finalize_pending_entries
    from core.store import Store

    def _row(store, asof):
        return store.open_position({
            "client_order_id": "AIS-x", "strategy": "premium_harvest",
            "structure": "put_credit_spread", "family": "put", "is_credit": 1,
            "underlying": "QQQ", "status": "pending", "opened_asof": asof,
            "opened_ts": asof + "T14:00:00+00:00", "expiration": "2026-08-28",
            "contracts": 1, "short_strike": 645.0, "long_strike": 640.0, "width": 5.0,
            "legs_json": "[]", "entry_credit_ps": 0.92, "max_loss": 408.0})

    def _leg(strike, qty):
        return Position(symbol="QQQ", qty=qty, avg_price=0.0, market_value=0.0,
                        asset_class="option", underlying="QQQ",
                        option_expiration="2026-08-28", option_strike=strike,
                        option_right="P")

    class _A:
        name = "ibkr_paper"
        def __init__(self, held): self._held = held
        def list_orders(self): return []          # order feed has rolled over
        def get_positions(self): return self._held

    # (a) Broker HOLDS both legs -> the entry filled: promote to open.
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "p.db")
        pid = _row(st, "2026-07-01")
        _finalize_pending_entries(_A([_leg(645.0, -1), _leg(640.0, +1)]), st)
        assert [p["id"] for p in st.get_open_positions()] == [pid]
        st.close()

    # (b) Broker holds NOTHING and the row is from a previous day -> the DAY order
    #     died unfilled: drop it so it stops consuming risk budget forever.
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "p.db")
        _row(st, "2026-07-01")
        _finalize_pending_entries(_A([]), st)
        assert st.get_active_positions() == []
        st.close()

    # (c) Only ONE leg held -> a PARTIAL fill. Must NOT be promoted (it is not a
    #     complete defined-risk structure) and must NOT be deleted (the broker
    #     holds real, possibly unhedged exposure). Stay pending + flag loudly.
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "p.db")
        _row(st, "2026-07-01")
        _finalize_pending_entries(_A([_leg(645.0, -1)]), st)
        assert len(st.get_pending_positions()) == 1, "partial fill must not be deleted"
        assert st.get_open_positions() == [], "partial fill must not be promoted"
        flagged = st.conn.execute(
            "SELECT COUNT(*) FROM journal WHERE kind='partial_fill_detected'").fetchone()[0]
        assert flagged == 1, "a partial fill must be flagged for a human"
        st.close()


def test_partial_fill_is_adopted_not_deleted_when_the_order_dies():
    """REGRESSION from the FIRST REAL TRADE (2026-07-29).

    We ordered a 10-contract SPY 736/735 put debit spread. ONE contract filled;
    the remainder expired with the DAY order, and IB reported the order as
    Cancelled with filled_qty=0.0 while still holding the 1-contract spread.

    The old code saw a dead status and DELETED the position row — which would have
    left a real, live position at the broker with nothing in the system tracking
    it: never managed, never exited, riding to expiry. Adopt broker truth instead,
    rescaling contracts and max_loss to what actually filled."""
    import tempfile
    from pathlib import Path
    from core.brokers.base import Position
    from core.execution import _finalize_pending_entries, _held_units
    from core.store import Store

    def _row(store):
        return store.open_position({
            "client_order_id": "AIS-44f1", "strategy": "volatility_breakout",
            "structure": "put_debit_spread", "family": "put", "is_credit": 0,
            "underlying": "SPY", "status": "pending", "opened_asof": "2026-07-29",
            "opened_ts": "2026-07-29T14:01:32+00:00", "expiration": "2026-08-31",
            "contracts": 10, "short_strike": 735.0, "long_strike": 736.0, "width": 1.0,
            "legs_json": "[]", "entry_credit_ps": 0.31, "max_loss": 310.0})

    def _leg(strike, qty):
        return Position(symbol="SPY", qty=qty, avg_price=0.0, market_value=0.0,
                        asset_class="option", underlying="SPY",
                        option_expiration="2026-08-31", option_strike=strike,
                        option_right="P")

    class _O:
        client_order_id = "AIS-44f1"
        status = "Cancelled"          # exactly what IB reported
        filled_qty = 0.0              # ...even though 1 contract DID fill
        filled_avg_price = 0.0

    class _A:
        name = "ibkr_paper"
        def __init__(self, held): self._held = held
        def list_orders(self): return [_O()]
        def get_positions(self): return self._held

    # The real broker state: 1 complete spread held out of 10 ordered.
    held_legs = [_leg(735.0, -1.0), _leg(736.0, +1.0)]
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "p.db")
        pid = _row(st)
        _finalize_pending_entries(_A(held_legs), st)
        openp = st.get_open_positions()
        assert len(openp) == 1, "a partially-filled position must NOT be deleted"
        assert openp[0]["id"] == pid
        assert openp[0]["contracts"] == 1, "must adopt the ACTUAL filled size"
        assert openp[0]["max_loss"] == 31.0, "max_loss must rescale with size"
        flagged = st.conn.execute(
            "SELECT COUNT(*) FROM journal WHERE kind='partial_fill_adopted'").fetchone()[0]
        assert flagged == 1
        st.close()

    # Nothing held at the broker -> the order truly died unfilled -> delete.
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "p.db")
        _row(st)
        _finalize_pending_entries(_A([]), st)
        assert st.get_active_positions() == []
        st.close()

    # _held_units counts COMPLETE structures, not raw legs.
    row = {"structure": "put_debit_spread", "family": "put", "underlying": "SPY",
           "expiration": "2026-08-31", "contracts": 10,
           "short_strike": 735.0, "long_strike": 736.0}
    from core.reconcile import _leg_key
    held = {_leg_key("SPY", "2026-08-31", 735.0, "P"): -3.0,
            _leg_key("SPY", "2026-08-31", 736.0, "P"): +2.0}
    assert _held_units(row, held) == 2, "limited by the smaller leg"


def test_impossible_spread_value_is_rejected_not_clamped():
    """REGRESSION from a LIVE five-day freeze (2026-07-30).

    Stale after-hours quotes on a 1.00-wide SPY debit spread produced a raw value
    >= 1.00. The old code clamped it to exactly the width — the maximum possible —
    which read as 'profit target hit, +$69'. The system then placed a close at a
    $1.00 limit on a spread actually worth 0.08.

    A vertical cannot be worth <0 or >width; those are arbitrage bounds. A value
    outside them means the QUOTES are bad, and clamping turns garbage into a
    confident price at the boundary. It must be None (hold) instead."""
    from core.manage import _sane_spread_value

    assert _sane_spread_value(0.08, 1.0) == 0.08        # ordinary
    assert _sane_spread_value(1.0, 1.0) == 1.0          # legitimately at max
    assert _sane_spread_value(1.03, 1.0) == 1.0         # bid/ask noise -> clamp
    assert _sane_spread_value(1.5, 1.0) is None         # impossible -> BAD DATA
    assert _sane_spread_value(-0.5, 1.0) is None
    # Wider spreads get a proportionally wider tolerance.
    assert _sane_spread_value(5.2, 5.0) == 5.0
    assert _sane_spread_value(7.0, 5.0) is None


def test_vanished_close_order_releases_the_position():
    """REGRESSION from the same live freeze. The close was a DAY order; it expired
    unfilled and vanished from the broker feed entirely. `_finalize_pending_closes`
    did `order not found -> continue`, so the position stayed in close_pending
    FOREVER: skipped by management every cycle, never marked, never re-evaluated,
    never exited — while its real value drifted into a genuine stop-loss it could
    not act on. Resolve against POSITIONS, which are durable across sessions."""
    import json, tempfile
    from pathlib import Path
    from core.brokers.base import Position
    from core.manage import _finalize_pending_closes
    from core.store import Store

    def _seed(store):
        pid = store.open_position({
            "client_order_id": "AIS-x", "strategy": "volatility_breakout",
            "structure": "put_debit_spread", "family": "put", "is_credit": 0,
            "underlying": "SPY", "status": "open", "opened_asof": "2026-07-29",
            "opened_ts": "2026-07-29T14:00:00+00:00", "expiration": "2026-08-31",
            "contracts": 1, "short_strike": 735.0, "long_strike": 736.0, "width": 1.0,
            "legs_json": "[]", "entry_credit_ps": 0.31, "max_loss": 31.0})
        store.set_kv("pending_closes", json.dumps({str(pid): {
            "coid": "CLOSE-AIS-x", "reason": "profit_target",
            "exit_value_ps": 1.0, "realized_pnl": 69.0}}))
        return pid

    def _leg(strike, qty):
        return Position(symbol="SPY", qty=qty, avg_price=100.0, market_value=0.0,
                        asset_class="option", underlying="SPY",
                        option_expiration="2026-08-31", option_strike=strike,
                        option_right="P")

    class _A:
        name = "ibkr_paper"
        def __init__(self, held): self._held = held
        def list_orders(self): return []          # the close order is GONE
        def get_positions(self): return self._held

    # (a) Legs still held => the close never filled => release for re-evaluation.
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "p.db")
        pid = _seed(st)
        pend = _finalize_pending_closes(_A([_leg(735.0, -1), _leg(736.0, 1)]), st, "2026-08-04")
        assert str(pid) not in pend, "a vanished close must not freeze the position"
        assert len(st.get_open_positions()) == 1, "position stays OPEN and manageable"
        assert st.conn.execute(
            "SELECT COUNT(*) FROM journal WHERE kind='close_expired'").fetchone()[0] == 1
        st.close()

    # (b) Legs gone => it really did fill; record the close rather than reopening it.
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "p.db")
        pid = _seed(st)
        pend = _finalize_pending_closes(_A([]), st, "2026-08-04")
        assert str(pid) not in pend
        assert len(st.get_closed_positions()) == 1
        st.close()
