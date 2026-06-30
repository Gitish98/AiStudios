"""Phase 3 — volatility-compression breakout engine + debit-spread lifecycle."""

import math
import tempfile
from pathlib import Path

from core.brokers.base import OrderLeg, OrderRequest, Position
from core.brokers.sim import SimAdapter
from core.indicators import (
    atr_contraction_ratio, bandwidth_percentile, inside_day, nr7, range_position,
)
from core.manage import mark_spread_value_ps
from core.positions import (
    ManageParams, evaluate_exit, intrinsic_vertical_ps, order_to_position,
)
from core.reconcile import reconcile
from core.store import Store
from strategies.base import StrategyContext
from strategies.breakout import VolatilityBreakout


# ── squeeze indicators ───────────────────────────────────────────────────────

def test_nr7_and_inside_day():
    highs = [10, 10, 10, 10, 10, 10, 10.2]
    lows = [9, 9, 9, 9, 9, 9, 9.9]
    assert nr7(highs, lows) is True          # last range 0.3 is narrowest
    assert inside_day([10, 9.5], [9, 9.2]) is True
    assert inside_day([10, 11], [9, 8]) is False


def test_bandwidth_percentile_low_when_compressed():
    closes = [100 + 8 * math.sin(i / 2.5) for i in range(80)]   # volatile
    closes += [104 + 0.02 * i for i in range(25)]                # then very tight
    pct = bandwidth_percentile(closes)
    assert pct is not None and pct < 0.40


def test_range_position_direction():
    assert range_position([1, 2, 3, 4, 5] * 5) > 0.9     # ends at the high
    assert range_position([5, 4, 3, 2, 1] * 5) < 0.1     # ends at the low


def test_atr_contraction_ratio_below_one_when_calming():
    highs = [100 + (5 if i < 50 else 1) for i in range(80)]
    lows = [100 - (5 if i < 50 else 1) for i in range(80)]
    closes = [100 for _ in range(80)]
    r = atr_contraction_ratio(highs, lows, closes)
    assert r is not None and r < 1.0


# ── engine ───────────────────────────────────────────────────────────────────

def _compressed_uptrend():
    closes, highs, lows = [], [], []
    for i in range(80):
        b = 100 + 6 * math.sin(i / 2.5)
        closes.append(round(b, 2)); highs.append(round(b + 2.5, 2)); lows.append(round(b - 2.5, 2))
    last = closes[-1]
    for i in range(20):
        b = last + 0.05 * (i + 1)
        rng = max(0.1, 0.4 - 0.015 * i)
        closes.append(round(b, 2)); highs.append(round(b + rng / 2, 2)); lows.append(round(b - rng / 2, 2))
    # final bar narrowest + inside the prior
    closes[-1] = round(closes[-2] + 0.02, 2)
    highs[-1] = round(min(highs[-2], closes[-1] + 0.05), 2)
    lows[-1] = round(max(lows[-2], closes[-1] - 0.05), 2)
    return closes, highs, lows


def _ctx_for(symbol="SPY"):
    sim = SimAdapter(asof="2026-01-15")
    closes, highs, lows = _compressed_uptrend()
    return StrategyContext(
        underlying=symbol, spot=sim.get_quote(symbol).mid,
        option_chain=sim.get_option_chain(symbol),
        iv_history=sim.iv_history(symbol),
        closes=closes, highs=highs, lows=lows, config={})


_LENIENT = {"squeeze_pct": 0.9, "atr_contraction": 2.0, "max_iv_rank": 1.0,
            "range_high": 0.6, "range_low": 0.4, "max_debit_ratio": 0.95}


def test_breakout_emits_bull_call_debit_spread():
    sigs = VolatilityBreakout(_LENIENT).generate(_ctx_for("SPY"))
    assert sigs, "expected a breakout signal on a compressed uptrend"
    s = sigs[0]
    assert s.strategy == "call_debit_spread"
    assert s.est_credit < 0          # debit (our sign convention)
    assert s.max_loss > 0
    # long strike below short strike for a bull call spread
    long = next(l for l in s.legs if l.side == "buy")
    short = next(l for l in s.legs if l.side == "sell")
    assert long.strike < short.strike and long.option_type == "call"


def test_breakout_silent_without_history():
    ctx = _ctx_for("SPY")
    ctx.closes = ctx.closes[:40]      # too little history
    assert VolatilityBreakout(_LENIENT).generate(ctx) == []


# ── debit-spread exit logic ──────────────────────────────────────────────────

