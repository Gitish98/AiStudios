"""
Tests for the IBKR adapter's PURE logic — order planning, expiry/right mapping,
and safe construction. No ib_async/ib_insync and no Gateway required (those are
only touched on a live connect, which we don't do here)."""

from core.brokers.base import OrderLeg, OrderRequest
from core.brokers.ibkr import (
    IBKRAdapter, build_order_plan, ib_expiry, option_right, parse_account_values,
)


def test_parse_account_values_cad_base_converts_to_usd():
    # A real CA margin (CAD-base) paper account: the summary tags exist ONLY in
    # CAD, plus a stray USD CashBalance. The old USD filter read $0 equity (which
    # fails the risk gate closed). We must read the CAD figures and convert to USD.
    rows = [
        ("NetLiquidation", "CAD", "982834.73"),
        ("TotalCashValue", "CAD", "982025.49"),
        ("BuyingPower", "CAD", "3263892.78"),
        ("CashBalance", "CAD", "1128120.74"),
        ("CashBalance", "USD", "-102824.03"),   # stray FX sub-balance — must NOT be used
        ("CashBalance", "BASE", "982025.49"),
        ("ExchangeRate", "CAD", "1.00"),
        ("ExchangeRate", "USD", "1.4208"),      # 1 USD = 1.4208 CAD
        ("ExchangeRate", "BASE", "1.00"),
    ]
    a = parse_account_values(rows)
    assert a["base_currency"] == "CAD" and a["converted"] is True
    # 982834.73 CAD / 1.4208 ≈ 691,747 USD — NOT $0, NOT the -102,824 USD row.
    assert abs(a["equity"] - 982834.73 / 1.4208) < 1.0
    assert abs(a["cash"] - 982025.49 / 1.4208) < 1.0
    assert abs(a["buying_power"] - 3263892.78 / 1.4208) < 1.0
    assert a["equity"] > 600000  # sanity: the gate now sees real equity


def test_parse_account_values_usd_base_unconverted():
    rows = [
        ("NetLiquidation", "USD", "1000000"),
        ("TotalCashValue", "USD", "1000000"),
        ("BuyingPower", "USD", "2000000"),
        ("ExchangeRate", "USD", "1.00"),
        ("ExchangeRate", "BASE", "1.00"),
    ]
    a = parse_account_values(rows)
    assert a["base_currency"] == "USD" and a["converted"] is False
    assert a["equity"] == 1000000 and a["buying_power"] == 2000000


def test_parse_account_values_no_fx_falls_back_to_base_currency():
    # Non-USD base but no USD ExchangeRate: don't silently mis-scale — return base
    # values and flag not-converted so the caller can warn.
    rows = [
        ("NetLiquidation", "CAD", "500000"),
        ("TotalCashValue", "CAD", "500000"),
        ("BuyingPower", "CAD", "1000000"),
    ]
    a = parse_account_values(rows)
    assert a["base_currency"] == "CAD" and a["converted"] is False
    assert a["equity"] == 500000  # unconverted, but not $0


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
    # Unreachable endpoint must fail gracefully (return False, never raise). Use a
    # non-IB port that nothing listens on, so this holds even when a real IB Gateway
    # is running locally on 4002 (which would otherwise make this connect succeed).
    a = IBKRAdapter(host="127.0.0.1", port=4999, paper=True)
    assert a.try_connect() is False
    assert a.is_paper is True
