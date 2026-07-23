"""Regression tests for the 6 bugs found in the Phase 2+3 adversarial review."""

import tempfile
import types
from pathlib import Path

from core.brokers.base import OptionContract, OrderLeg, OrderRequest, OrderResult
from core.brokers.sim import SimAdapter
from core.execution import _count_opened_today
from core.manage import manage_open_positions, mark_spread_value_ps
from core.store import Store
from strategies.base import StrategyContext
from strategies.breakout import VolatilityBreakout


def _cfg():
    return types.SimpleNamespace(strategies={})


def _open(store, underlying="SPY", exp="2026-02-19", short=540, long=539,
          family="put", is_credit=1, entry=0.26):
    store.open_position({
        "client_order_id": f"o-{underlying}", "strategy": "put_credit_spread",
        "structure": "put_credit_spread", "family": family, "is_credit": is_credit,
        "underlying": underlying, "status": "open", "opened_asof": "2026-01-15",
        "opened_ts": "t", "expiration": exp, "contracts": 1, "short_strike": short,
        "long_strike": long, "width": short - long, "entry_credit_ps": entry,
        "max_loss": (short - long - entry) * 100})


# ── #1: one failing position must not abort the whole management pass ─────────

def test_manage_isolates_per_position_errors():
    class FlakyMark(SimAdapter):
        def mark_option(self, u, exp, strike, right, asof=None):
            if u == "BAD":
                raise RuntimeError("boom")
            return super().mark_option(u, exp, strike, right, asof)

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        _open(store, underlying="BAD", exp="2026-02-19")   # raises on mark
        _open(store, underlying="SPY", exp="2026-02-19")   # past asof -> should settle
        m = manage_open_positions(FlakyMark(asof="2027-06-01"), store, _cfg(), "2027-06-01")
        # The good one still got processed (closed at expiry); the bad one is held.
        assert any(c["underlying"] == "SPY" for c in m["closed"])
        assert any(h.get("reason") == "mark_error" for h in m["held"])
        store.close()


# ── #2: quote failure at expiry must NOT settle as max-loss ──────────────────

def test_no_spot_at_expiry_holds_instead_of_catastrophic_settle():
    class NoQuote(SimAdapter):
        def get_quote(self, symbol):
            raise RuntimeError("quote feed down")

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        _open(store, underlying="SPY", exp="2026-02-19")  # expired vs asof below
        m = manage_open_positions(NoQuote(asof="2027-06-01"), store, _cfg(), "2027-06-01")
        # Not closed, and certainly not as a fabricated max-loss.
        assert not m["closed"]
        assert any(h.get("reason") == "no_spot_at_expiry" for h in m["held"])
        assert len(store.get_open_positions()) == 1   # still open, awaiting a real quote
        store.close()


# ── #3: daily realized P&L sums ALL of today's closes (across cycles) ────────

def test_realized_pnl_on_sums_across_cycles():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        _open(store, underlying="A"); _open(store, underlying="B")
        ids = [p["id"] for p in store.get_open_positions()]
        store.close_position(ids[0], "2026-05-01", "t1", "stop_loss", 1.0, -2000.0)
        store.close_position(ids[1], "2026-05-01", "t2", "stop_loss", 1.0, -2000.0)
        assert store.realized_pnl_on("2026-05-01") == -4000.0
        assert store.realized_pnl_on("2026-05-02") == 0.0
        store.close()


# ── #4: closing orders must not consume the daily new-position budget ────────

def test_count_opened_today_excludes_closing_orders():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")

        def rec(coid, strat):
            o = OrderRequest(client_order_id=coid, legs=[], strategy=strat, underlying="SPY")
            r = OrderResult(accepted=True, client_order_id=coid, status="filled")
            store.record_order("2026-05-01T10:00:00", o, r)

        rec("AIS-1", "put_credit_spread")          # an opener
        rec("AIS-2", "call_debit_spread")          # an opener
        rec("CLOSE-AIS-1", "close_put_credit_spread")  # a close -> must NOT count
        assert _count_opened_today(store, "2026-05-01") == 2
        store.close()


# ── #5: breakout requires a real two-sided market on the SHORT leg ───────────

def _call(strike, delta, bid, ask, oi=1000):
    return OptionContract(
        symbol=f"C{strike}", underlying="SPY", expiration="2026-02-19", strike=strike,
        option_type="call", bid=bid, ask=ask, last=max(bid, 0.5), implied_vol=0.12,
        delta=delta, open_interest=oi, volume=100, dte=35)


def _breakout_ctx(short_bid, short_ask):
    import math
    closes, highs, lows = [], [], []
    for i in range(80):
        b = 100 + 6 * math.sin(i / 2.5)
        closes.append(b); highs.append(b + 2.5); lows.append(b - 2.5)
    last = closes[-1]
    for i in range(20):
        b = last + 0.05 * (i + 1); rng = max(0.1, 0.4 - 0.015 * i)
        closes.append(b); highs.append(b + rng / 2); lows.append(b - rng / 2)
    closes[-1] = closes[-2] + 0.02
    highs[-1] = min(highs[-2], closes[-1] + 0.05)
    lows[-1] = max(lows[-2], closes[-1] - 0.05)
    chain = [
        _call(99, 0.55, 1.95, 2.05),
        _call(100, 0.45, 1.45, 1.55),   # long ~0.45 delta
        _call(101, 0.35, short_bid, short_ask),  # short = next higher strike
        _call(102, 0.25, 0.45, 0.55),
    ]
    return StrategyContext(
        underlying="SPY", spot=100.0, option_chain=chain,
        iv_history=[0.10, 0.20, 0.30] * 25,  # 75 obs: meets the IV-rank sample floor
        closes=closes, highs=highs, lows=lows, config={})


_LEN = {"squeeze_pct": 0.9, "atr_contraction": 2.0, "max_iv_rank": 1.0,
        "range_high": 0.6, "range_low": 0.4, "max_debit_ratio": 0.95,
        "min_open_interest": 100, "max_spread_pct": 0.5}


def test_breakout_rejects_illiquid_short_leg():
    sigs = VolatilityBreakout(_LEN).generate(_breakout_ctx(short_bid=0.0, short_ask=0.0))
    assert sigs == []   # short leg has no two-sided market -> no trade


def test_breakout_accepts_liquid_short_leg():
    sigs = VolatilityBreakout(_LEN).generate(_breakout_ctx(short_bid=0.95, short_ask=1.05))
    assert sigs and sigs[0].strategy == "call_debit_spread"


# ── #6: a vertical's mark is clamped to [0, width] ───────────────────────────

def test_mark_clamped_to_width():
    class WideMark:
        name = "ibkr_paper"
        def mark_option(self, u, exp, strike, right, asof=None):
            return {545: 10.0, 540: 2.0}[strike]   # raw value 8 > width 5
    pos = {"underlying": "SPY", "expiration": "2026-02-19", "family": "put",
           "structure": "put_credit_spread", "short_strike": 545, "long_strike": 540}
    assert mark_spread_value_ps(WideMark(), pos) == 5.0
