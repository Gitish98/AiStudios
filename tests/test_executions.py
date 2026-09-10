"""Regression tests for fill measurement from the broker's EXECUTION REPORT.

Found 2026-09-09, the day a 10-contract close filled 6 and cancelled: IB's
order feed reported `Cancelled, filled 0.0` (session-scoped), and the partial-
close finalizer — believing the price unrecoverable — would have resized the
remainder and DISCARDED six contracts of realized P&L from the record. But
`reqExecutions()` is ACCOUNT-scoped for the day: all nine fills were there,
with prices, under another clientId. This locks in: the netting helper, the
store split, both finalizers' fallbacks, and the snapshot that survives IB's
nightly reset.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from core.brokers.base import Execution, OrderResult, Position
from core.fills import combo_fill_from_executions
from core.store import Store

COID = "CLOSE-AIS-f126e838322cf15299d9"
ENTRY_COID = "AIS-f126e838322cf15299d9"


def _bag(qty, price, coid=COID, xid=None, t="2026-09-09T15:38:45+00:00"):
    return Execution(exec_id=xid or f"x{qty}{price}{coid[-4:]}", time=t, local_symbol="SPY",
                     sec_type="BAG", side="BOT", qty=qty, price=price, order_id="12246",
                     client_order_id=coid, client_id=7)


def _leg(qty, price, sym, side, coid=COID, xid=None):
    return Execution(exec_id=xid or f"l{sym}{qty}", time="2026-09-09T15:38:45+00:00",
                     local_symbol=sym, sec_type="OPT", side=side, qty=qty, price=price,
                     order_id="12246", client_order_id=coid, client_id=7)


def _spy_leg_positions(qty=4):
    return [
        Position(symbol="SPY   260930P00762000", qty=-qty, avg_price=884.54, market_value=0.0,
                 asset_class="option", underlying="SPY", option_expiration="2026-09-30",
                 option_strike=762.0, option_right="P"),
        Position(symbol="SPY   260930P00763000", qty=+qty, avg_price=921.44, market_value=0.0,
                 asset_class="option", underlying="SPY", option_expiration="2026-09-30",
                 option_strike=763.0, option_right="P"),
    ]


class _Adapter:
    """Real dataclasses in, so the fakes cannot drift from the broker types."""
    name = "ibkr_paper"

    def __init__(self, positions=(), orders=(), execs=()):
        self._p, self._o, self._e = list(positions), list(orders), list(execs)

    def get_positions(self):
        return self._p

    def list_orders(self):
        return self._o

    def get_executions(self):
        return self._e

    def cancel_order(self, _):
        return True


def _seed_position(store, *, status="open", contracts=10, coid=ENTRY_COID,
                   entry_fill_ps=-0.369, entry_slip_ps=0.009, fill_mode="paper"):
    row = {"client_order_id": coid, "strategy": "breakout", "structure": "put_debit_spread",
           "family": "put", "is_credit": 0, "underlying": "SPY", "status": status,
           "opened_asof": "2026-08-31", "opened_ts": "2026-08-31T14:01:49+00:00",
           "expiration": "2026-09-30", "contracts": contracts, "short_strike": 762.0,
           "long_strike": 763.0, "width": 1.0, "entry_credit_ps": 0.36, "max_loss": 36.0 * contracts,
           "entry_fill_ps": entry_fill_ps, "entry_slip_ps": entry_slip_ps, "fill_mode": fill_mode}
    cols = ", ".join(row); qs = ", ".join("?" for _ in row)
    cur = store.conn.execute(f"INSERT INTO positions ({cols}) VALUES ({qs})", tuple(row.values()))
    store.conn.commit()
    return cur.lastrowid


def _order_row(store, coid, broker_order_id="12246", ts="2026-09-09T14:00:25+00:00"):
    """The orders row record_order() writes at placement. _executions_for scopes
    executions to THIS attempt via its broker_order_id (IB orderId)."""
    store.conn.execute(
        "INSERT OR REPLACE INTO orders (client_order_id, ts, broker_order_id, status, strategy, "
        "underlying, filled_qty, filled_avg_price, payload) VALUES (?,?,?,?,?,?,?,?,?)",
        (coid, ts, broker_order_id, "PendingSubmit", "close", "SPY", 0.0, 0.0, "{}"))
    store.conn.commit()


def _pending_close(store, pid, *, reason="dte_manage", exit_value_ps=0.39, realized_pnl=30.0):
    store.set_kv("pending_closes", json.dumps({str(pid): {
        "coid": COID, "reason": reason, "exit_value_ps": exit_value_ps,
        "realized_pnl": realized_pnl}}))


# ───────────────────────────── the netting helper ─────────────────────────────

def test_combo_fill_nets_only_the_bag_rows_for_that_order():
    """2 + 3 + 1 contracts at -0.39 each — the real 2026-09-09 fills. Leg rows
    and other orders' rows are ignored."""
    execs = [_bag(2, -0.39), _bag(3, -0.39, xid="b"), _bag(1, -0.39, xid="c"),
             _leg(2, 10.13, "SPY 763P", "SLD"), _leg(2, 9.74, "SPY 762P", "BOT"),
             _bag(5, -0.21, coid="AIS-other", xid="z")]
    assert combo_fill_from_executions(execs, COID) == (6.0, -0.39)


