"""
Tier 2 scenario-hunt fixes: the chasing stop, half days, and paper provenance.

Each of these is a "bad day, not a disaster" scenario — but the chasing stop and
the half-day cron both reproduce ingredients of real incidents we already had
(unfillable closes; fabricated after-hours marks), so they get the same
regression treatment.
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import types
from pathlib import Path

from core.brokers.base import OrderResult, Position
from core.store import Store


# ───────────────────── escalation ladder (chasing stop) ─────────────────────

def _debit_pos(**kw):
    d = {"client_order_id": "AIS-t", "strategy": "volatility_breakout",
         "structure": "put_debit_spread", "family": "put", "is_credit": 0,
         "underlying": "SPY", "status": "open", "opened_asof": "2026-07-29",
         "opened_ts": "t", "expiration": "2026-08-31", "contracts": 1,
         "short_strike": 735.0, "long_strike": 736.0, "width": 1.0,
         "legs_json": "[]", "entry_credit_ps": 0.31, "max_loss": 31.0}
    d.update(kw)
    return d


def test_escalation_concedes_price_by_attempt():
    """A mid-price DAY limit re-priced once per cycle can chase a falling market
    forever. Each failed attempt now concedes 15% (capped at 45%): selling to
    close a debit accepts LESS; paying to close a credit offers MORE, capped at
    the width."""
    from core.manage import _closing_order

    debit = _debit_pos()
    assert _closing_order(debit, 0.50, attempt=0).limit_price == 0.50
    assert _closing_order(debit, 0.50, attempt=1).limit_price == 0.42  # -15%
    assert _closing_order(debit, 0.50, attempt=3).limit_price == 0.28  # -45%
    assert _closing_order(debit, 0.50, attempt=9).limit_price == 0.28  # capped

    credit = _debit_pos(is_credit=1, structure="put_credit_spread",
                        short_strike=736.0, long_strike=735.0)
    assert _closing_order(credit, 0.50, attempt=0).limit_price == 0.50
    assert _closing_order(credit, 0.50, attempt=2).limit_price == 0.65  # +30%
    assert _closing_order(credit, 0.90, attempt=3).limit_price == 1.00  # width cap


def test_close_limit_never_zero():
    """$0.00 combo limits ('give it away at any price') are commonly rejected;
    one tick is the honest minimum on both sides."""
    from core.manage import _closing_order
    assert _closing_order(_debit_pos(), 0.0, attempt=0).limit_price == 0.01
    credit = _debit_pos(is_credit=1)
    assert _closing_order(credit, 0.0, attempt=0).limit_price == 0.01


def test_close_attempts_counted_from_journal():
    """Attempts must survive process restarts and the orders table's INSERT OR
    REPLACE (which makes re-placements invisible there) — so they are counted
    from close_pending journal events."""
    from core.manage import _close_attempts
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "p.db")
        assert _close_attempts(st, 1) == 0
        st.append("t1", "close_pending", {"position_id": 1, "coid": "CLOSE-x"})
        st.append("t2", "close_pending", {"position_id": 1, "coid": "CLOSE-x"})
        st.append("t3", "close_pending", {"position_id": 2, "coid": "CLOSE-y"})
        assert _close_attempts(st, 1) == 2
        assert _close_attempts(st, 2) == 1     # scoped to the position
        st.close()


def test_manage_places_escalated_close_after_prior_attempts():
    from core.manage import manage_open_positions

    class B:
        name = "ibkr_paper"
        def __init__(self):
            self.placed = []
        def get_positions(self):
            return [Position(symbol="SPY", qty=-1, avg_price=100.0, market_value=0.0,
                             asset_class="option", underlying="SPY",
                             option_expiration="2026-08-31", option_strike=735.0,
                             option_right="P"),
                    Position(symbol="SPY", qty=1, avg_price=100.0, market_value=0.0,
                             asset_class="option", underlying="SPY",
                             option_expiration="2026-08-31", option_strike=736.0,
                             option_right="P")]
        def mark_option(self, u, exp, strike, right, asof=None):
            return {736.0: 0.10, 735.0: 0.02}[strike]     # value 0.08 -> stop loss
        def get_quote(self, symbol):
            return types.SimpleNamespace(mid=700.0)
        def place_order(self, order):
            self.placed.append(order)
            return OrderResult(accepted=True, client_order_id=order.client_order_id,
                               broker_order_id="B1", status="Submitted", filled_qty=0)
        def list_orders(self):
            return []

    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "p.db")
        pid = st.open_position(_debit_pos())
        # One prior failed close on record -> this placement is attempt 1.
        st.append("t1", "close_pending", {"position_id": pid, "coid": "CLOSE-AIS-t"})
        b = B()
        manage_open_positions(b, st, types.SimpleNamespace(strategies={}), "2026-08-05")
        assert len(b.placed) == 1
        # mark 0.08, attempt 1 -> limit 0.08*0.85 = 0.068 -> rounds to 0.07
        assert b.placed[0].limit_price == 0.07
        esc = st.conn.execute(
            "SELECT COUNT(*) FROM journal WHERE kind='close_escalated'").fetchone()[0]
        assert esc == 1
        # The BOOKED intent stays the honest mark, not the conceded limit.
        pend = json.loads(st.get_kv("pending_closes") or "{}")
        assert pend[str(pid)]["exit_value_ps"] == 0.08
        st.close()


# ─────────────────────────── half days ───────────────────────────

def test_early_close_detection():
    from core.market_calendar import early_close_et
    # 2026: Thanksgiving is Thu Nov 26 -> Fri Nov 27 is a 13:00 half day.
    assert early_close_et(dt.date(2026, 11, 27)) == 13
    # Christmas Eve 2026 falls on a Thursday -> half day.
    assert early_close_et(dt.date(2026, 12, 24)) == 13
    # July 3 2026 is a FULL holiday (July 4th is a Saturday) -> the trading-day
    # guard owns it; early-close must NOT double-claim it.
    assert early_close_et(dt.date(2026, 7, 3)) is None
    # 2027: Thanksgiving Thu Nov 25 -> Fri Nov 26 half day.
    assert early_close_et(dt.date(2027, 11, 26)) == 13
    # Ordinary days and weekends: None.
    assert early_close_et(dt.date(2026, 8, 5)) is None
    assert early_close_et(dt.date(2026, 12, 26)) is None   # Saturday


# ───────────────────── paper-fill provenance ─────────────────────

def _seed(store):
    return store.open_position(_debit_pos())


def test_fill_mode_recorded_from_store_mode():
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "p.db")
        pid = _seed(st)
        st.set_kv("mode", "paper")
        st.record_entry_fill(pid, -0.326, 0.016)
        row = st.conn.execute("SELECT fill_mode FROM positions WHERE id=?",
                              (pid,)).fetchone()
        assert row["fill_mode"] == "paper"
        # A later LIVE exit fill must not overwrite the entry's provenance record
        # (COALESCE keeps the first mode; a mixed round-trip would be visible in
        # the journal history if it ever matters).
        st.set_kv("mode", "live")
        st.record_exit_fill(pid, 0.05, 0.01)
        row = st.conn.execute("SELECT fill_mode FROM positions WHERE id=?",
                              (pid,)).fetchone()
        assert row["fill_mode"] == "paper"
        st.close()


def test_slippage_summary_carries_provenance():
    import dashboard_pro
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        st = Store(db)
        pid = _seed(st)
        st.set_kv("mode", "paper")
        st.record_entry_fill(pid, -0.326, 0.016)
        sl = dashboard_pro._slippage(st)
        assert sl["paper_fills"] == 1 and sl["live_fills"] == 0
        st.close()


# ───────────────────── cycle duration observability ─────────────────────

def test_cycle_end_reports_duration():
    from core.brokers.sim import SimAdapter
    from core.config import Config
    from core.execution import run_cycle
    from strategies.premium_harvest import PremiumHarvest

    cfg = Config(
        raw={}, mode="paper",
        account={"type": "margin", "region": "CA", "starting_equity_usd": 30000,
                 "options_approval_level": 3},
        brokers={"execution": "sim"}, risk={},
        strategies={"premium_harvest": {"min_iv_rank": 0.0}},
        watchlist=["SPY"])
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "p.db")
        s = run_cycle(SimAdapter(asof="2026-01-15"),
                      [PremiumHarvest({"min_iv_rank": 0.0})], st, cfg,
                      asof="2026-01-15")
        assert s["duration_secs"] >= 0.0
        row = st.conn.execute("SELECT payload FROM journal WHERE kind='cycle_end' "
                              "ORDER BY id DESC LIMIT 1").fetchone()
        assert "duration_secs" in json.loads(row["payload"])
        st.close()


def test_fx_rates_roundtrip_and_dashboard_payload():
    """The CAD equity view needs the day's real USD/CAD rate. One rate per day,
    idempotent; the dashboard payload carries the series; bad rates refused."""
    import dashboard_pro
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "p.db")
        st.record_fx_rate("2026-08-05", 1.4011)
        st.record_fx_rate("2026-08-05", 1.4015)      # same day: replace
        st.record_fx_rate("2026-08-06", 1.3990)
        st.record_fx_rate("2026-08-07", 0.0)         # refused
        st.record_fx_rate("2026-08-07", -1.0)        # refused
        fx = st.get_fx_rates()
        assert fx == {"2026-08-05": 1.4015, "2026-08-06": 1.399}
        data = dashboard_pro._gather(st)
        assert data["fx_rates"] == fx
        st.close()