def _debit_pos(entry=0.40, short=545, long=540, exp="2026-12-18"):
    return {"structure": "call_debit_spread", "family": "call", "is_credit": 0,
            "underlying": "SPY", "expiration": exp, "contracts": 1,
            "short_strike": short, "long_strike": long, "width": short - long,
            "entry_credit_ps": entry, "max_loss": entry * 100}


P = ManageParams(debit_profit_gain=1.0, debit_stop_frac=0.50)


def test_debit_take_profit_at_double():
    d = evaluate_exit(_debit_pos(entry=0.40), current_value_ps=0.80, spot=550,
                      asof="2026-11-01", params=P)
    assert d.action == "close" and d.reason == "profit_target"
    assert abs(d.realized_pnl - 40.0) < 1e-6


def test_debit_stop_at_half_loss():
    d = evaluate_exit(_debit_pos(entry=0.40), current_value_ps=0.20, spot=541,
                      asof="2026-11-01", params=P)
    assert d.action == "close" and d.reason == "stop_loss"
    assert abs(d.realized_pnl + 20.0) < 1e-6


def test_debit_expiry_call_intrinsic():
    # spot well above short strike -> spread worth full width.
    d = evaluate_exit(_debit_pos(entry=0.40, short=545, long=540, exp="2026-11-01"),
                      current_value_ps=0.0, spot=600, asof="2026-11-01", params=P)
    assert d.action == "expire"
    assert abs(d.realized_pnl - (5 - 0.40) * 100) < 1e-6
    # and worthless below the long strike
    d2 = evaluate_exit(_debit_pos(entry=0.40, short=545, long=540, exp="2026-11-01"),
                       current_value_ps=0.0, spot=500, asof="2026-11-01", params=P)
    assert abs(d2.realized_pnl + 40.0) < 1e-6


def test_intrinsic_call_vs_put():
    assert intrinsic_vertical_ps("call", 545, 540, 600) == 5.0
    assert intrinsic_vertical_ps("call", 545, 540, 500) == 0.0
    assert intrinsic_vertical_ps("put", 545, 540, 500) == 5.0


def test_order_to_position_debit_call_spread():
    o = OrderRequest(
        client_order_id="d",
        legs=[OrderLeg("C540", "buy", 1, "option", "call", strike=540, expiration="2026-02-19"),
              OrderLeg("C545", "sell", 1, "option", "call", strike=545, expiration="2026-02-19")],
        limit_price=2.0, strategy="call_debit_spread", est_credit=-200.0,
        max_loss=200.0, underlying="SPY")
    p = order_to_position(o, "2026-01-15", "t")
    assert p["structure"] == "call_debit_spread" and p["family"] == "call"
    assert p["is_credit"] == 0
    assert p["short_strike"] == 545 and p["long_strike"] == 540
    assert abs(p["entry_credit_ps"] - 2.0) < 1e-9


def test_mark_call_spread_value_is_positive():
    sim = SimAdapter(asof="2026-01-15")
    chain = sim.get_option_chain("SPY")
    calls = sorted({c.strike for c in chain if c.option_type == "call"})
    lo, hi = calls[len(calls)//2], calls[len(calls)//2 + 1]
    pos = {"underlying": "SPY", "expiration": chain[0].expiration, "family": "call",
           "structure": "call_debit_spread", "short_strike": hi, "long_strike": lo}
    v = mark_spread_value_ps(sim, pos)
    assert v is not None and v >= 0.0


def test_reconcile_matches_call_debit_spread():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        store.open_position({
            "client_order_id": "d", "strategy": "call_debit_spread",
            "structure": "call_debit_spread", "family": "call", "is_credit": 0,
            "underlying": "SPY", "status": "open", "opened_asof": "2026-01-15",
            "opened_ts": "t", "expiration": "2026-02-19", "contracts": 1,
            "short_strike": 545, "long_strike": 540, "width": 5,
            "entry_credit_ps": 2.0, "max_loss": 200})

        class Stub:
            name = "ibkr_paper"
            def get_positions(self):
                return [
                    Position("C540", 1, 2, 0, "option", "SPY", option_expiration="2026-02-19",
                             option_strike=540, option_right="C"),
                    Position("C545", -1, 1, 0, "option", "SPY", option_expiration="2026-02-19",
                             option_strike=545, option_right="C"),
                ]
        rep = reconcile(Stub(), store)
        assert rep["ok"], rep["drift"]
        store.close()
