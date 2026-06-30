"""Phase 3 (engine 3) — earnings-vol iron-condor engine + condor lifecycle."""

import json
import math
import tempfile
from pathlib import Path

from core.brokers.base import OptionContract, OrderLeg, OrderRequest, Position
from core.brokers.sim import SimAdapter
from core.manage import mark_spread_value_ps
from core.positions import (
    ManageParams, condor_intrinsic_ps, evaluate_exit, order_to_position,
)
from core.reconcile import reconcile
from core.store import Store
from strategies.base import StrategyContext
from strategies.earnings import EarningsVol


# ── condor position model ────────────────────────────────────────────────────

def _condor_order(sp=540, lp=535, sc=560, lc=565, credit=1.50):
    return OrderRequest(
        client_order_id="ic",
        legs=[OrderLeg("SP", "sell", 1, "option", "put", strike=sp, expiration="2026-02-19"),
              OrderLeg("LP", "buy", 1, "option", "put", strike=lp, expiration="2026-02-19"),
              OrderLeg("SC", "sell", 1, "option", "call", strike=sc, expiration="2026-02-19"),
              OrderLeg("LC", "buy", 1, "option", "call", strike=lc, expiration="2026-02-19")],
        limit_price=credit, strategy="iron_condor", est_credit=credit * 100,
        max_loss=(5 - credit) * 100, underlying="SPY")


def test_order_to_position_iron_condor():
    p = order_to_position(_condor_order(), "2026-01-15", "t")
    assert p["structure"] == "iron_condor" and p["is_credit"] == 1
    legs = json.loads(p["legs_json"])
    assert legs == {"sp": 540, "lp": 535, "sc": 560, "lc": 565, "width": 5}
    assert abs(p["entry_credit_ps"] - 1.50) < 1e-9
    assert p["width"] == 5


def test_condor_intrinsic_one_side_only():
    legs = {"sp": 540, "lp": 535, "sc": 560, "lc": 565, "width": 5}
    assert condor_intrinsic_ps(legs, 550) == 0.0       # inside both shorts -> worthless (max profit)
    assert condor_intrinsic_ps(legs, 530) == 5.0       # below long put -> full width (max loss side)
    assert condor_intrinsic_ps(legs, 570) == 5.0       # above long call -> full width
    assert abs(condor_intrinsic_ps(legs, 538) - 2.0) < 1e-9  # between put strikes


def test_condor_expiry_pnl():
    pos = order_to_position(_condor_order(credit=1.50), "2026-01-15", "t")
    pos["id"] = 1
    P = ManageParams()
    # Settles worthless inside the range -> keep full credit.
    d = evaluate_exit(pos, 0.0, 550, "2026-02-19", P)
    assert d.action == "expire" and abs(d.realized_pnl - 150.0) < 1e-6
    # Breached -> full width loss minus credit kept.
    d2 = evaluate_exit(pos, 0.0, 525, "2026-02-19", P)
    assert abs(d2.realized_pnl - (1.50 - 5.0) * 100) < 1e-6   # -$350


def test_condor_take_profit():
    pos = order_to_position(_condor_order(credit=1.50), "2026-01-15", "t")
    pos["id"] = 1
    # value falls to 0.75 (50% captured) -> take profit.
    d = evaluate_exit(pos, 0.75, 550, "2026-01-20", ManageParams(profit_target_pct=0.50))
    assert d.action == "close" and d.reason == "profit_target"
    assert abs(d.realized_pnl - 75.0) < 1e-6


