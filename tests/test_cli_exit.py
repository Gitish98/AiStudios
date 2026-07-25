"""The unattended cron must be able to FAIL LOUDLY. cmd_run_cycle used to swallow
every error and return 0, so a heartbeat monitor could never tell a healthy cycle
from a broker outage. This locks: a fatal error -> non-zero exit + a journaled
cycle_fatal event; a normal run -> exit 0."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import cli
from core.store import Store


def test_run_cycle_exits_nonzero_and_journals_on_fatal():
    """The fatal path must exit 1 AND journal the failure.

    The store is redirected to a temp DB: an earlier version of this test used the
    default Store(), so running the suite ON THE VM wrote fake 'cycle_fatal' events
    ('broker exploded') into the PRODUCTION journal. That is not merely untidy — it
    put fabricated failures into the audit trail and produced a false alarm during
    a real end-of-day review. Tests must never touch production state."""
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "test.db"

        def boom(args, dry_run=False):
            raise RuntimeError("broker exploded")

        orig_body, orig_store = cli._run_cycle_body, cli.Store
        cli._run_cycle_body = boom
        cli.Store = lambda *a, **k: Store(db)      # redirect the fatal-path journal
        try:
            raised = None
            try:
                cli.cmd_run_cycle(SimpleNamespace(asof=None, force=False))
            except SystemExit as e:
                raised = e
            assert raised is not None and raised.code == 1, "must exit non-zero on failure"
        finally:
            cli._run_cycle_body, cli.Store = orig_body, orig_store

        s = Store(db)
        row = s.conn.execute(
            "SELECT payload FROM journal WHERE kind='cycle_fatal' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        s.close()
        assert row is not None and "broker exploded" in row["payload"]


def test_run_cycle_success_exits_zero():
    called = {"n": 0}
    def ok(args, dry_run=False):
        called["n"] += 1
    orig = cli._run_cycle_body
    cli._run_cycle_body = ok
    try:
        cli.cmd_run_cycle(SimpleNamespace(asof=None, force=False))  # no SystemExit
    finally:
        cli._run_cycle_body = orig
    assert called["n"] == 1
