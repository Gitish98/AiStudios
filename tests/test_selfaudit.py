"""Regression tests for the entry-fill capture gap and the self-audit layer.

The bug these lock down (live, 2026-08-31, trade #3 SPY 763/762 x10): an entry
whose order had VANISHED from IB's session-scoped feed but whose legs were ALL
held at the broker was promoted to 'open' with its fill price silently dropped —
while the PARTIAL-fill branch, right below it, reconstructed the same price from
the same feed. Full fills are the common case, so the measurement we depend on
was being lost in the majority case and kept in the exception.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from core import selfaudit
from core.selfaudit import Finding, run_self_audit
from core.store import Store


# ───────────────────────────── fakes ─────────────────────────────

class _Pos:
    """Minimal stand-in for a broker Position (what get_positions() returns)."""

    def __init__(self, symbol, qty, avg_price, underlying, expiration, strike,
                 right="P"):
        self.symbol = symbol
        self.qty = qty
        self.avg_price = avg_price
        self.underlying = underlying
        self.option_expiration = expiration
        self.option_strike = strike
        self.option_right = right          # the field core.brokers.base.Position uses


class _Adapter:
    name = "ibkr_paper"

    def __init__(self, positions):
        self._positions = positions

    def get_positions(self):
        return self._positions


def _spy_legs(short_avg=884.541179, long_avg=921.4373, qty=10):
    """The real leg average costs from trade #3, straight off the broker."""
    return [
        _Pos("SPY   260930P00762000", -qty, short_avg, "SPY", "2026-09-30", 762.0),
        _Pos("SPY   260930P00763000", +qty, long_avg, "SPY", "2026-09-30", 763.0),
    ]


def _open_position(store, **over):
    row = {
        "underlying": "SPY", "structure": "put_debit_spread", "status": "open",
        "is_credit": 0, "contracts": 10, "short_strike": 762.0,
        "long_strike": 763.0, "width": 1.0, "expiration": "2026-09-30",
        "opened_asof": "2026-08-31", "opened_ts": "2026-08-31T14:01:49+00:00",
        "entry_credit_ps": 0.36, "max_loss": 360.0,
    }
    row.update(over)
    cols = ", ".join(row)
    qs = ", ".join("?" for _ in row)
    cur = store.conn.execute(f"INSERT INTO positions ({cols}) VALUES ({qs})",
                             tuple(row.values()))
    store.conn.commit()
    return cur.lastrowid


# ──────────────────────── the capture itself ────────────────────────

def test_full_fill_captures_price_from_positions_feed():
    """The exact regression: all legs held -> the price must be reconstructed.

    Before the fix this path journaled `entry_slip_ps: None` and left
    entry_fill_ps NULL, even though the broker was reporting the average costs
    the whole time."""
    from core.execution import _capture_entry_fill_from_book
    with tempfile.TemporaryDirectory() as td:
        store = Store(Path(td) / "p.db")
        pid = _open_position(store, entry_fill_ps=None)
        p = dict(store.conn.execute(
            "SELECT * FROM positions WHERE id=?", (pid,)).fetchone())

        slip = _capture_entry_fill_from_book(p, 10, store, _Adapter(_spy_legs()))

        # Paid 0.369/share against an intended 0.360 debit -> 0.009 adverse.
        assert slip == 0.009, slip
        row = dict(store.conn.execute(
            "SELECT * FROM positions WHERE id=?", (pid,)).fetchone())
        assert row["entry_fill_ps"] == -0.369      # debit is a NEGATIVE credit
        assert row["entry_slip_ps"] == 0.009
        # A measured fill is also what stamps paper-vs-live on the record.
        assert row["fill_mode"] is not None
        store.close()


