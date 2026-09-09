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

        out = dashboard_pro.build_pro(store_path=db, out_dir=Path(td))
        assert out.exists(), "dashboard html was not written"
        assert out.name == "dashboard_pro.html"

        html = out.read_text(encoding="utf-8")

        # Self-contained: no external resources, no runtime fetch.
        assert "fetch(" not in html, "must not call fetch()"
        assert 'src="http' not in html and "src='http" not in html, "no http src"
        assert 'href="http' not in html and "href='http" not in html, "no http href"

        # Inline SVG equity-curve sparkline present.
        assert "<svg" in html and 'class="spark"' in html, "missing equity-curve svg"
        assert "<polyline" in html, "missing equity-curve polyline"

        # Win rate computed from the two seeded closed trades = 50.0.
        assert "50.0" in html, "expected win-rate 50.0 in output"

        # Honest empty-state sections are present (rendered from inlined JS).
        assert "IV-rank bootstrap" in html, "missing IV bootstrap section"
        assert "Graduation gate" in html, "missing graduation gate section"
        # The bootstrap banner is now CONDITIONAL: it names only the symbols that
        # actually lack a benchmark rank. It must NOT claim the whole system is
        # waiting — SPY/QQQ/IWM/DIA use real 252-day Cboe history and can trade.
        assert "still bootstrapping IV rank" in html or "IV-rank bootstrap" in html


def test_bootstrap_banner_names_only_the_waiting_symbols():
    """REGRESSION: the banner used to say the whole system was bootstrapping and
    that "no signals yet" were possible — which became flatly untrue once
    SPY/QQQ/IWM/DIA got real 252-day Cboe ranks and the system placed its first
    trade. A dashboard that misdescribes the system is worse than no dashboard."""
    import json, tempfile
    from pathlib import Path
    import dashboard_pro as dp
    from core.store import Store

    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        store = Store(db)
        store.record_iv_snapshot("XLF", "2026-07-29", 0.22)
        store.set_kv("cycle_decisions", json.dumps([
            {"symbol": "SPY", "iv_rank": 0.27, "iv_source": "cboe:VIX",
             "iv_obs": 252, "chain": 574, "signals": 1, "note": "signal generated"},
            {"symbol": "XLF", "iv_rank": None, "iv_source": "bootstrap",
             "iv_obs": 20, "chain": 320, "signals": 0, "note": "no trusted IV rank yet"}]))
        store.close()
        html = dp.build_pro(store_path=db, out_dir=Path(td)).read_text(encoding="utf-8")

    # The banner text is assembled client-side, so assert on the two things that
    # ARE statically checkable: the payload the JS filters, and the template.
    import json as _j
    # raw_decode reads the first complete JSON object and ignores the JS after it.
    payload, _ = _j.JSONDecoder().raw_decode(html.split("var D = ", 1)[1])
    ranks = {d["symbol"]: d["iv_rank"] for d in payload["decisions"]}
    assert ranks["XLF"] is None, "XLF must be flagged as having no trusted rank"
    assert ranks["SPY"] == 0.27, "SPY must carry its real benchmark rank"
    # The banner filters on exactly that null-rank condition...
    assert "still bootstrapping IV rank" in html
    assert "d.iv_rank==null" in html, "banner must filter to unranked symbols only"
    # ...and the old blanket claim is gone for good.
    assert "no signals yet" not in html, "must not claim signals are impossible"


def test_iv_and_graduation_helpers():
    # IV bootstrap counts DISTINCT session-days, not raw snapshots.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        store = Store(Path(td) / "p.db")
        store.record_iv_snapshot("SPY", "2026-06-01", 0.2)
        store.record_iv_snapshot("QQQ", "2026-06-01", 0.3)   # same day
        store.record_iv_snapshot("SPY", "2026-06-02", 0.25)  # new day
        prog = dashboard_pro._iv_progress(store, target=20)
        assert prog["days"] == 2 and prog["target"] == 20
        store.close()

    g = dashboard_pro._graduation(
        [{"realized_pnl": 90.0, "closed_asof": "2026-06-15",
          "structure": "put_credit_spread", "contracts": 1, "exit_reason": "profit_target"},
         {"realized_pnl": -200.0, "closed_asof": "2026-06-18",
          "structure": "put_credit_spread", "contracts": 1, "exit_reason": "stop_loss"}])
    assert g["trades"] == 2 and g["days"] == 2 and g["min_trades"] == 40
    assert g["eligible"] is False
    # Costs MUST be subtracted now: the gross expectancy is -55.00, so a
    # cost-aware net expectancy has to be strictly worse. The dashboard used to
    # report the gross number, which is how it could show "eligible" while the
    # real gate said no.
    assert g["gross"] == -110.0 and g["costs"] > 0
    assert g["expectancy"] < -55.0
    # The gate's EVIDENCE quality has to travel with its verdict. Without these
    # the dashboard shows a cost figure with no way to tell measured fills from
    # a model's guess -- and the modeled part is the optimistic part.
    assert "cost_measured_pct" in g and "cost_per_trade" in g
    assert g["cost_per_trade"] > 0