def test_combo_fill_is_quantity_weighted():
    assert combo_fill_from_executions([_bag(1, -0.40, xid="a"), _bag(3, -0.36, xid="b")], COID) == (4.0, -0.37)


def test_combo_fill_absent_is_none_never_zero():
    assert combo_fill_from_executions([], COID) is None
    assert combo_fill_from_executions([_leg(2, 10.0, "SPY 763P", "SLD")], COID) is None
    assert combo_fill_from_executions([_bag(2, float("nan"))], COID) is None


# ───────────────────────────── the store split ─────────────────────────────

def test_split_books_closed_units_and_shrinks_the_remainder():
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db")
        pid = _seed_position(s, contracts=10)
        new_id = s.split_closed_units(pid, 6, "2026-09-10", "2026-09-10T14:06:00+00:00",
                                      "dte_manage", 0.39, 18.0, exit_fill_ps=0.39, exit_slip_ps=0.0)
        orig = dict(s.conn.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone())
        new = dict(s.conn.execute("SELECT * FROM positions WHERE id=?", (new_id,)).fetchone())
        bad = (s.split_closed_units(pid, 0, "d", "t", "r", 0.1, 0.0),
               s.split_closed_units(pid, 4, "d", "t", "r", 0.1, 0.0))   # 4 == remaining total
        s.close()
        assert orig["contracts"] == 4 and orig["max_loss"] == 144.0 and orig["status"] == "open"
        assert new["contracts"] == 6 and new["max_loss"] == 216.0 and new["status"] == "closed"
        assert new["exit_fill_ps"] == 0.39 and new["realized_pnl"] == 18.0
        assert new["entry_fill_ps"] == -0.369          # entry measurement carried over
        assert new["client_order_id"] == ENTRY_COID + "#part6"
        assert bad == (None, None)


# ───────────────────────────── the partial-close finalizer ─────────────────────────────

