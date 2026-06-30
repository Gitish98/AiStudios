"""
Offline test for dashboard_pro.build_pro — seeds a temp store with closed
positions + journal entries, builds the HTML, and asserts it is written,
self-contained (no external src/href, no fetch), and contains the equity-curve
SVG plus the computed win-rate number.

Stdlib-runner friendly: plain asserts, no `import pytest` at module top.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import dashboard_pro
from core.store import Store


def _seed(db_path: Path) -> None:
    store = Store(db_path)

    # Two closed positions: one winner, one loser -> win rate 50.0%.
    win = store.open_position({
        "client_order_id": "c1", "strategy": "premium", "structure": "put_credit_spread",
        "family": "put", "is_credit": 1, "underlying": "SPY", "status": "open",
        "opened_asof": "2026-06-01", "opened_ts": "2026-06-01T14:00:00+00:00",
        "expiration": "2026-06-20", "contracts": 1, "short_strike": 500.0,
        "long_strike": 495.0, "width": 5.0, "legs_json": "[]",
        "entry_credit_ps": 1.20, "max_loss": 380.0,
    })
    store.close_position(win, "2026-06-15", "2026-06-15T14:00:00+00:00",
                         "profit_target", 0.30, 90.0)

    loss = store.open_position({
        "client_order_id": "c2", "strategy": "premium", "structure": "call_debit_spread",
        "family": "call", "is_credit": 0, "underlying": "QQQ", "status": "open",
        "opened_asof": "2026-06-02", "opened_ts": "2026-06-02T14:00:00+00:00",
        "expiration": "2026-06-21", "contracts": 2, "short_strike": 450.0,
        "long_strike": 455.0, "width": 5.0, "legs_json": "[]",
        "entry_credit_ps": 2.00, "max_loss": 400.0,
    })
    store.close_position(loss, "2026-06-18", "2026-06-18T14:00:00+00:00",
                         "stop_loss", 1.00, -200.0)

    # An open position so the open-positions table renders.
    store.open_position({
        "client_order_id": "c3", "strategy": "premium", "structure": "put_credit_spread",
        "family": "put", "is_credit": 1, "underlying": "IWM", "status": "open",
        "opened_asof": "2026-06-25", "opened_ts": "2026-06-25T14:00:00+00:00",
        "expiration": "2026-07-18", "contracts": 1, "short_strike": 200.0,
        "long_strike": 195.0, "width": 5.0, "legs_json": "[]",
        "entry_credit_ps": 0.80, "max_loss": 420.0,
    })

    store.append("2026-06-25T14:00:01+00:00", "order_placed", {"underlying": "IWM", "status": "filled"})
    store.append("2026-06-15T14:00:01+00:00", "risk_decision", {"approved": True})
    store.close()


def test_build_pro_self_contained_with_curve_and_winrate():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "portfolio.db"
        _seed(db)

        out = dashboard_pro.build_pro(store_path=db)
        assert out.exists(), "dashboard html was not written"
        assert out.name == "dashboard_pro.html"

        html = out.read_text()

        # Self-contained: no external resources, no runtime fetch.
        assert "fetch(" not in html, "must not call fetch()"
        assert 'src="http' not in html and "src='http" not in html, "no http src"
        assert 'href="http' not in html and "href='http" not in html, "no http href"

        # Inline SVG equity-curve sparkline present.
        assert "<svg" in html and 'class="spark"' in html, "missing equity-curve svg"
        assert "<polyline" in html, "missing equity-curve polyline"

        # Win rate computed from the two seeded closed trades = 50.0.
        assert "50.0" in html, "expected win-rate 50.0 in output"


def test_summary_math():
    closed = [
        {"realized_pnl": 90.0, "exit_reason": "profit_target"},
        {"realized_pnl": -200.0, "exit_reason": "stop_loss"},
        {"realized_pnl": 10.0, "exit_reason": "profit_target"},
    ]
    s = dashboard_pro._closed_summary(closed)
    assert s["count"] == 3
    assert s["wins"] == 2
    assert s["net_pnl"] == -100.0
    assert s["win_rate"] == round(200.0 / 3, 1)
    assert s["by_reason"]["profit_target"] == 2

    curve = dashboard_pro._equity_curve(closed)
    assert [p["cum"] for p in curve] == [90.0, -110.0, -100.0]


if __name__ == "__main__":
    test_build_pro_self_contained_with_curve_and_winrate()
    test_summary_math()
    print("ok")
