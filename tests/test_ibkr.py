"""
Tests for the IBKR adapter's PURE logic — order planning, expiry/right mapping,
and safe construction. No ib_async/ib_insync and no Gateway required (those are
only touched on a live connect, which we don't do here)."""

from core.brokers.base import OrderLeg, OrderRequest
from core.brokers.ibkr import (
    IBKRAdapter, build_order_plan, ib_expiry, option_right,
)


def _raises(exc, fn):
    try:
        fn()
    except exc:
        return True
    return False


def test_ib_expiry_and_right():
    assert ib_expiry("2026-02-19") == "20260219"
    assert option_right("call") == "C"
    assert option_right("put") == "P"


def _put_credit_spread(qty=1, credit=0.26, est_credit=26.0):
    return OrderRequest(
        client_order_id="ib1",
        legs=[
            OrderLeg("SPY_SP", "sell", qty, "option", "put", strike=540,
                     expiration="2026-02-19"),
            OrderLeg("SPY_LP", "buy", qty, "option", "put", strike=539,
                     expiration="2026-02-19"),
        ],
        limit_price=credit, strategy="put_credit_spread",
        est_credit=est_credit, max_loss=74.0, underlying="SPY",
    )


def test_combo_plan_credit_is_negative_price():
    plan = build_order_plan(_put_credit_spread())
    assert plan["kind"] == "combo"
    assert plan["action"] == "BUY"
    # Net credit must be submitted as a NEGATIVE combo limit price (IB convention).
    assert plan["limit_price"] < 0


def test_combo_plan_leg_actions_and_ratios():
    plan = build_order_plan(_put_credit_spread(qty=2))
    assert plan["total_quantity"] == 2          # gcd of (2,2)
    assert all(leg["ratio"] == 1 for leg in plan["legs"])
    actions = {leg["strike"]: leg["action"] for leg in plan["legs"]}
    assert actions[540] == "SELL" and actions[539] == "BUY"
    assert all(leg["right"] == "P" for leg in plan["legs"])
    assert all(leg["expiry"] == "20260219" for leg in plan["legs"])


def test_single_equity_plan():
    o = OrderRequest(
        client_order_id="s",
        legs=[OrderLeg("SPY", "buy", 10, "us_equity")],
        limit_price=550.0, underlying="SPY",
    )
    plan = build_order_plan(o)
    assert plan["kind"] == "single" and plan["sec_type"] == "STK"
    assert plan["action"] == "BUY" and plan["total_quantity"] == 10
    assert plan["limit_price"] == 550.0


def test_refuses_live_port_when_paper():
    assert _raises(ValueError, lambda: IBKRAdapter(port=4001, paper=True))  # Gateway LIVE
    assert _raises(ValueError, lambda: IBKRAdapter(port=7496, paper=True))  # TWS LIVE


def test_try_connect_returns_false_without_gateway():
    # No IB library / no Gateway here — must fail gracefully, never raise.
    a = IBKRAdapter(host="127.0.0.1", port=4002, paper=True)
    assert a.try_connect() is False
    assert a.is_paper is True