def test_partial_close_is_booked_at_the_measured_price():
    """The 2026-09-09 scenario end to end: close of 10 filled 6 and vanished;
    broker holds 4/4; the execution report has three BAG fills at -0.39."""
    from core.manage import _finalize_pending_closes
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db")
        pid = _seed_position(s, contracts=10); _order_row(s, COID)
        _pending_close(s, pid, realized_pnl=30.0)
        a = _Adapter(positions=_spy_leg_positions(4), orders=[],
                     execs=[_bag(2, -0.39, xid="a"), _bag(3, -0.39, xid="b"), _bag(1, -0.39, xid="c")])
        pend = _finalize_pending_closes(a, s, "2026-09-10")
        orig = dict(s.conn.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone())
        closed = [dict(r) for r in s.conn.execute("SELECT * FROM positions WHERE status='closed'")]
        kinds = [r[0] for r in s.conn.execute("SELECT kind FROM journal")]
        s.close()
        assert pend == {}
        assert orig["contracts"] == 4 and orig["status"] == "open"
        assert len(closed) == 1
        c = closed[0]
        assert c["contracts"] == 6 and c["exit_reason"] == "dte_manage"
        assert c["exit_fill_ps"] == 0.39 and c["exit_slip_ps"] == 0.0     # sold at the mark, exactly
        assert c["realized_pnl"] == 18.0                                  # 30.0 for 10 -> 3.0 x 6
        assert "partial_close_booked" in kinds and "partial_close_adopted" not in kinds


def test_partial_close_is_not_booked_when_the_report_disagrees_with_the_book():
    """Executions say 5 closed; the broker holds 4 of 10 (so 6 closed). The
    broker's book is truth — do not invent, resize and say so."""
    from core.manage import _finalize_pending_closes
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db")
        pid = _seed_position(s, contracts=10); _order_row(s, COID)
        _pending_close(s, pid)
        a = _Adapter(positions=_spy_leg_positions(4), orders=[], execs=[_bag(5, -0.39, xid="a")])
        _finalize_pending_closes(a, s, "2026-09-10")
        orig = dict(s.conn.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone())
        n_closed = s.conn.execute("SELECT COUNT(*) FROM positions WHERE status='closed'").fetchone()[0]
        rows = [(r[0], json.loads(r[1])) for r in s.conn.execute(
            "SELECT kind, payload FROM journal WHERE kind LIKE 'partial_close%'")]
        s.close()
        assert orig["contracts"] == 4 and n_closed == 0
        assert rows and rows[0][0] == "partial_close_adopted"
        assert rows[0][1]["executions_qty"] == 5.0


def test_partial_close_without_any_report_keeps_the_old_honest_behaviour():
    from core.manage import _finalize_pending_closes
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db")
        pid = _seed_position(s, contracts=10); _order_row(s, COID)
        _pending_close(s, pid)
        _finalize_pending_closes(_Adapter(positions=_spy_leg_positions(4), orders=[], execs=[]), s, "2026-09-10")
        orig = dict(s.conn.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone())
        n_closed = s.conn.execute("SELECT COUNT(*) FROM positions WHERE status='closed'").fetchone()[0]
        kinds = [r[0] for r in s.conn.execute("SELECT kind FROM journal")]
        s.close()
        assert orig["contracts"] == 4 and n_closed == 0
        assert "partial_close_adopted" in kinds


def test_snapshot_journal_survives_the_nightly_reset():
    """The live report is day-scoped. A snapshot journaled the day of the fill
    — in EITHER shape (the hand-journaled `order_ref` one or the per-cycle
    `client_order_id` one) — must still book it the next morning."""
    from core.manage import _finalize_pending_closes
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db")
        pid = _seed_position(s, contracts=10); _order_row(s, COID)
        _pending_close(s, pid, realized_pnl=30.0)
        s.append("2026-09-09T22:00:00+00:00", "executions_snapshot", {
            "source": "reqExecutions(ExecutionFilter())", "count": 3, "fills": [
                {"time": "t", "local_symbol": "SPY", "sec_type": "BAG", "side": "BOT", "qty": 2.0,
                 "price": -0.39, "order_id": 12246, "client_id": 7, "order_ref": COID, "exec_id": "e1"},
                "this is not a dict and must be skipped, not crash the cycle",
                {"time": "t", "local_symbol": "SPY", "sec_type": "BAG", "side": "BOT", "qty": 9.0,
                 "price": None, "order_id": 12246, "client_id": 7, "order_ref": COID, "exec_id": "hole"},
                {"time": "t", "local_symbol": "SPY", "sec_type": "BAG", "side": "BOT", "qty": 3.0,
                 "price": -0.39, "order_id": 12246, "client_id": 7, "order_ref": COID, "exec_id": "e2"},
                {"time": "t", "local_symbol": "SPY", "sec_type": "BAG", "side": "BOT", "qty": 1.0,
                 "price": -0.39, "order_id": 12246, "client_id": 7, "client_order_id": COID, "exec_id": "e3"},
            ]})
        # live report empty (reset happened); snapshot must carry it
        _finalize_pending_closes(_Adapter(positions=_spy_leg_positions(4), orders=[], execs=[]), s, "2026-09-10")
        closed = [dict(r) for r in s.conn.execute("SELECT * FROM positions WHERE status='closed'")]
        s.close()
        assert len(closed) == 1 and closed[0]["contracts"] == 6 and closed[0]["exit_fill_ps"] == 0.39