def test_mark_condor_value():
    sim = SimAdapter(asof="2026-01-15")
    chain = sim.get_option_chain("SPY")
    puts = sorted(c.strike for c in chain if c.option_type == "put")
    calls = sorted(c.strike for c in chain if c.option_type == "call")
    legs = {"sp": puts[len(puts)//2 - 2], "lp": puts[len(puts)//2 - 4],
            "sc": calls[len(calls)//2 + 2], "lc": calls[len(calls)//2 + 4],
            "width": 0}
    legs["width"] = max(legs["sp"] - legs["lp"], legs["lc"] - legs["sc"])
    pos = {"underlying": "SPY", "expiration": chain[0].expiration,
           "structure": "iron_condor", "legs_json": json.dumps(legs)}
    v = mark_spread_value_ps(sim, pos)
    assert v is not None and 0.0 <= v <= legs["width"]


def test_reconcile_iron_condor_four_legs():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        store.open_position(order_to_position(_condor_order(), "2026-01-15", "t"))

        class Stub:
            name = "ibkr_paper"
            def get_positions(self):
                def leg(strike, right, qty):
                    return Position(f"{right}{strike}", qty, 1, 0, "option", "SPY",
                                    option_expiration="2026-02-19",
                                    option_strike=strike, option_right=right)
                return [leg(540, "P", -1), leg(535, "P", 1), leg(560, "C", -1), leg(565, "C", 1)]
        rep = reconcile(Stub(), store)
        assert rep["ok"], rep["drift"]
        store.close()


# ── engine ───────────────────────────────────────────────────────────────────

def _opt(strike, right, mid, delta, oi=2000):
    half = max(0.05, mid * 0.03)
    return OptionContract(
        symbol=f"{right}{strike}", underlying="ABC", expiration="2026-02-19", strike=strike,
        option_type=right, bid=round(mid - half, 2), ask=round(mid + half, 2), last=mid,
        implied_vol=0.85, delta=delta, open_interest=oi, volume=500, dte=30)


def _earnings_ctx(rich=True):
    spot = 100.0
    chain = []
    for k in range(70, 131):                 # $1 ladder 70..130
        moneyness = (k - spot) / spot
        cmid = max(0.10, 4.0 * math.exp(-abs(moneyness) * 6) + max(0, spot - k) * 0.0)
        pmid = max(0.10, 4.0 * math.exp(-abs(moneyness) * 6))
        chain.append(_opt(k, "call", round(cmid + max(0, spot - k) * 0.5, 2), 0.5))
        chain.append(_opt(k, "put", round(pmid + max(0, k - spot) * 0.5, 2), -0.5))
    # Flat, low-realized-vol history so a fat ATM straddle looks RICH (or not).
    base = [100 + (0.6 if rich else 6.0) * math.sin(i / 3.0) for i in range(80)]
    return StrategyContext(
        underlying="ABC", spot=spot, option_chain=chain,
        iv_history=[0.30, 0.60, 0.95],       # current 0.85 -> high IV rank
        closes=base, highs=[b + 1 for b in base], lows=[b - 1 for b in base],
        asof="2026-01-15", earnings_date="2026-01-19", config={})


_PARAMS = {"min_iv_rank": 0.30, "rich_threshold": 1.2, "wing_width": 5.0,
           "min_open_interest": 100, "max_spread_pct": 0.5, "min_credit_ratio": 0.05}


def test_earnings_sells_condor_when_implied_move_is_rich():
    sigs = EarningsVol(_PARAMS).generate(_earnings_ctx(rich=True))
    assert sigs, "expected an iron condor into a rich earnings event"
    s = sigs[0]
    assert s.strategy == "iron_condor" and s.est_credit > 0
    rights = sorted({l.option_type for l in s.legs})
    assert rights == ["call", "put"] and len(s.legs) == 4


def test_earnings_silent_without_earnings_date():
    ctx = _earnings_ctx(rich=True)
    ctx.earnings_date = None
    assert EarningsVol(_PARAMS).generate(ctx) == []


def test_earnings_silent_when_move_not_rich():
    # High realized vol -> the same implied move is NOT rich relative to it -> stand aside.
    sigs = EarningsVol(_PARAMS).generate(_earnings_ctx(rich=False))
    assert sigs == []


def test_condor_order_plan_is_four_leg_credit():
    from core.brokers.ibkr import build_order_plan
    plan = build_order_plan(_condor_order(credit=1.50))
    assert plan["kind"] == "combo" and len(plan["legs"]) == 4
    assert plan["limit_price"] < 0          # net credit -> negative IB combo price
    sells = {l["strike"] for l in plan["legs"] if l["action"] == "SELL"}
    buys = {l["strike"] for l in plan["legs"] if l["action"] == "BUY"}
    assert sells == {540, 560} and buys == {535, 565}


def test_condor_full_lifecycle_in_sim():
    import types
    from core.manage import manage_open_positions
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        order = _condor_order(credit=1.50)
        sim = SimAdapter(asof="2026-01-15")
        res = sim.place_order(order)
        assert res.accepted
        store.open_position(order_to_position(order, "2026-01-15", "t"))
        assert len(store.get_open_positions()) == 1

        # Far-future cycle: past expiry -> management settles the condor.
        m = manage_open_positions(SimAdapter(asof="2027-06-01"), store,
                                  types.SimpleNamespace(strategies={}), "2027-06-01")
        assert len(m["closed"]) == 1
        assert len(store.get_open_positions()) == 0
        store.close()