def test_graduation_card_shows_cost_evidence_and_session_meaning():
    """The three hurdles must each be legible on the PAGE, not just in the CLI.

    'Paper record' counts trading sessions since the first trade; a reader who
    thinks it counts closing days will read 2/60 and conclude the system has
    barely run. And a cost number with no provenance invites trusting a model."""
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "portfolio.db"
        _seed(db)
        html = dashboard_pro.build_pro(
            store_path=db, out_dir=Path(td)).read_text(encoding="utf-8")
    assert "MEASURED fills" in html
    assert "cost_measured_pct" in html and "cost_per_trade" in html
    assert "Trading sessions since the FIRST trade" in html
    assert "All THREE must pass independently" in html


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


def test_rendered_page_javascript_actually_parses():
    """REGRESSION: a one-line caveat edit shipped an unescaped apostrophe inside
    a single-quoted JS string — a syntax error that silently killed the ENTIRE
    page script (the dashboard fell back to its static skeleton) while every
    Python test stayed green, because Python tests never parse the JS. Node does.
    Skips silently where node is unavailable (the VM does not need it)."""
    import shutil, subprocess, tempfile
    node = shutil.which("node")
    if not node:
        return                                      # environment without node
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        _seed(db)
        html = dashboard_pro.build_pro(store_path=db, out_dir=Path(td)).read_text(encoding="utf-8")
        js = html.split("<script>")[1].split("</script>")[0]
        jsf = Path(td) / "page.js"
        jsf.write_text(js, encoding="utf-8")
        r = subprocess.run([node, "--check", str(jsf)], capture_output=True, text=True)
        assert r.returncode == 0, f"rendered page JS does not parse:\n{r.stderr[:800]}"



def test_advanced_cards_render_from_journal_data():
    """Road to November (projected dates + checklist), the cycle-health calendar,
    and the IV-rank-vs-threshold chart are built only from data already in the
    store, and the page must carry them."""
    from datetime import datetime, timedelta, timezone
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "portfolio.db"
        _seed(db)
        store = Store(db)
        now = datetime.now(timezone.utc)
        for i in range(3):
            ts = (now - timedelta(days=i)).replace(hour=14, minute=0)
            store.append(ts.isoformat(), "cycle_start", {"asof": ts.date().isoformat(), "broker": "ibkr_paper"})
            store.append((ts + timedelta(minutes=1)).isoformat(), "iv_rank_source",
                         {"symbol": "SPY", "source": "cboe:VIX", "rank": 0.1 + 0.1 * i, "observations": 252})
            store.append((ts + timedelta(minutes=6)).isoformat(), "cycle_end",
                         {"placed": 0, "rejected": 0, "reconcile_ok": True, "duration_secs": 300,
                          "ib_notices": {"10091": 2330}})
        store.close()
        out = dashboard_pro.build_pro(store_path=db, out_dir=Path(td))
        html = out.read_text(encoding="utf-8")
    import json as _json, re as _re
    payload = _json.loads(_re.search(r"^var D = (\{.*\});\s*$", html, _re.M).group(1).replace("<\\/", "</"))
    road = payload["road"]
    assert road["window_start"] == "2026-11-02" and road["sessions_to_window"] >= 0
    assert road["iv_need"] == 60 and len(road["manual_steps"]) == 4
    cal = payload["cycle_calendar"]
    assert cal["days"] and any(d["ends"] == 1 for d in cal["days"])
    assert payload["ivr_series"]["SPY"] and payload["ivr_threshold"] == 0.4
    # The IVR title is assembled at runtime from the threshold, so match its
    # literal prefix; the other two are literal strings in the page script.
    for title in ("Road to November", "Cycle health", "IV rank against the "):
        assert title in html, title