def test_executions_for_dedups_live_and_snapshot_by_exec_id():
    from core.manage import _executions_for
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db")
        _order_row(s, COID)
        s.append("2026-09-09T22:00:00+00:00", "executions_snapshot", {"fills": [
            {"sec_type": "BAG", "qty": 2.0, "price": -0.39, "order_ref": COID, "exec_id": "dup", "order_id": 12246},
            {"sec_type": "BAG", "qty": 1.0, "price": -0.39, "order_ref": COID, "exec_id": "only-snap", "order_id": 12246},
        ]})
        got = _executions_for(_Adapter(execs=[_bag(2, -0.39, xid="dup")]), s, COID)
        s.close()
        assert sorted(e.exec_id for e in got) == ["dup", "only-snap"]


# ───────────────────────────── full close + entry paths ─────────────────────────────

def test_full_close_reported_filled_at_zero_takes_the_price_from_the_report():
    from core.manage import _finalize_pending_closes
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db")
        pid = _seed_position(s, contracts=10); _order_row(s, COID)
        _pending_close(s, pid, exit_value_ps=0.39, realized_pnl=30.0)
        a = _Adapter(positions=[], orders=[OrderResult(accepted=True, client_order_id=COID,
                                                       broker_order_id="12246", status="Filled",
                                                       filled_qty=0.0, filled_avg_price=0.0)],
                     execs=[_bag(10, -0.40, xid="a")])
        _finalize_pending_closes(a, s, "2026-09-10")
        row = dict(s.conn.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone())
        ev = json.loads(s.conn.execute("SELECT payload FROM journal WHERE kind='position_closed'").fetchone()[0])
        s.close()
        assert row["status"] == "closed" and row["exit_fill_ps"] == 0.40
        assert row["exit_slip_ps"] == -0.01                 # received 0.40 vs 0.39 intended: favourable
        assert ev["via"] == "pending_fill+executions"


def test_entry_reported_filled_at_zero_takes_the_price_from_the_report():
    from core.execution import _finalize_pending_entries
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db")
        pid = _seed_position(s, status="pending", contracts=10, entry_fill_ps=None, entry_slip_ps=None)
        _order_row(s, ENTRY_COID, broker_order_id="12246")
        a = _Adapter(positions=_spy_leg_positions(10),
                     orders=[OrderResult(accepted=True, client_order_id=ENTRY_COID, broker_order_id="12235",
                                         status="Filled", filled_qty=0.0, filled_avg_price=0.0)],
                     execs=[_bag(10, 0.369, coid=ENTRY_COID, xid="e")])   # a DEBIT: IB price positive
        _finalize_pending_entries(a, s)
        row = dict(s.conn.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone())
        ev = json.loads(s.conn.execute("SELECT payload FROM journal WHERE kind='entry_filled'").fetchone()[0])
        s.close()
        assert row["status"] == "open" and row["entry_fill_ps"] == -0.369 and row["entry_slip_ps"] == 0.009
        assert ev["via"] == "order_feed+executions"


