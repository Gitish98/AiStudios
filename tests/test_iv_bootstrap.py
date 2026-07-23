"""IV-rank history bootstrap — the system builds its own IV history over time so
premium-harvest / earnings-vol work on a real broker with no paid feed."""

import tempfile
import types
from pathlib import Path

from core.brokers.sim import SimAdapter
from core.config import Config
from core.execution import run_cycle, _atm_iv_from_chain
from core.options_math import iv_rank
from core.store import Store
from strategies.premium_harvest import PremiumHarvest


def test_atm_iv_from_chain_picks_nearest_strike():
    sim = SimAdapter(asof="2026-01-15")
    chain = sim.get_option_chain("SPY")
    spot = sim.get_quote("SPY").mid
    atm = _atm_iv_from_chain(chain, spot)
    assert atm is not None and atm > 0


def test_snapshots_accrue_and_build_history():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        # Record a few days of ATM IV for SPY.
        for i, iv in enumerate([0.12, 0.15, 0.20, 0.18, 0.25]):
            store.record_iv_snapshot("SPY", f"2026-01-{10+i:02d}", iv)
        hist = store.get_iv_history("SPY")
        assert hist == [0.12, 0.15, 0.20, 0.18, 0.25]   # oldest -> newest
        # Idempotent per day (re-recording the same day replaces, doesn't append).
        store.record_iv_snapshot("SPY", "2026-01-10", 0.99)
        assert len(store.get_iv_history("SPY")) == 5
        # And the history is usable for an IV rank.
        # Usable as a rank input — but only once the sample floor is met;
        # 5 days is storage, not a trustworthy rank (see test_options_math).
        assert iv_rank(0.20, hist, min_observations=2) is not None
        assert iv_rank(0.20, hist) is None   # real floor refuses 5 obs
        store.close()


def test_cycle_records_iv_snapshot():
    """Each cycle records today's ATM IV, so a real broker accrues its own history."""
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        cfg = Config(
            raw={}, mode="paper",
            account={"type": "margin", "region": "CA", "starting_equity_usd": 30000,
                     "options_approval_level": 3},
            brokers={"execution": "sim"},
            risk={"per_position_notional_pct": 0.5, "per_trade_risk_pct": 0.5,
                  "max_single_underlying_pct": 0.5, "max_gross_leverage": 50.0,
                  "max_daily_new_positions": 20, "max_concurrent_positions": 20},
            strategies={"premium_harvest": {"min_iv_rank": 0.0}},
            watchlist=["SPY"])
        # Run a cycle; it should record an IV snapshot for SPY.
        run_cycle(SimAdapter(asof="2026-01-15"),
                  [PremiumHarvest({"min_iv_rank": 0.0})], store, cfg, asof="2026-01-15")
        assert len(store.get_iv_history("SPY")) >= 1   # snapshot was recorded
        store.close()
