"""
Tests for realized-fill capture and slippage math — the measurement layer the
day-20 audit found structurally missing.

Locks three critical regressions:
  1. `_is_filled` must NOT require filled_qty > 0 (a real cross-process IBKR fill
     reports status='Filled' with filled=0.0; the old check left entries stuck
     'pending' forever and left phantom positions after a filled close).
  2. Fill prices are captured at fill time into the positions table (IB's fill
     feed is session-scoped — unrecoverable the next day).
  3. The dashboard's graduation gate equals the CLI's cost-aware gate. They must
     never diverge again: the dashboard previously omitted the cost model and so
     reported "eligible" exactly when the honest gate said no.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import dashboard_pro
from core.fills import (
    entry_slippage_ps, exit_slippage_ps, signed_credit_ps, summarize_slippage,
)
from core.manage import _is_filled
from core.store import Store


class _R:
    """Minimal broker OrderResult stand-in."""
    def __init__(self, status="filled", filled_qty=0.0, filled_avg_price=0.0):
        self.status = status
        self.filled_qty = filled_qty
        self.filled_avg_price = filled_avg_price


def test_is_filled_accepts_zero_filled_qty():
    # THE bug: IB reports status='Filled' with filled=0.0 when an order is
    # reconstructed in a new process (our daily cron). This must count as filled.
    assert _is_filled(_R("Filled", 0.0), 1) is True
    assert _is_filled(_R("filled", 2.0), 1) is True
    assert _is_filled(_R("Submitted", 0.0), 1) is False
    assert _is_filled(_R("Cancelled", 0.0), 1) is False
    assert _is_filled(_R("", 0.0), 1) is False


def test_signed_credit_convention():
    # IB BAG: negative price == credit received. Ours: credit positive.
    assert signed_credit_ps(-0.26) == 0.26      # received 0.26 credit
    assert signed_credit_ps(0.40) == -0.40      # paid a 0.40 debit
    # Unusable values must be None, never a fabricated 0.
    for bad in (None, 0.0, "abc", float("nan")):
        assert signed_credit_ps(bad) is None


def test_entry_slippage_positive_is_adverse():
    # Intended to receive 0.26; actually received 0.24 -> 0.02 adverse.
    assert entry_slippage_ps(0.26, -0.24) == 0.02
    # Received MORE than intended -> negative slippage (favourable).
    assert entry_slippage_ps(0.26, -0.28) == -0.02
    assert entry_slippage_ps(0.26, None) is None


def test_exit_slippage_positive_is_adverse():
    # Intended to pay 0.10 to close; actually paid 0.13 -> 0.03 adverse.
    assert exit_slippage_ps(0.10, 0.13) == 0.03
    assert exit_slippage_ps(0.10, 0.08) == -0.02
    assert exit_slippage_ps(None, 0.13) is None


def test_summarize_slippage_reports_coverage():
    rows = [
        {"entry_slip_ps": 0.02, "exit_slip_ps": 0.03},
        {"entry_slip_ps": 0.01, "exit_slip_ps": None},   # exit never reported
        {"entry_slip_ps": None, "exit_slip_ps": None},   # unmeasured position
    ]
    s = summarize_slippage(rows)
    assert s["positions"] == 3 and s["legs_measured"] == 3
    assert s["coverage"] == round(2 / 3, 3)      # honest about what we could measure
    assert s["mean_entry_slip_ps"] == 0.015
    assert s["worst_slip_ps"] == 0.03


def _seed_position(store: Store) -> int:
    return store.open_position({
        "client_order_id": "c1", "strategy": "premium_harvest",
        "structure": "put_credit_spread", "family": "put", "is_credit": 1,
        "underlying": "SPY", "status": "pending", "opened_asof": "2026-07-01",
        "opened_ts": "2026-07-01T14:00:00+00:00", "expiration": "2026-08-21",
        "contracts": 1, "short_strike": 540.0, "long_strike": 539.0, "width": 1.0,
        "legs_json": "[]", "entry_credit_ps": 0.26, "max_loss": 74.0,
    })


def test_fill_capture_persists_and_survives_migration():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        store = Store(db)
        pid = _seed_position(store)

        store.record_entry_fill(pid, signed_credit_ps(-0.24), entry_slippage_ps(0.26, -0.24))
        store.record_exit_fill(pid, signed_credit_ps(0.13), exit_slippage_ps(0.10, 0.13))
        row = store.conn.execute(
            "SELECT entry_fill_ps, entry_slip_ps, exit_fill_ps, exit_slip_ps "
            "FROM positions WHERE id=?", (pid,)).fetchone()
        assert row["entry_fill_ps"] == 0.24 and row["entry_slip_ps"] == 0.02
        assert row["exit_fill_ps"] == -0.13 and row["exit_slip_ps"] == 0.03
        store.close()

        # Re-opening runs _migrate again: must be idempotent, not lose data.
        store2 = Store(db)
        row2 = store2.conn.execute(
            "SELECT entry_slip_ps FROM positions WHERE id=?", (pid,)).fetchone()
        assert row2["entry_slip_ps"] == 0.02
        store2.close()


def test_dashboard_graduation_matches_cli_cost_aware_gate():
    # REGRESSION: the dashboard used to compute expectancy with NO cost model, so
    # a gross-positive but net-negative book rendered as "eligible". The two must
    # now agree exactly, because there is only one implementation.
    from core.costs import CostModel
    from core.performance import compute_metrics, graduation_status

    # Small gross wins that commissions+slippage eat: gross > 0, net < 0.
    closed = [{"realized_pnl": 3.0, "closed_asof": f"2026-06-{d:02d}",
               "structure": "put_credit_spread", "contracts": 1,
               "exit_reason": "profit_target"} for d in range(1, 21)]

    dash = dashboard_pro._graduation(closed)
    days = sorted({p["closed_asof"] for p in closed})
    m = compute_metrics(closed, days, CostModel.from_config(None) if False else CostModel())
    truth = graduation_status(m)

    assert dash["trades"] == m["trades"]
    assert dash["eligible"] == truth["graduated"]
    # The whole point: costs are subtracted, so gross-positive is not "eligible".
    assert dash["gross"] > 0
    assert dash["net"] < dash["gross"]
    assert dash["eligible"] is False