def test_vanished_entry_prefers_the_report_over_avg_cost_reconstruction():
    from core.execution import _finalize_pending_entries
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db")
        pid = _seed_position(s, status="pending", contracts=10, entry_fill_ps=None, entry_slip_ps=None)
        _order_row(s, ENTRY_COID, broker_order_id="12246")
        a = _Adapter(positions=_spy_leg_positions(10), orders=[],           # order gone from the feed
                     execs=[_bag(10, 0.371, coid=ENTRY_COID, xid="e")])
        _finalize_pending_entries(a, s)
        row = dict(s.conn.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone())
        ev = json.loads(s.conn.execute("SELECT payload FROM journal WHERE kind='entry_filled'").fetchone()[0])
        s.close()
        assert row["entry_fill_ps"] == -0.371 and ev["via"] == "executions"


def test_default_adapter_reports_no_executions_not_fake_ones():
    """A broker with no execution report must say so with an empty list — the
    base-class default — never fabricate one."""
    from core.brokers import sim as _sim
    cls = getattr(_sim, "SimAdapter")
    try:
        inst = cls()
    except TypeError:
        inst = cls.__new__(cls)      # default get_executions needs no state
    assert inst.get_executions() == []



# ───────────────────── the preflight review's fifteen (all confirmed) ─────────────────────

def test_executions_are_scoped_to_the_attempt_not_the_reused_coid():
    """A close's coid is reused on every re-placement. The 4-lot remainder's
    re-close (IB orderId 12260) must be priced off ITS fills only — never
    blended with the earlier 6 @ -0.39 under the same orderRef."""
    from core.manage import _executions_for
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db")
        _order_row(s, COID, broker_order_id="12260", ts="2026-09-10T14:00:25+00:00")
        old = [_bag(2, -0.39, xid="o1"), _bag(3, -0.39, xid="o2"), _bag(1, -0.39, xid="o3")]  # order_id 12246
        new = Execution(exec_id="n1", time="2026-09-10T15:00:00+00:00", local_symbol="SPY", sec_type="BAG",
                        side="BOT", qty=4, price=-0.35, order_id="12260", client_order_id=COID, client_id=7)
        got = _executions_for(_Adapter(execs=old + [new]), s, COID)
        s.close()
        assert [e.exec_id for e in got] == ["n1"]
        assert combo_fill_from_executions(got, COID) == (4.0, -0.35)


def test_without_an_orders_row_executions_report_absence():
    from core.manage import _executions_for
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db")
        got = _executions_for(_Adapter(execs=[_bag(6, -0.39)]), s, COID)
        s.close()
        assert got == []


def test_time_fallback_when_no_broker_order_id_was_recorded():
    from core.manage import _executions_for
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db")
        _order_row(s, COID, broker_order_id="", ts="2026-09-10T14:00:25+00:00")
        before = Execution(exec_id="b", time="2026-09-09T15:38:45+00:00", local_symbol="SPY", sec_type="BAG",
                           side="BOT", qty=6, price=-0.39, order_id="", client_order_id=COID, client_id=7)
        after = Execution(exec_id="a", time="2026-09-10T15:00:00+00:00", local_symbol="SPY", sec_type="BAG",
                          side="BOT", qty=4, price=-0.35, order_id="", client_order_id=COID, client_id=7)
        got = _executions_for(_Adapter(execs=[before, after]), s, COID)
        s.close()
        assert [e.exec_id for e in got] == ["a"]


