"""Backtest harness — replays the real cycle over historical bars."""

import types

from backtest import BacktestBroker, run_backtest, _metrics
from core.brokers.sim import SimAdapter
from core.costs import CostModel
from core.config import Config
from strategies.premium_harvest import PremiumHarvest


def _bars():
    seed = SimAdapter(asof="2026-06-30")
    return {s: seed.get_history(s, 250) for s in ("SPY", "QQQ", "IWM", "DIA")}


def _cfg():
    return Config(
        raw={}, mode="paper",
        account={"type": "margin", "region": "CA", "starting_equity_usd": 30000,
                 "options_approval_level": 3},
        brokers={"execution": "sim"},
        risk={"per_position_notional_pct": 0.5, "per_trade_risk_pct": 0.5,
              "max_single_underlying_pct": 0.5, "max_gross_leverage": 50.0,
              "max_daily_new_positions": 20, "max_concurrent_positions": 20,
              "portfolio_heat_pct": 0.9},
        strategies={"premium_harvest": {"min_iv_rank": 0.0}},
        watchlist=["SPY", "QQQ", "IWM", "DIA"])


def test_backtest_broker_is_history_driven():
    bars = _bars()
    b = BacktestBroker(bars)
    b.asof = bars["SPY"][120]["date"]
    spot = b._spot("SPY")
    assert spot == round(bars["SPY"][120]["c"], 2)        # spot follows the bar at asof
    chain = b.get_option_chain("SPY")
    assert chain and any(c.option_type == "put" for c in chain)
    # history is truncated at asof
    assert all(bar["date"] <= b.asof for bar in b.get_history("SPY", 300))


def test_backtest_runs_and_reports_metrics():
    bars = _bars()
    rep = run_backtest([PremiumHarvest({"min_iv_rank": 0.0})], bars, _cfg(),
                       start="2026-01-01", end="2026-06-30")
    assert rep["days"] > 0
    assert rep["trades"] >= 1                # the strategy traded over the window
    assert "win_rate" in rep and "max_drawdown" in rep and "expectancy_net" in rep
    # Cost-aware: net = gross - costs, and costs are non-negative.
    assert rep["total_costs"] >= 0
    assert abs(rep["net_pnl"] - (rep["gross_pnl"] - rep["total_costs"])) < 0.01
    assert rep["wins"] + rep["losses"] <= rep["trades"]


def test_metrics_math():
    closed = [{"realized_pnl": 100, "exit_reason": "profit_target"},
              {"realized_pnl": -50, "exit_reason": "stop_loss"},
              {"realized_pnl": 30, "exit_reason": "expired"}]
    zero = CostModel(per_contract=0, min_per_order=0, slippage_per_contract=0)
    m = _metrics(closed, days=["d1", "d2"], costs=zero)
    assert m["trades"] == 3 and m["wins"] == 2 and m["losses"] == 1
    assert m["gross_pnl"] == 80 and m["total_costs"] == 0 and m["net_pnl"] == 80
    assert m["expectancy_net"] == round(80 / 3, 2)
    assert m["profit_factor"] == round(130 / 50, 2)
    assert m["max_drawdown"] == 50   # peak 100 -> trough 50 after the -50


def test_cost_model_drag():
    # A managed close on a 2-leg spread pays round-trip; an expiry pays open only.
    cm = CostModel()  # IBKR-ish defaults
    rt = cm.position_cost("put_credit_spread", 1, "profit_target")
    exp = cm.position_cost("put_credit_spread", 1, "expired")
    assert rt > exp > 0                       # expiry is cheaper (no closing trade)
    # An iron condor (4 legs) costs more than a vertical (2 legs).
    assert cm.position_cost("iron_condor", 1, "stop_loss") > rt
