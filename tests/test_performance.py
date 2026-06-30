"""Performance metrics + graduation gate + trade rows (shared, cost-adjusted)."""

from core.costs import CostModel
from core.performance import compute_metrics, graduation_status, trade_rows


def _closed(n_win, n_loss, win=50.0, loss=-40.0):
    out = []
    for i in range(n_win):
        out.append({"realized_pnl": win, "exit_reason": "profit_target",
                    "structure": "put_credit_spread", "contracts": 1,
                    "closed_asof": f"2026-01-{(i % 28) + 1:02d}", "entry_credit_ps": 0.26,
                    "underlying": "SPY", "short_strike": 540, "long_strike": 539,
                    "opened_asof": "2026-01-01", "expiration": "2026-02-19"})
    for i in range(n_loss):
        out.append({"realized_pnl": loss, "exit_reason": "stop_loss",
                    "structure": "put_credit_spread", "contracts": 1,
                    "closed_asof": f"2026-02-{(i % 28) + 1:02d}"})
    return out


def test_metrics_net_of_costs():
    closed = _closed(2, 1)
    days = sorted({p["closed_asof"] for p in closed})
    m = compute_metrics(closed, days, CostModel())
    assert m["trades"] == 3 and m["wins"] == 2
    assert m["net_pnl"] < m["gross_pnl"]            # costs subtracted
    assert m["total_costs"] > 0


def test_graduation_requires_days_trades_and_positive_net():
    # Plenty of trades/days but negative net expectancy -> not graduated.
    losing = compute_metrics(_closed(20, 40), [f"d{i}" for i in range(70)], CostModel())
    g = graduation_status(losing)
    assert not g["graduated"]
    assert any("expectancy" in r for r in g["reasons"])

    # Too few trades -> not graduated even if positive.
    few = compute_metrics(_closed(5, 1, win=100, loss=-10),
                          [f"d{i}" for i in range(70)], CostModel())
    g2 = graduation_status(few)
    assert not g2["graduated"]
    assert any("trades" in r for r in g2["reasons"])


def test_graduation_passes_when_all_met():
    closed = _closed(60, 20, win=100, loss=-10)
    days = [f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}" for i in range(70)]
    m = compute_metrics(closed, days, CostModel(per_contract=0, min_per_order=0,
                                                slippage_per_contract=0))
    g = graduation_status(m, min_days=60, min_trades=40)
    assert m["expectancy_net"] > 0 and g["graduated"], (m, g)


def test_trade_rows_have_gross_cost_net():
    rows = trade_rows(_closed(1, 0), CostModel())
    r = rows[0]
    assert set(["opened", "closed", "underlying", "structure", "contracts",
                "gross_pnl", "cost", "net_pnl"]).issubset(r.keys())
    assert abs(r["net_pnl"] - (r["gross_pnl"] - r["cost"])) < 0.01