def test_a_dead_close_with_partial_legs_is_resolved_by_the_book():
    """TOMORROW MORNING. The feed reports the close `Cancelled, filled 0.0`
    (it filled 6 of 10, then the DAY order expired). The dead-order branch used
    to release the row at 10 and book nothing — and the next pass would have
    sent a 10-lot close against 4 held legs, OPENING six reverse spreads."""
    from core.manage import _finalize_pending_closes
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db")
        pid = _seed_position(s, contracts=10); _order_row(s, COID)
        _pending_close(s, pid, realized_pnl=30.0)
        dead = OrderResult(accepted=True, client_order_id=COID, broker_order_id="12246",
                           status="Cancelled", filled_qty=0.0, filled_avg_price=0.0)
        a = _Adapter(positions=_spy_leg_positions(4), orders=[dead],
                     execs=[_bag(2, -0.39, xid="a"), _bag(3, -0.39, xid="b"), _bag(1, -0.39, xid="c")])
        pend = _finalize_pending_closes(a, s, "2026-09-10")
        orig = dict(s.conn.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone())
        closed = [dict(r) for r in s.conn.execute("SELECT * FROM positions WHERE status='closed'")]
        s.close()
        assert pend == {}
        assert orig["contracts"] == 4 and orig["status"] == "open"
        assert len(closed) == 1 and closed[0]["contracts"] == 6 and closed[0]["exit_fill_ps"] == 0.39


def test_a_dead_close_with_every_leg_still_held_is_simply_dead():
    from core.manage import _finalize_pending_closes
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db")
        pid = _seed_position(s, contracts=10); _order_row(s, COID)
        _pending_close(s, pid)
        dead = OrderResult(accepted=True, client_order_id=COID, broker_order_id="12246",
                           status="Cancelled", filled_qty=0.0, filled_avg_price=0.0)
        pend = _finalize_pending_closes(_Adapter(positions=_spy_leg_positions(10), orders=[dead], execs=[]), s, "2026-09-10")
        orig = dict(s.conn.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone())
        n_closed = s.conn.execute("SELECT COUNT(*) FROM positions WHERE status='closed'").fetchone()[0]
        s.close()
        assert pend == {} and orig["contracts"] == 10 and n_closed == 0


def test_a_zero_price_combo_row_refuses_the_whole_answer():
    """0.0 is IB's 'unpopulated'. One such row diluting the average would be
    booked as measured. Refuse rather than blend."""
    assert combo_fill_from_executions([_bag(2, -0.39, xid="a"), _bag(3, 0.0, xid="b")], COID) is None


def test_zero_slippage_is_a_measurement_not_absence():
    """Every perfect fill in the record was being charged MODELED slippage while
    the journal called it measured."""
    from core.fills import plausible_slip_ps
    assert plausible_slip_ps(0.0, 1.0) is True
    assert plausible_slip_ps(0, 1.0) is True
    assert plausible_slip_ps(None, 1.0) is False
    assert plausible_slip_ps(0.009, 1.0) is True
    assert plausible_slip_ps(1.5, 1.0) is False


def test_partial_rows_count_as_one_trade_for_graduation():
    from core.performance import compute_metrics
    rows = [
        {"client_order_id": "AIS-x#part6", "realized_pnl": 180.0, "structure": "put_debit_spread",
         "contracts": 6, "exit_reason": "dte_manage", "closed_asof": "2026-09-10", "width": 1.0},
        {"client_order_id": "AIS-x", "realized_pnl": 120.0, "structure": "put_debit_spread",
         "contracts": 4, "exit_reason": "dte_manage", "closed_asof": "2026-09-11", "width": 1.0},
        {"client_order_id": "AIS-y", "realized_pnl": -200.0, "structure": "put_credit_spread",
         "contracts": 1, "exit_reason": "stop_loss", "closed_asof": "2026-09-12", "width": 1.0},
    ]
    m = compute_metrics(rows, ["2026-09-10", "2026-09-11", "2026-09-12"])
    assert m["trades"] == 2
    assert m["wins"] == 1 and m["losses"] == 1


def test_audit_makes_an_unbooked_partial_close_loud():
    from core.selfaudit import _check_unbooked_partial_closes
    from datetime import datetime, timezone
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db")
        s.append(datetime.now(timezone.utc).isoformat(), "partial_close_adopted",
                 {"position_id": 5, "was": 10, "remaining": 4, "executions_qty": None})
        found = _check_unbooked_partial_closes(s)
        s.close()
    assert found and found[0].severity == "high" and "6 contract" in found[0].summary