def test_capture_is_silent_when_the_broker_has_nothing_to_say():
    """No legs, or a zero avg cost, must yield NO write — not a zero.

    IB leaves avgCost at 0.0 when it is unpopulated. Recording that would put a
    fabricated 'we paid nothing' into the series the cost model trusts."""
    from core.execution import _capture_entry_fill_from_book
    with tempfile.TemporaryDirectory() as td:
        store = Store(Path(td) / "p.db")
        pid = _open_position(store)
        p = dict(store.conn.execute(
            "SELECT * FROM positions WHERE id=?", (pid,)).fetchone())

        assert _capture_entry_fill_from_book(p, 10, store, _Adapter([])) is None
        assert _capture_entry_fill_from_book(
            p, 10, store, _Adapter(_spy_legs(short_avg=0.0, long_avg=0.0))) is None

        row = dict(store.conn.execute(
            "SELECT * FROM positions WHERE id=?", (pid,)).fetchone())
        assert row["entry_fill_ps"] is None
        assert row["entry_slip_ps"] is None
        store.close()


def test_backfill_recovers_an_open_position_and_journals_it():
    """A miss must be recoverable on a LATER cycle, not only at fill time.

    One-shot capture made a single miss permanent; the positions feed stays
    truthful for as long as we hold the legs, so ask again."""
    from core.execution import _backfill_missing_entry_fills
    with tempfile.TemporaryDirectory() as td:
        store = Store(Path(td) / "p.db")
        pid = _open_position(store)

        _backfill_missing_entry_fills(_Adapter(_spy_legs()), store)

        row = dict(store.conn.execute(
            "SELECT * FROM positions WHERE id=?", (pid,)).fetchone())
        assert row["entry_slip_ps"] == 0.009
        kinds = [r[0] for r in store.conn.execute("SELECT kind FROM journal")]
        assert "entry_fill_backfilled" in kinds
        store.close()


def test_backfill_never_overwrites_a_fill_time_measurement():
    """A reconstruction is a fallback, never a correction of the real thing."""
    from core.execution import _backfill_missing_entry_fills
    with tempfile.TemporaryDirectory() as td:
        store = Store(Path(td) / "p.db")
        pid = _open_position(store, entry_fill_ps=-0.362, entry_slip_ps=0.002)

        _backfill_missing_entry_fills(_Adapter(_spy_legs()), store)

        row = dict(store.conn.execute(
            "SELECT * FROM positions WHERE id=?", (pid,)).fetchone())
        assert row["entry_fill_ps"] == -0.362 and row["entry_slip_ps"] == 0.002
        store.close()


def test_backfill_refuses_to_act_on_a_sim_book():
    """A failed IB connect falls back to SIM, whose empty book must never be
    allowed to testify about a real position (the sim-fallback guard)."""
    from core.execution import _backfill_missing_entry_fills

    class _Sim(_Adapter):
        name = "sim"

    with tempfile.TemporaryDirectory() as td:
        store = Store(Path(td) / "p.db")
        pid = _open_position(store)
        _backfill_missing_entry_fills(_Sim(_spy_legs()), store)
        row = dict(store.conn.execute(
            "SELECT * FROM positions WHERE id=?", (pid,)).fetchone())
        assert row["entry_fill_ps"] is None
        store.close()


# ──────────────────────────── the auditor ────────────────────────────

