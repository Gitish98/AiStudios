"""Tests for the Alpaca order-body construction and store dedup semantics."""

import tempfile
from pathlib import Path

from core.brokers.alpaca import build_order_body
from core.brokers.base import OrderLeg, OrderRequest, OrderResult
from core.store import Store


def _spread(qty=1, credit=0.30, est_credit=30.0):
    return OrderRequest(
        client_order_id="x",
        legs=[
            OrderLeg("SPY_SP", "sell", qty, "option", "put", strike=540),
            OrderLeg("SPY_LP", "buy", qty, "option", "put", strike=539),
        ],
        limit_price=credit, strategy="put_credit_spread",
        est_credit=est_credit, max_loss=70.0, underlying="SPY",
    )


def test_mleg_credit_sent_as_negative_price():
    body = build_order_body(_spread())
    assert body["order_class"] == "mleg"
    # Credit order -> negative limit price (Alpaca convention).
    assert float(body["limit_price"]) < 0


def test_mleg_ratio_qty_gcd_reduced():
    body = build_order_body(_spread(qty=2))
    # 2-contract spread -> parent qty 2, each ratio 1 (GCD reduced).
    assert body["qty"] == "2"
    assert all(leg["ratio_qty"] == "1" for leg in body["legs"])


def test_mleg_position_intent_set():
    body = build_order_body(_spread())
    intents = {leg["side"]: leg["position_intent"] for leg in body["legs"]}
    assert intents["sell"] == "sell_to_open"
    assert intents["buy"] == "buy_to_open"


def test_single_leg_body():
    o = OrderRequest(
        client_order_id="s",
        legs=[OrderLeg("SPY", "buy", 10, "us_equity")],
        limit_price=550.0, order_type="limit",
    )
    body = build_order_body(o)
    assert body["symbol"] == "SPY" and body["qty"] == "10" and body["side"] == "buy"
    assert "order_class" not in body


def test_store_rejected_order_can_be_retried():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        order = _spread()
        rejected = OrderResult(accepted=False, client_order_id="x",
                               status="rejected", reason="insufficient bp")
        store.record_order("2026-01-15T10:00:00", order, rejected)
        # A rejected order must NOT count as a permanent duplicate.
        assert store.has_order("x") is False
        # Once accepted, it IS a duplicate.
        ok = OrderResult(accepted=True, client_order_id="x",
                         broker_order_id="B1", status="accepted")
        store.record_order("2026-01-15T10:01:00", order, ok)
        assert store.has_order("x") is True
        store.close()
