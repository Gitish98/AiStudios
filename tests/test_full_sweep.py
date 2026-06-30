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

def test_debit_vertical_notional_not_double_counted():
    o = OrderRequest(
        client_order_id="d",
        legs=[OrderLeg("C100", "buy", 1, "option", "call", strike=100),
              OrderLeg("C105", "sell", 1, "option", "call", strike=105)],
        limit_price=1.00, strategy="call_debit_spread", est_credit=-100.0,
        max_loss=100.0, underlying="SPY")
    assert _notional(o) == 100.0      # net debit ×100, NOT 2×100×1.00 = 200
