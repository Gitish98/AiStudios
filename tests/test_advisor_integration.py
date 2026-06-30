"""The advisor is wired into the cycle as a VETO-ONLY layer: it runs after the
gate approves and can only DROP trades, never add or place them."""

import tempfile
from pathlib import Path

from core.brokers.sim import SimAdapter
from core.config import Config
from core.execution import run_cycle
from core.store import Store
from strategies.premium_harvest import PremiumHarvest


class VetoAll:
    """A fake advisor that vetoes every candidate."""
    def annotate(self, candidates, research):
        return [{"client_order_id": c["client_order_id"], "keep": False,
                 "allocation_mult": 0.0, "thesis": "no thanks"} for c in candidates]


class KeepAll:
    def annotate(self, candidates, research):
        return [{"client_order_id": c["client_order_id"], "keep": True,
                 "allocation_mult": 1.0, "thesis": "looks fine"} for c in candidates]


class TriesToAdd:
    """A hostile advisor that returns an extra candidate that was never passed
    in — the cycle must ignore it (the advisor can only see/veto approved ones)."""
    def annotate(self, candidates, research):
        out = [{"client_order_id": c["client_order_id"], "keep": True,
                "allocation_mult": 1.0, "thesis": "ok"} for c in candidates]
        out.append({"client_order_id": "GHOST-ORDER", "keep": True,
                    "allocation_mult": 1.0, "thesis": "injected"})
        return out


def _cfg():
    return Config(
        raw={}, mode="paper",
        account={"type": "margin", "region": "CA", "starting_equity_usd": 30000,
                 "options_approval_level": 3},
        brokers={"execution": "sim"},
        risk={"per_position_notional_pct": 0.5, "per_trade_risk_pct": 0.5,
              "max_single_underlying_pct": 0.5, "max_gross_leverage": 50.0,
              "max_daily_new_positions": 20, "max_concurrent_positions": 20},
        strategies={"premium_harvest": {"min_iv_rank": 0.0}},
        watchlist=["SPY", "IWM", "DIA"])


def _run(advisor):
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        s = run_cycle(SimAdapter(asof="2026-01-15"),
                      [PremiumHarvest({"min_iv_rank": 0.0})], store, _cfg(),
                      asof="2026-01-15", advisor=advisor)
        open_n = len(store.get_open_positions())
        store.close()
        return s, open_n


def test_advisor_can_veto_all_trades():
    s, open_n = _run(VetoAll())
    assert len(s["placed"]) == 0          # everything vetoed
    assert len(s.get("vetoed", [])) >= 1
    assert open_n == 0                      # nothing reached the book


def test_advisor_keep_matches_no_advisor():
    s_keep, n_keep = _run(KeepAll())
    s_none, n_none = _run(None)
    assert len(s_keep["placed"]) == len(s_none["placed"]) >= 1
    assert n_keep == n_none
    assert not s_keep.get("vetoed")


def test_advisor_cannot_inject_a_trade():
    # The hostile advisor's GHOST-ORDER must never be placed; only the real
    # gate-approved candidates can go through.
    s, _ = _run(TriesToAdd())
    placed_ids = {p["client_order_id"] for p in s["placed"]}
    assert "GHOST-ORDER" not in placed_ids
    assert all(pid.startswith("AIS-") for pid in placed_ids)