def test_unreadable_report_is_journaled_not_read_as_empty():
    from core.manage import _executions_for
    class Boom(_Adapter):
        def get_executions(self):
            raise RuntimeError("gateway restarting")
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db")
        _order_row(s, COID)
        got = _executions_for(Boom(), s, COID)
        kinds = [r[0] for r in s.conn.execute("SELECT kind FROM journal")]
        s.close()
        assert got == [] and "executions_unreadable" in kinds



# ───────────────────── second-round review: the seven ─────────────────────

def test_feed_collision_prefers_the_current_attempt_and_does_not_release_a_live_close():
    """Day 2, 15:30: the feed lists BOTH yesterday's dead 12246 and today's
    working 12250 under one reused coid. Last-wins picked the dead one,
    released the live close, and the next pass placed a third."""
    from core.manage import _orders_by_attempt, _finalize_pending_closes
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db")
        pid = _seed_position(s, contracts=4)                       # already resized
        _order_row(s, COID, broker_order_id="12250", ts="2026-09-10T14:00:25+00:00")
        _pending_close(s, pid, realized_pnl=12.0)
        dead = OrderResult(accepted=True, client_order_id=COID, broker_order_id="12246",
                           status="Cancelled", filled_qty=0.0, filled_avg_price=0.0)
        live = OrderResult(accepted=True, client_order_id=COID, broker_order_id="12250",
                           status="Submitted", filled_qty=0.0, filled_avg_price=0.0)
        chosen = _orders_by_attempt(s, [live, dead])[COID]
        assert chosen.broker_order_id == "12250"
        pend = _finalize_pending_closes(_Adapter(positions=_spy_leg_positions(4), orders=[live, dead],
                                                 execs=[]), s, "2026-09-10")
        orig = dict(s.conn.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone())
        status = s.conn.execute("SELECT status FROM orders WHERE client_order_id=?", (COID,)).fetchone()[0]
        s.close()
        assert str(pid) in pend, "the live close must stay pending"
        assert orig["contracts"] == 4 and status != "canceled"


def test_orders_by_attempt_prefers_a_live_row_when_no_attempt_is_recorded():
    from core.manage import _orders_by_attempt
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db")
        dead = OrderResult(accepted=True, client_order_id=COID, broker_order_id="1", status="Cancelled")
        live = OrderResult(accepted=True, client_order_id=COID, broker_order_id="2", status="Submitted")
        assert _orders_by_attempt(s, [live, dead])[COID].broker_order_id == "2"
        assert _orders_by_attempt(s, [dead, live])[COID].broker_order_id == "2"
        s.close()


def test_sim_fills_are_stamped_sim_and_never_count_as_evidence():
    """The simulator fills at the limit by construction: its 0.0 slip is a fact
    about the simulator. Counting it made the cost-aware backtest cost-blind."""
    from core.costs import CostModel
    from core.performance import compute_metrics
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db")
        s.set_kv("broker", "sim"); s.set_kv("mode", "paper")
        pid = _seed_position(s, contracts=1, entry_fill_ps=None, entry_slip_ps=None, fill_mode=None)
        s.record_entry_fill(pid, -0.36, 0.0)          # COALESCE fills the mode in
        row = dict(s.conn.execute("SELECT fill_mode, entry_slip_ps FROM positions WHERE id=?", (pid,)).fetchone())
        s.close()
    assert row["fill_mode"] == "sim" and row["entry_slip_ps"] == 0.0
    c = CostModel()
    sim_row = {"structure": "put_credit_spread", "contracts": 1, "width": 1.0, "fill_mode": "sim",
               "entry_slip_ps": 0.0, "exit_slip_ps": 0.0, "realized_pnl": 20.0,
               "exit_reason": "profit_target", "closed_asof": "2026-09-10", "client_order_id": "a"}
    real_row = dict(sim_row, fill_mode="paper", client_order_id="b")
    m_sim = compute_metrics([sim_row], ["2026-09-10"], c)
    m_real = compute_metrics([real_row], ["2026-09-10"], c)
    assert c.measured_dollars(sim_row, "profit_target")[0] == 0.0
    assert m_sim["cost_measured_pct"] == 0.0
    assert m_real["cost_measured_pct"] > 0.0
    assert m_sim["total_costs"] > m_real["total_costs"]     # sim pays the MODEL


