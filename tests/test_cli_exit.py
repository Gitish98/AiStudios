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


def test_run_cycle_exits_nonzero_and_journals_on_fatal(monkeypatch=None):
    # Force the cycle body to blow up.
    def boom(args, dry_run=False):
        raise RuntimeError("broker exploded")
    orig = cli._run_cycle_body
    cli._run_cycle_body = boom
    try:
        raised = None
        try:
            cli.cmd_run_cycle(SimpleNamespace(asof=None, force=False))
        except SystemExit as e:
            raised = e
        assert raised is not None and raised.code == 1, "must exit non-zero on failure"
    finally:
        cli._run_cycle_body = orig

    # The failure is recorded for later diagnosis.
    s = Store()
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
