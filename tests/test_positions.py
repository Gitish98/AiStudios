"""Phase 2 — position exit logic + end-to-end management cycle."""

import tempfile
from pathlib import Path

from core.brokers.base import OrderLeg, OrderRequest
from core.brokers.sim import SimAdapter
from core.config import Config
from core.execution import run_cycle
from core.manage import manage_open_positions
from core.market_calendar import is_trading_day
from core.positions import (
    ManageParams, evaluate_exit, intrinsic_vertical_ps, order_to_position,
)
from core.store import Store
from strategies.premium_harvest import PremiumHarvest


def _pos(entry=0.30, short=540, long=539, exp="2026-12-18", contracts=1):
    return {
        "id": 1, "underlying": "SPY", "expiration": exp, "contracts": contracts,
        "short_strike": short, "long_strike": long, "width": short - long,
        "entry_credit_ps": entry, "max_loss": (short - long - entry) * 100,
    }


P = ManageParams()  # 50% / 2x / 21 DTE


def test_take_profit_fires_at_half_credit():
    # entry 0.30; value falls to 0.15 (= 50% captured) -> close, profit.
    d = evaluate_exit(_pos(entry=0.30), current_value_ps=0.15, spot=560,
                      asof="2026-11-01", params=P)
    assert d.action == "close" and d.reason == "profit_target"
    assert d.realized_pnl > 0


def test_stop_loss_fires_at_3x_value():
    # entry 0.30; value rises to 0.90 (=3x) -> loss = 2x credit -> stop.
    d = evaluate_exit(_pos(entry=0.30), current_value_ps=0.90, spot=520,
                      asof="2026-11-01", params=P)
    assert d.action == "close" and d.reason == "stop_loss"
    assert d.realized_pnl < 0


def test_hold_in_the_middle():
    d = evaluate_exit(_pos(entry=0.30), current_value_ps=0.25, spot=555,
                      asof="2026-11-01", params=P)
    assert d.action == "hold"


def test_dte_management_exit():
    # 10 days to expiry, value mid-range -> time exit.
    d = evaluate_exit(_pos(entry=0.30, exp="2026-11-11"), current_value_ps=0.22,
                      spot=555, asof="2026-11-01", params=P)
    assert d.action == "close" and d.reason == "dte_manage"


def test_expiry_worthless_keeps_full_credit():
    # At expiry, spot well above short strike -> spread expires worthless.
    d = evaluate_exit(_pos(entry=0.30, short=540, long=539, exp="2026-11-01"),
                      current_value_ps=0.0, spot=580, asof="2026-11-01", params=P)
    assert d.action == "expire"
    assert abs(d.realized_pnl - 30.0) < 1e-6   # +$0.30 * 100


def test_expiry_max_loss_when_deep_itm():
    # Spot far below long strike -> spread worth full width -> max loss.
    d = evaluate_exit(_pos(entry=0.30, short=540, long=539, exp="2026-11-01"),
                      current_value_ps=0.0, spot=500, asof="2026-11-01", params=P)
    assert d.action == "expire"
    assert abs(d.realized_pnl - (0.30 - 1.0) * 100) < 1e-6   # -$70


def test_intrinsic_clamped_to_width():
    assert intrinsic_vertical_ps("put", 540, 539, 600) == 0.0
    assert intrinsic_vertical_ps("put", 540, 539, 500) == 1.0  # clamped to width
    assert abs(intrinsic_vertical_ps("put", 540, 539, 539.5) - 0.5) < 1e-9


def _config(tickers, manage=None):
    sp = {"min_iv_rank": 0.0}
    if manage:
        sp["manage"] = manage
    return Config(
        raw={}, mode="paper",
        account={"type": "margin", "region": "CA", "starting_equity_usd": 30000,
                 "options_approval_level": 3},
        brokers={"execution": "sim"},
        risk={"per_position_notional_pct": 0.2, "per_trade_risk_pct": 0.5,
              "max_single_underlying_pct": 0.5, "max_gross_leverage": 5.0,
              "max_daily_new_positions": 20, "max_concurrent_positions": 20},
        strategies={"premium_harvest": sp},
        watchlist=tickers,
    )


def test_open_then_manage_closes_eventually():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "t.db"
        config = _config(["SPY", "IWM", "DIA"])
        strat = [PremiumHarvest({"min_iv_rank": 0.0})]

        # Day 1: open spreads.
        store = Store(db)
        s1 = run_cycle(SimAdapter(asof="2026-01-15"), strat, store, config, asof="2026-01-15")
        opened = len(store.get_open_positions())
        store.close()
        assert s1["placed"] and opened >= 1

        # Far future date: every position is past expiry -> management settles them all.
        store = Store(db)
        m = manage_open_positions(SimAdapter(asof="2027-06-01"), store, config, "2027-06-01")
        remaining = len(store.get_open_positions())
        store.close()
        assert m["evaluated"] == opened
        assert len(m["closed"]) == opened
        assert remaining == 0


def test_order_to_position_derivation():
    order = OrderRequest(
        client_order_id="x",
        legs=[OrderLeg("SPY_SP", "sell", 1, "option", "put", strike=540, expiration="2026-02-19"),
              OrderLeg("SPY_LP", "buy", 1, "option", "put", strike=539, expiration="2026-02-19")],
        limit_price=0.26, strategy="put_credit_spread", est_credit=26.0,
        max_loss=74.0, underlying="SPY")
    p = order_to_position(order, "2026-01-15", "2026-01-15T10:00:00")
    assert p["short_strike"] == 540 and p["long_strike"] == 539 and p["width"] == 1
    assert abs(p["entry_credit_ps"] - 0.26) < 1e-9


def test_calendar_blocks_weekend():
    from datetime import date
    assert is_trading_day(date(2026, 1, 17)) is False   # Saturday
    assert is_trading_day(date(2026, 1, 1)) is False     # New Year's Day (holiday)
    assert is_trading_day(date(2026, 1, 15)) is True      # Thursday