def test_audit_exit_codes_by_degree():
    """3 pages, 2 rides, 0 is quiet. Paging on HIGH for a week masks the real
    failure inside that week."""
    import io as _io, contextlib, argparse
    import cli
    from unittest import mock
    from core.selfaudit import Finding
    def run(findings):
        with mock.patch("core.selfaudit.run_self_audit", return_value=findings):
            buf = _io.StringIO()
            with contextlib.redirect_stdout(buf):
                return cli.cmd_audit(argparse.Namespace(brief=True))
    assert run([]) == 0
    assert run([Finding("x", "medium", "m")]) == 0
    assert run([Finding("x", "high", "h")]) == 2
    assert run([Finding("x", "critical", "c")]) == 3


def test_wrapper_pages_on_critical_only():
    src = (Path(__file__).resolve().parents[1] / "scripts" / "run_cycle.sh").read_text(encoding="utf-8")
    assert '"${AUDIT_RC:-0}" -ge 3' in src and '-ge 2 ]' not in src


def test_unbooked_partial_is_booked_late_once_the_report_has_it():
    """The audit remedy promised a retry; now there is one. Day 1 adopted
    without a price (row resized 10->4); a later snapshot carries the six
    fills; the next cycle books them, once."""
    from core.manage import _retry_unbooked_partials
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db")
        pid = _seed_position(s, contracts=4)                       # already resized
        _order_row(s, COID)
        s.append("2026-09-10T14:06:00+00:00", "partial_close_adopted", {
            "position_id": pid, "was": 10, "remaining": 4, "coid": COID, "reason": "dte_manage",
            "exit_value_ps": 0.39, "realized_pnl_total": 30.0, "executions_qty": None})
        a = _Adapter(positions=_spy_leg_positions(4), orders=[],
                     execs=[_bag(2, -0.39, xid="a"), _bag(3, -0.39, xid="b"), _bag(1, -0.39, xid="c")])
        _retry_unbooked_partials(a, s, "2026-09-10")
        _retry_unbooked_partials(a, s, "2026-09-10")               # idempotent
        closed = [dict(r) for r in s.conn.execute("SELECT * FROM positions WHERE status='closed'")]
        orig = dict(s.conn.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone())
        booked = [r[0] for r in s.conn.execute("SELECT kind FROM journal WHERE kind='partial_close_booked'")]
        s.close()
        assert len(closed) == 1 and closed[0]["contracts"] == 6
        assert closed[0]["exit_fill_ps"] == 0.39 and closed[0]["realized_pnl"] == 18.0
        assert closed[0]["max_loss"] == 216.0                      # 36/contract, from the remainder's rate
        assert orig["contracts"] == 4                              # untouched
        assert len(booked) == 1


def test_dashboard_summary_groups_partials_like_the_metrics():
    import dashboard_pro
    rows = [{"client_order_id": "AIS-x#part6", "realized_pnl": 18.0, "exit_reason": "dte_manage"},
            {"client_order_id": "AIS-x", "realized_pnl": -40.0, "exit_reason": "stop_loss"},
            {"client_order_id": "AIS-y", "realized_pnl": 27.5, "exit_reason": "dte_manage"}]
    sm = dashboard_pro._closed_summary(rows)
    assert sm["count"] == 2 and sm["wins"] == 1 and sm["net_pnl"] == 5.5