def _fresh_cycles(store, n=4, day="2026-09-01"):
    """Recent cycle_end rows so the freshness check stays quiet."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    for i in range(n):
        store.append((now - timedelta(minutes=5 * (n - i))).isoformat(),
                     "cycle_end", {"placed": 0, "reconcile_ok": True})


def test_audit_flags_an_unmeasured_fill_that_the_backfill_did_not_recover():
    """The whole point: a missing measurement must become LOUD, not stay blank."""
    with tempfile.TemporaryDirectory() as td:
        store = Store(Path(td) / "p.db")
        _open_position(store, opened_ts="2026-08-31T14:01:49+00:00")
        _fresh_cycles(store, n=4)

        codes = [f.code for f in run_self_audit(store)]
        assert "unmeasured_entry_fill" in codes
        store.close()


def test_audit_gives_a_fresh_fill_its_grace_period():
    """One cycle without a measurement is normal — the backfill runs on the NEXT
    cycle. Alarming instantly would train the operator to ignore the alarm."""
    from datetime import datetime, timedelta, timezone
    with tempfile.TemporaryDirectory() as td:
        store = Store(Path(td) / "p.db")
        now = datetime.now(timezone.utc)
        _open_position(store, opened_ts=(now - timedelta(minutes=1)).isoformat())
        store.append(now.isoformat(), "cycle_end", {"reconcile_ok": True})

        codes = [f.code for f in run_self_audit(store)]
        assert "unmeasured_entry_fill" not in codes
        store.close()


def test_audit_screams_when_cycles_stop():
    """A stopped schedule leaves every dashboard number stale but current-looking.
    This is the only check rated critical."""
    with tempfile.TemporaryDirectory() as td:
        store = Store(Path(td) / "p.db")
        store.append("2026-01-01T10:00:00+00:00", "cycle_end", {"reconcile_ok": True})
        found = {f.code: f for f in run_self_audit(store)}
        assert "stale_cycles" in found
        assert found["stale_cycles"].severity == "critical"
        store.close()


def test_audit_is_quiet_on_a_healthy_book():
    """No findings when everything checked is satisfied — otherwise the signal
    is worthless."""
    with tempfile.TemporaryDirectory() as td:
        store = Store(Path(td) / "p.db")
        _fresh_cycles(store, n=3)
        assert run_self_audit(store) == []
        store.close()


def test_audit_flags_a_position_stuck_pending():
    """Pending consumes risk budget while being neither managed nor expected at
    the broker; a row that never resolves is budget spent on nothing."""
    with tempfile.TemporaryDirectory() as td:
        store = Store(Path(td) / "p.db")
        _open_position(store, status="pending",
                       opened_ts="2026-08-01T14:00:00+00:00")
        _fresh_cycles(store, n=5)
        codes = [f.code for f in run_self_audit(store)]
        assert "stuck_pending" in codes
        store.close()


def test_audit_never_raises_out_of_a_broken_check():
    """A smoke detector must not be able to burn the house down."""
    def _boom(store):
        raise RuntimeError("check exploded")

    original = selfaudit.CHECKS
    selfaudit.CHECKS = (_boom,)
    try:
        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "p.db")
            found = run_self_audit(store)
            assert [f.code for f in found] == ["audit_check_failed"]
            store.close()
    finally:
        selfaudit.CHECKS = original


def test_audit_journals_even_when_clean():
    """An alarm that only writes on failure cannot be told apart from one that is
    switched off."""
    from core.selfaudit import journal_self_audit
    with tempfile.TemporaryDirectory() as td:
        store = Store(Path(td) / "p.db")
        _fresh_cycles(store, n=3)
        journal_self_audit(store)
        rows = [r[0] for r in store.conn.execute(
            "SELECT kind FROM journal WHERE kind='self_audit'")]
        assert rows, "a clean audit must still be journaled"
        store.close()


def test_findings_sort_worst_first():
    from core.selfaudit import run_self_audit  # noqa: F401
    fs = [Finding("a", "low", "x"), Finding("b", "critical", "y"),
          Finding("c", "medium", "z")]
    fs.sort(key=lambda f: (selfaudit.SEV_ORDER.get(f.severity, 9), f.code))
    assert [f.severity for f in fs] == ["critical", "medium", "low"]


def test_dashboard_surfaces_audit_findings_and_says_so_when_it_cannot():
    """A finding only the journal knows about is barely louder than the NULL it
    replaced. And an auditor that FAILED must not render as a clean page."""
    import dashboard_pro

    with tempfile.TemporaryDirectory() as td:
        store = Store(Path(td) / "p.db")
        _open_position(store, opened_ts="2026-08-31T14:01:49+00:00")
        _fresh_cycles(store, n=4)
        payload = dashboard_pro._self_audit(store)
        codes = [f["code"] for f in payload["findings"]]
        assert "unmeasured_entry_fill" in codes
        store.close()

    # A broken auditor reports None — NOT an empty list, which would read as
    # "all clear" on the page.
    original = selfaudit.CHECKS

    class _Boom:
        conn = None
    selfaudit.CHECKS = None                      # force run_self_audit to blow up
    try:
        assert dashboard_pro._self_audit(_Boom())["findings"] is None
    finally:
        selfaudit.CHECKS = original


# ─────────────── strictness: no number beats a wrong number ───────────────
# Every case below produced a plausible-looking fill that PASSED
# plausible_slip_ps and would have been promoted to MEASURED evidence in the
# go-live gate. Found by adversarial preflight review, reproduced here.

def _condor(store, **over):
    import json
    row = {
        "underlying": "SPY", "structure": "iron_condor", "status": "open",
        "is_credit": 1, "contracts": 1, "short_strike": 750.0,
        "long_strike": 745.0, "width": 5.0, "expiration": "2026-09-30",
        "opened_asof": "2026-08-31", "opened_ts": "2026-08-31T14:01:49+00:00",
        "entry_credit_ps": 1.20, "max_loss": 380.0,
        "legs_json": json.dumps({"sp": 750.0, "lp": 745.0,
                                 "sc": 790.0, "lc": 795.0}),
    }
    row.update(over)
    cols = ", ".join(row)
    qs = ", ".join("?" for _ in row)
    cur = store.conn.execute(f"INSERT INTO positions ({cols}) VALUES ({qs})",
                             tuple(row.values()))
    store.conn.commit()
    return cur.lastrowid


def test_condor_is_priced_from_all_four_legs_or_not_at_all():
    """An iron condor keeps only its PUT wing in short_strike/long_strike; the
    calls live in legs_json. The loose matcher returned 2 legs of 4 and netted
    half the credit as 'the fill' — a condor with ZERO real slippage produced a
    fabricated 0.60/share adverse number that passed every downstream guard."""
    from core.execution import _capture_entry_fill_from_book
    with tempfile.TemporaryDirectory() as td:
        store = Store(Path(td) / "p.db")
        pid = _condor(store)
        p = dict(store.conn.execute(
            "SELECT * FROM positions WHERE id=?", (pid,)).fetchone())

        put_wing_only = [
            _Pos("SPY 750P", -1, 300.0, "SPY", "2026-09-30", 750.0, "P"),
            _Pos("SPY 745P", +1, 240.0, "SPY", "2026-09-30", 745.0, "P"),
        ]
        assert _capture_entry_fill_from_book(
            p, 1, store, _Adapter(put_wing_only)) is None
        assert dict(store.conn.execute(
            "SELECT * FROM positions WHERE id=?", (pid,)).fetchone()
        )["entry_fill_ps"] is None

        # All four legs present -> a real measurement. Put wing +0.60, call wing
        # +0.60 = 1.20 net credit, exactly as intended: zero slippage.
        full = put_wing_only + [
            _Pos("SPY 790C", -1, 250.0, "SPY", "2026-09-30", 790.0, "C"),
            _Pos("SPY 795C", +1, 190.0, "SPY", "2026-09-30", 795.0, "C"),
        ]
        assert _capture_entry_fill_from_book(p, 1, store, _Adapter(full)) == 0.0
        store.close()


def test_a_surviving_single_leg_after_assignment_yields_no_price():
    """The short leg is assigned away overnight. The loose matcher netted the one
    remaining long put and wrote a $9.21/share fill on a $1-wide spread."""
    from core.execution import _capture_entry_fill_from_book
    with tempfile.TemporaryDirectory() as td:
        store = Store(Path(td) / "p.db")
        pid = _open_position(store)
        p = dict(store.conn.execute(
            "SELECT * FROM positions WHERE id=?", (pid,)).fetchone())
        orphan = [_Pos("SPY   260930P00763000", +10, 921.4373,
                       "SPY", "2026-09-30", 763.0, "P")]
        assert _capture_entry_fill_from_book(p, 10, store, _Adapter(orphan)) is None
        store.close()


def test_a_call_spread_cannot_be_priced_from_the_put_spread_at_the_same_strikes():
    """The matcher ignored option right entirely, so same-strike puts and calls
    were interchangeable."""
    from core.execution import _capture_entry_fill_from_book
    with tempfile.TemporaryDirectory() as td:
        store = Store(Path(td) / "p.db")
        pid = _open_position(store)
        p = dict(store.conn.execute(
            "SELECT * FROM positions WHERE id=?", (pid,)).fetchone())
        calls = [
            _Pos("SPY 762C", -10, 884.54, "SPY", "2026-09-30", 762.0, "C"),
            _Pos("SPY 763C", +10, 921.44, "SPY", "2026-09-30", 763.0, "C"),
        ]
        assert _capture_entry_fill_from_book(p, 10, store, _Adapter(calls)) is None
        store.close()


def test_broker_holding_fewer_units_than_asked_yields_no_price():
    """Pricing 10 units against a book holding 1 would net a per-share figure that
    does not describe the position we are about to charge costs against."""
    from core.execution import _capture_entry_fill_from_book
    with tempfile.TemporaryDirectory() as td:
        store = Store(Path(td) / "p.db")
        pid = _open_position(store)
        p = dict(store.conn.execute(
            "SELECT * FROM positions WHERE id=?", (pid,)).fetchone())
        assert _capture_entry_fill_from_book(
            p, 10, store, _Adapter(_spy_legs(qty=1))) is None
        # ...but pricing the 1 unit actually held is fine.
        assert _capture_entry_fill_from_book(
            p, 1, store, _Adapter(_spy_legs(qty=1))) == 0.009
        store.close()


def test_position_is_held_always_returns_a_pair():
    """It promised tuple[bool, bool] but returned a bare False on two early
    exits, so `all_held, any_held = ...` raised TypeError and aborted the entire
    cycle — every open position left unmarked and unmanaged that run."""
    from core.execution import _position_is_held
    assert _position_is_held({"contracts": 0}, {}) == (False, False)
    assert _position_is_held(
        {"contracts": 1, "structure": "iron_condor", "legs_json": None}, {}) == (False, False)


# ─────────── credibility: an alarm that cries wolf is switched off ───────────

def test_weekend_is_not_a_stale_schedule():
    """A 30-hour rule reported CRITICAL every Saturday, Sunday and Monday
    morning on a perfectly healthy system. Staleness is measured in MISSED
    TRADING DAYS."""
    from datetime import datetime, timezone
    from unittest import mock
    with tempfile.TemporaryDirectory() as td:
        store = Store(Path(td) / "p.db")
        # Last cycle Friday 2026-08-28 15:35 ET (19:35 UTC).
        store.append("2026-08-28T19:35:00+00:00", "cycle_end", {"reconcile_ok": True})
        # Now: Sunday 2026-08-30 14:00 ET — 43 hours later, nothing missed.
        sunday = datetime(2026, 8, 30, 18, 0, tzinfo=timezone.utc)
        with mock.patch.object(selfaudit, "_now", return_value=sunday):
            assert [f.code for f in run_self_audit(store)] == []
        # Monday 09:00 ET, before the 10:00 cron — still nothing missed.
        monday_am = datetime(2026, 8, 31, 13, 0, tzinfo=timezone.utc)
        with mock.patch.object(selfaudit, "_now", return_value=monday_am):
            assert "stale_cycles" not in [f.code for f in run_self_audit(store)]
        # Tuesday: Monday came and went with no cycle. THAT is an alarm.
        tuesday = datetime(2026, 9, 1, 13, 0, tzinfo=timezone.utc)
        with mock.patch.object(selfaudit, "_now", return_value=tuesday):
            assert "stale_cycles" in [f.code for f in run_self_audit(store)]
        store.close()


def test_routine_entry_drift_is_not_a_finding_but_persistent_drift_is():
    """Every entry raises drift: the order fills between placement and reconcile,
    so the broker holds a leg our row will not call open until the next cycle.
    Alarming on that would fire on every single trade."""
    with tempfile.TemporaryDirectory() as td:
        store = Store(Path(td) / "p.db")
        _fresh_cycles(store, n=1)                      # older history
        store.append("2026-09-01T14:06:00+00:00", "reconcile_drift",
                     {"drift": [{"kind": "untracked_at_broker"}]})
        store.append("2026-09-01T14:06:01+00:00", "cycle_end", {"reconcile_ok": False})
        assert "reconcile_drift_unresolved" not in [f.code for f in run_self_audit(store)]

        # Same drift again next cycle, still unresolved -> now it is real.
        store.append("2026-09-01T19:36:00+00:00", "reconcile_drift",
                     {"drift": [{"kind": "untracked_at_broker"}]})
        store.append("2026-09-01T19:36:01+00:00", "cycle_end", {"reconcile_ok": False})
        assert "reconcile_drift_unresolved" in [f.code for f in run_self_audit(store)]
        store.close()


def test_an_expired_worthless_spread_is_not_an_unmeasured_exit():
    """Expiring worthless is the strategy's most common WIN. It was never sold,
    so there is no exit price to have missed — and flagging it would leave the
    audit permanently dirty."""
    with tempfile.TemporaryDirectory() as td:
        store = Store(Path(td) / "p.db")
        _fresh_cycles(store, n=3)
        _open_position(store, status="closed", closed_asof="2026-09-04",
                       exit_reason="expired", realized_pnl=21.0,
                       entry_fill_ps=-0.36, fill_mode="paper")
        assert "unmeasured_exit_fill" not in [f.code for f in run_self_audit(store)]

        # A spread we actually SOLD and failed to measure is still a finding.
        _open_position(store, status="closed", closed_asof="2026-09-04",
                       exit_reason="profit_target", realized_pnl=10.0,
                       entry_fill_ps=-0.36, fill_mode="paper")
        assert "unmeasured_exit_fill" in [f.code for f in run_self_audit(store)]
        store.close()


def test_grace_period_runs_from_the_fill_not_from_order_placement():
    """A limit entry can sit pending for many cycles then fill. Counting from
    placement spends the whole grace period before the backfill gets one try."""
    from datetime import datetime, timedelta, timezone
    with tempfile.TemporaryDirectory() as td:
        store = Store(Path(td) / "p.db")
        now = datetime.now(timezone.utc)
        pid = _open_position(store,
                             opened_ts=(now - timedelta(days=9)).isoformat())
        # Five cycles passed while it sat pending...
        for i in range(5):
            store.append((now - timedelta(hours=40 - i)).isoformat(),
                         "cycle_end", {"reconcile_ok": True})
        # ...then it filled just now.
        store.append((now - timedelta(minutes=2)).isoformat(), "entry_filled",
                     {"position_id": pid, "underlying": "SPY"})
        store.append(now.isoformat(), "cycle_end", {"reconcile_ok": True})

        assert "unmeasured_entry_fill" not in [f.code for f in run_self_audit(store)]
        store.close()


def test_audit_stays_cheap_on_a_badly_stale_store():
    """is_trading_day consults the exchange calendar (~135ms a call). Walking a
    stale store day by day cost 9.5s — paid on EVERY cycle and every dashboard
    build. A watchdog that is expensive to consult gets consulted less."""
    import time
    with tempfile.TemporaryDirectory() as td:
        store = Store(Path(td) / "p.db")
        store.append("2024-01-02T15:00:00+00:00", "cycle_end", {"reconcile_ok": True})
        t0 = time.time()
        found = run_self_audit(store)
        elapsed = time.time() - t0
        assert "stale_cycles" in [f.code for f in found]
        assert elapsed < 2.0, f"self-audit took {elapsed:.1f}s"
        store.close()


def test_audit_brief_is_compact_and_carries_no_dollar_figures():
    """The heartbeat body leaves the VM for a third-party monitor. It must say
    'go look', not publish the book — and it must fit: the raw cycle log is
    ~1.1MB, and healthchecks.io keeps only the first ~100KB, which on this
    system is 2,330 copies of one IB warning."""
    import io as _io, contextlib, argparse
    import cli
    with tempfile.TemporaryDirectory() as td:
        store = Store(Path(td) / "p.db")
        _open_position(store, opened_ts="2026-08-31T14:01:49+00:00")
        _fresh_cycles(store, n=4)
        store.close()
        import core.store as _cs
        orig = _cs.Store.__init__

        def _patched(self, path=None, *a, **k):
            return orig(self, Path(td) / "p.db", *a, **k)
        _cs.Store.__init__ = _patched
        cli.Store = _cs.Store
        try:
            buf = _io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.cmd_audit(argparse.Namespace(brief=True))
            out = buf.getvalue().strip()
        finally:
            _cs.Store.__init__ = orig

    assert len(out.splitlines()) == 1, "brief must be ONE line"
    assert "$" not in out, f"brief must carry no dollar figures: {out}"
    assert "unmeasured_entry_fill(high)" in out
    assert rc == 2, "a high finding must exit non-zero so a monitor can act"
