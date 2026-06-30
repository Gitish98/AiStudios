"""Tests for the sim adapter + one end-to-end paper cycle (no network, no keys)."""

import tempfile
from pathlib import Path

from core.brokers.sim import SimAdapter
from core.config import Config
from core.store import Store
from core.execution import run_cycle
from strategies.premium_harvest import PremiumHarvest


def test_sim_is_deterministic():
    a = SimAdapter(asof="2026-01-15")
    b = SimAdapter(asof="2026-01-15")
    assert a.get_quote("SPY").mid == b.get_quote("SPY").mid
    assert a.get_quote("SPY").mid != SimAdapter(asof="2026-02-15").get_quote("SPY").mid


def test_sim_chain_has_both_types_and_greeks():
    chain = SimAdapter(asof="2026-01-15").get_option_chain("SPY")
    assert any(c.option_type == "call" for c in chain)
    puts = [c for c in chain if c.option_type == "put"]
    assert puts and all(p.delta is not None for p in puts)
    assert all(-1.0 <= p.delta <= 0.0 for p in puts)


def _config(tickers):
    return Config(
        raw={}, mode="paper",
        account={"type": "margin", "starting_equity_usd": 30000,
                 "options_approval_level": 3},
        brokers={"execution": "sim"},
        risk={"per_position_notional_pct": 0.20, "per_trade_risk_pct": 0.5,
              "max_single_underlying_pct": 0.5, "max_gross_leverage": 5.0},
        strategies={"premium_harvest": {"min_iv_rank": 0.0}},  # force signals in sim
        watchlist=tickers,
    )


def test_end_to_end_cycle_places_paper_orders():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        adapter = SimAdapter(asof="2026-01-15", equity=30000)
        config = _config(["SPY", "XLF"])
        summary = run_cycle(adapter, [PremiumHarvest({"min_iv_rank": 0.0})],
                            store, config, asof="2026-01-15")
        assert summary["mode"] == "paper"
        assert len(summary["signals"]) >= 1
        assert len(summary["placed"]) >= 1
        store.close()


def test_idempotency_no_double_place():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "t.db"
        config = _config(["SPY", "XLF"])
        strat = [PremiumHarvest({"min_iv_rank": 0.0})]

        store = Store(db)
        s1 = run_cycle(SimAdapter(asof="2026-01-15"), strat, store, config, asof="2026-01-15")
        placed_first = len(s1["placed"])
        store.close()

        # Same day, same signal -> should be skipped as duplicate.
        store = Store(db)
        s2 = run_cycle(SimAdapter(asof="2026-01-15"), strat, store, config, asof="2026-01-15")
        store.close()

        assert placed_first >= 1
        assert len(s2["placed"]) == 0
        assert len(s2["skipped_duplicates"]) >= 1


def test_kill_switch_blocks_cycle():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        store.set_kill_switch(True)
        config = _config(["SPY"])
        summary = run_cycle(SimAdapter(asof="2026-01-15"),
                            [PremiumHarvest({"min_iv_rank": 0.0})],
                            store, config, asof="2026-01-15")
        assert len(summary["placed"]) == 0
        store.close()
