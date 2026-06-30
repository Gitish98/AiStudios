"""Position sizing — scales to the account, floors small, gate still the ceiling."""

import tempfile
from pathlib import Path

from core.brokers.sim import SimAdapter
from core.config import Config
from core.execution import run_cycle
from core.sizing import SizeParams, size_contracts
from core.store import Store
from strategies.premium_harvest import PremiumHarvest


def test_size_contracts_fixed_fractional():
    p = SizeParams(enabled=True, per_trade_risk_pct=0.005, max_contracts=10)
    # $30k * 0.5% = $150 budget; $74 max loss/contract -> floor(150/74) = 2.
    assert size_contracts(74, 30000, p) == 2
    # $100k -> floor(500/74) = 6.
    assert size_contracts(74, 100000, p) == 6
    # capped at max_contracts.
    assert size_contracts(1, 100000, p) == 10


def test_size_zero_when_too_big_for_small_account():
    p = SizeParams(enabled=True, per_trade_risk_pct=0.005, max_contracts=10)
    # $3k * 0.5% = $15 budget; a $74 spread can't fit even 1 contract -> 0 -> skip.
    assert size_contracts(74, 3000, p) == 0


def test_disabled_sizing_is_one_contract():
    p = SizeParams(enabled=False)
    assert size_contracts(74, 1_000_000, p) == 1


def test_allocation_mult_scales_down_only():
    p = SizeParams(enabled=True, per_trade_risk_pct=0.01, max_contracts=20)
    full = size_contracts(74, 100000, p)                    # 1000/74 = 13
    half = size_contracts(74, 100000, p, allocation_mult=0.5)
    assert half == int(full * 0.5)
    # A mult above 1 can't increase size (clamped).
    assert size_contracts(74, 100000, p, allocation_mult=5.0) == full


def _cfg(equity, enabled=True, per_trade=0.005):
    return Config(
        raw={"sizing": {"enabled": enabled, "per_trade_risk_pct": per_trade,
                        "max_contracts": 10},
             "risk": {"per_trade_risk_pct": per_trade}},
        mode="paper",
        account={"type": "margin", "region": "CA", "starting_equity_usd": equity,
                 "options_approval_level": 3},
        brokers={"execution": "sim"},
        risk={"per_position_notional_pct": 0.9, "per_trade_risk_pct": per_trade,
              "max_single_underlying_pct": 0.9, "max_gross_leverage": 50.0,
              "portfolio_heat_pct": 0.9, "max_daily_new_positions": 20,
              "max_concurrent_positions": 20, "max_buying_power_pct": 1.0},
        strategies={"premium_harvest": {"min_iv_rank": 0.0}},
        watchlist=["SPY", "IWM"])


def _run(cfg, equity):
    class BigAcct(SimAdapter):
        def get_account(self):
            a = super().get_account(); a.equity = equity; a.buying_power = equity * 4
            return a
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        s = run_cycle(BigAcct(asof="2026-01-15"),
                      [PremiumHarvest({"min_iv_rank": 0.0})], store, cfg, asof="2026-01-15")
        store.close()
        return s


def test_cycle_sizes_more_contracts_on_a_bigger_account():
    s = _run(_cfg(100000, per_trade=0.01), 100000)
    assert s["placed"]
    assert any(p.get("contracts", 1) > 1 for p in s["placed"])   # sized up


def test_cycle_skips_when_too_small():
    # $2k account, 0.5% = $10 budget; the ~$74 spreads can't fit -> all rejected.
    s = _run(_cfg(2000, per_trade=0.005), 2000)
    assert s["placed"] == []
    assert any("too small to size" in r for rej in s["rejected"] for r in rej["reasons"])
