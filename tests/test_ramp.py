"""Regression tests for the live ramp's evidence engine (core/ramp.py) and the
seventh go-live gate (`graduated`) it feeds.

Found 2026-09-08 while planning the November tier-1 validation:
`ramp_advancement_status` had NO caller — nothing computed clean days, nothing
computed live-only metrics, `ready_to_advance` was surfaced nowhere — and the
docs' "graduated only" rule for the live ramp was enforced by no code.

The first implementation was then reviewed by 20 agents, who found fourteen ways
it could OVERSTATE the evidence. Every one is a test here.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core.ramp import (breach_events, clean_days, live_days, live_metrics,
                       live_session_dates, ramp_status, tier_started_ts)
from core.store import Store

RAMP = [{"tier": 1, "max_notional_usd": 250, "max_positions": 2, "clean_days_to_advance": 10},
        {"tier": 2, "max_notional_usd": 1000, "max_positions": 4, "clean_days_to_advance": 10},
        {"tier": 3, "max_notional_usd": 5000, "max_positions": 6, "clean_days_to_advance": 20}]


def _cfg(ramp_tier=1, mode="live"):
    # core.golive._golive_cfg reads config.raw["go_live"], not config.go_live.
    go_live = {"ramp_tier": ramp_tier, "ramp": RAMP}
    return SimpleNamespace(
        mode=mode,
        brokers={"execution": "ibkr_live", "ibkr_port": 4002, "ibkr_live_port": 4001,
                 "ibkr_market_data_type": 3, "ibkr_live_market_data_type": 1},
        go_live=go_live, raw={"go_live": go_live})


def _ts(day: str, hhmm: str = "14:00") -> str:
    # 14:00 UTC == 10:00 ET on the same date, like the real 10:00 cron.
    return f"2026-11-{day}T{hhmm}:00+00:00"


def _arm(store, tier=1, day="02"):
    store.append(_ts(day, "13:00"), "go_live_armed",
                 {"date": f"2026-11-{day}", "tier": tier, "max_notional_usd": 250})


def _live_day(store, day: str, *, tier=1, drift=False, ok=True, broker="ibkr_live",
              complete=True, asof=None, arm=True):
    """One cycle on 2026-11-<day>. Arming is per day in reality (the phrase is
    date-bound), so a live day carries its own go_live_armed row."""
    if arm and broker == "ibkr_live":
        _arm(store, tier=tier, day=day)
    store.append(_ts(day, "14:00"), "cycle_start",
                 {"asof": asof or f"2026-11-{day}", "broker": broker, "equity": 1.0})
    if drift:
        store.append(_ts(day, "14:05"), "reconcile_drift",
                     {"drift": [{"kind": "untracked_at_broker"}]})
    if complete:
        store.append(_ts(day, "14:06"), "cycle_end",
                     {"placed": 0, "rejected": 0, "reconcile_ok": ok, "duration_secs": 300})


def _closed(store, *, fill_mode, pnl, day="10"):
    row = {"underlying": "SPY", "structure": "put_credit_spread", "status": "closed",
           "is_credit": 1, "contracts": 1, "short_strike": 700.0, "long_strike": 699.0,
           "width": 1.0, "expiration": "2026-12-18", "opened_asof": f"2026-11-{day}",
           "closed_asof": f"2026-11-{day}", "entry_credit_ps": 0.30,
           "realized_pnl": pnl, "exit_reason": "profit_target",
           "fill_mode": fill_mode, "max_loss": 70.0}
    cols = ", ".join(row); qs = ", ".join("?" for _ in row)
    store.conn.execute(f"INSERT INTO positions ({cols}) VALUES ({qs})", tuple(row.values()))
    store.conn.commit()


# ───────────────────────── what resets the clock, and what must not ─────────────────────────

def test_routine_one_cycle_drift_does_not_reset_the_clock():
    """Every entry produces one cycle of untracked_at_broker by design. Counting
    it would reset the clock on every trade and make tier 1 a trap."""
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db"); _arm(s)
        _live_day(s, "03")
        _live_day(s, "04", drift=True, ok=False)     # fills between place and reconcile
        _live_day(s, "05")                            # cleared next cycle
        _live_day(s, "06")
        n, last = clean_days(s, tier_started_ts(s, 1), tier=1); s.close()
        assert n == 4 and last is None


def test_persistent_drift_resets_the_clock():
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db"); _arm(s)
        _live_day(s, "03")
        _live_day(s, "04", drift=True, ok=False)
        _live_day(s, "05", drift=True, ok=False)      # persisted -> breach at 05
        _live_day(s, "06"); _live_day(s, "09")
        n, last = clean_days(s, tier_started_ts(s, 1), tier=1); s.close()
        assert last and last["kind"] == "reconcile_drift_persistent"
        assert last["ts"].startswith("2026-11-05")
        assert n == 2                                  # 06 and 09 only


def test_kill_engaged_resets_but_a_disengage_does_not():
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db"); _arm(s)
        _live_day(s, "03"); _live_day(s, "04")
        s.append(_ts("04", "18:00"), "kill_switch", {"engaged": True, "reason": "daily_loss_limit"})
        s.append(_ts("04", "19:00"), "kill_switch", {"engaged": False})   # cleared same evening
        _live_day(s, "05"); _live_day(s, "06")
        n, last = clean_days(s, tier_started_ts(s, 1), tier=1); s.close()
        assert last["kind"] == "kill_switch" and last["ts"].startswith("2026-11-04T18")
        assert n == 2


def test_frozen_underlying_and_fatal_cycle_reset():
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db"); _arm(s)
        _live_day(s, "03"); _live_day(s, "04")
        s.append(_ts("04", "14:07"), "underlying_frozen", {"underlying": "SPY", "severity": "critical"})
        s.append(_ts("04", "20:00"), "underlying_unfrozen", {"underlying": "SPY", "by": "operator"})
        _live_day(s, "05")
        s.append(_ts("05", "14:07"), "cycle_fatal", {"error": "boom"})
        _live_day(s, "06")
        n, last = clean_days(s, tier_started_ts(s, 1), tier=1)
        kinds = [k for _, k in breach_events(s, tier_started_ts(s, 1))]; s.close()
        assert last["kind"] == "cycle_fatal" and n == 1
        assert kinds == ["underlying_frozen", "cycle_fatal"]


def test_paper_cycles_never_count_as_live_days():
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db"); _arm(s)
        for d in ("03", "04", "05"):
            _live_day(s, d, broker="ibkr_paper")
        dates = live_session_dates(s); cd = clean_days(s, tier_started_ts(s, 1), tier=1); s.close()
        assert dates == [] and cd == (0, None)


def test_a_tier_that_was_never_armed_has_no_evidence():
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db")
        _live_day(s, "03", arm=False)
        rs = ramp_status(s, _cfg(ramp_tier=1)); s.close()
        assert rs["started"] is None
        assert rs["advancement"]["ready_to_advance"] is False
        assert any("never been armed" in r for r in rs["advancement"]["reasons"]), rs


def test_live_metrics_exclude_paper_fills():
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db")
        _closed(s, fill_mode="paper", pnl=100.0)
        _closed(s, fill_mode="live", pnl=30.0)
        m = live_metrics(s); s.close()
        assert m["trades"] == 1 and m["gross_pnl"] == 30.0


def test_ready_to_advance_only_with_the_full_evidence():
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db")
        for d in ["03", "04", "05", "06", "09", "10", "11", "12", "13", "16"]:
            _live_day(s, d)
        rs1 = ramp_status(s, _cfg(ramp_tier=1))
        _closed(s, fill_mode="live", pnl=60.0, day="10")   # opened on an armed tier-1 day
        rs2 = ramp_status(s, _cfg(ramp_tier=1)); s.close()
        assert rs1["clean_days"] == 10
        assert rs1["advancement"]["ready_to_advance"] is False
        assert any("no closed live trades" in r for r in rs1["advancement"]["reasons"])
        assert rs2["live_metrics"]["trades"] == 1
        assert rs2["advancement"]["ready_to_advance"] is True, rs2["advancement"]


def test_ramp_status_on_paper_is_explicit_not_empty():
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db")
        rs = ramp_status(s, _cfg(ramp_tier=0)); s.close()
        assert rs["tier"] == 0 and rs["advancement"]["ready_to_advance"] is False
        assert any("paper" in r for r in rs["advancement"]["reasons"])


# ───────────────────── the fourteen ways v1 overstated the evidence ─────────────────────

def test_evidence_is_attributed_to_the_tier_armed_that_day():
    """Review finding #1 (high). Tier 2 armed, a kill, then ten clean days back at
    tier 1 with a winner. v1 counted those as tier-2 evidence and gate #7 would
    have permitted tier 3 with zero clean days ever run at tier 2."""
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db")
        _live_day(s, "02", tier=2)
        _live_day(s, "03", tier=2)
        s.append(_ts("03", "18:00"), "kill_switch", {"engaged": True, "reason": "daily_loss_limit"})
        s.append(_ts("03", "19:00"), "kill_switch", {"engaged": False})
        for d in ["04", "05", "06", "09", "10", "11", "12", "13", "16", "17"]:
            _live_day(s, d, tier=1)
        _closed(s, fill_mode="live", pnl=60.0, day="10")
        t2 = ramp_status(s, _cfg(ramp_tier=3), tier=2)
        t1 = ramp_status(s, _cfg(ramp_tier=3), tier=1)
        s.close()
        assert t2["clean_days"] == 0 and t2["live_metrics"]["trades"] == 0
        assert t2["advancement"]["ready_to_advance"] is False
        assert t1["clean_days"] == 10 and t1["live_metrics"]["trades"] == 1
        assert t1["advancement"]["ready_to_advance"] is True


def test_a_cycle_killed_by_the_timeout_is_not_a_clean_day():
    """Findings #4/#9. SIGTERM from the cron `timeout` journals neither cycle_end
    nor cycle_fatal. A day the book was never reconciled is not clean."""
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db")
        _live_day(s, "03")
        _live_day(s, "04", complete=False)             # start, no end
        _live_day(s, "05")
        days = live_days(s); n, _ = clean_days(s, tier_started_ts(s, 1), tier=1); s.close()
        assert days["2026-11-04"].completed is False and not days["2026-11-04"].clean
        assert n == 2


def test_days_with_the_kill_switch_engaged_are_not_clean():
    """Findings #5/#8. Ten days of the system being switched off are not ten
    days of clean operation."""
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db")
        _live_day(s, "03")
        s.append(_ts("03", "18:00"), "kill_switch", {"engaged": True})
        for d in ("04", "05", "06"):
            _live_day(s, d)                            # cron keeps firing, kill on
        s.append(_ts("06", "18:00"), "kill_switch", {"engaged": False})
        _live_day(s, "09"); _live_day(s, "10")
        days = live_days(s); n, _ = clean_days(s, tier_started_ts(s, 1), tier=1); s.close()
        assert all(days[f"2026-11-{d}"].kill_engaged for d in ("04", "05", "06"))
        assert n == 2                                   # 09, 10


def test_days_with_a_frozen_underlying_are_not_clean():
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db")
        _live_day(s, "03")
        s.append(_ts("03", "14:07"), "underlying_frozen", {"underlying": "SPY"})
        _live_day(s, "04"); _live_day(s, "05")         # still frozen
        s.append(_ts("05", "18:00"), "underlying_unfrozen", {"underlying": "SPY"})
        _live_day(s, "06")
        days = live_days(s); n, _ = clean_days(s, tier_started_ts(s, 1), tier=1); s.close()
        assert days["2026-11-04"].frozen and days["2026-11-05"].frozen
        assert n == 1                                   # 06


def test_a_replayed_cycle_cannot_pad_the_evidence():
    """Finding #11. `run-cycle --asof <future>` journals a cycle_start whose asof
    disagrees with its own timestamp. That is a replay, not a live day."""
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db")
        _live_day(s, "03")
        for fake in ("2026-11-20", "2026-11-21", "2026-11-22"):
            _live_day(s, "03", asof=fake, arm=False)   # same real day, faked asof
        days = live_days(s); n, _ = clean_days(s, tier_started_ts(s, 1), tier=1); s.close()
        assert list(days) == ["2026-11-03"]
        assert days["2026-11-03"].replayed and n == 0


def test_live_metrics_use_the_configured_cost_model_not_defaults():
    """Findings #7/#10/#14. Defaults are the optimistic half."""
    from core.costs import CostModel
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db")
        _live_day(s, "03"); _closed(s, fill_mode="live", pnl=10.0, day="03")
        cfg = _cfg(ramp_tier=1)
        with mock.patch.object(CostModel, "from_config", return_value=CostModel()) as fc:
            ramp_status(s, cfg)
            fc.assert_called_with(cfg)
        with mock.patch.object(CostModel, "from_config", side_effect=ValueError("bad costs")):
            rs = ramp_status(s, cfg)
        s.close()
        assert rs["advancement"]["ready_to_advance"] is False
        assert any("cost model unreadable" in r for r in rs["advancement"]["reasons"])


def test_an_unreadable_journal_is_evidence_unavailable_not_zero_breaches():
    """Finding #12. A swallowed read error was the OPTIMISTIC direction."""
    class _Boom:
        def execute(self, *a, **k):
            raise RuntimeError("disk I/O error")
    stub = SimpleNamespace(conn=_Boom(), kill_switch=False)
    rs = ramp_status(stub, _cfg(ramp_tier=1))
    assert rs["error"] and "unreadable" in rs["error"]
    assert rs["advancement"]["ready_to_advance"] is False
    assert any("evidence unavailable" in r for r in rs["advancement"]["reasons"])


def test_a_malformed_ramp_row_is_reported_not_raised():
    """Finding #13. A typo in the ramp table must not traceback the tools."""
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db"); _live_day(s, "03")
        go_live = {"ramp_tier": 1, "ramp": [{"tier": 1, "clean_days_to_advance": "ten"}]}
        cfg = SimpleNamespace(mode="live", brokers={}, go_live=go_live, raw={"go_live": go_live})
        rs = ramp_status(s, cfg); s.close()
        assert rs["advancement"]["ready_to_advance"] is False
        assert rs["error"] is not None


def test_factory_passes_the_store_to_the_gate():
    """Findings #2/#3 (high). Without it, every tier >= 2 fails closed at cycle
    time while go-live reports LIVE ARMED: the ramp dead above tier 1."""
    src = (Path(__file__).resolve().parents[1] / "core" / "brokers" / "factory.py").read_text(encoding="utf-8")
    assert "GoLiveGate(config, store=store)" in src
    assert "GoLiveGate(config)" not in src


def test_reconcile_auto_kill_journals_a_kill_event():
    """Finding #6. `reconcile --auto_kill_on_drift` engaged the switch with no
    journal row, so the ramp never saw the KILL."""
    src = (Path(__file__).resolve().parents[1] / "cli.py").read_text(encoding="utf-8")
    i = src.index("auto_kill_on_drift")
    block = src[i:i + 700]
    assert "set_kill_switch(True)" in block and '"kill_switch"' in block and '"engaged": True' in block


def test_cmd_ramp_survives_a_broken_status_and_closes_the_store():
    import io, contextlib, argparse
    import cli
    import core.store as _cs
    with tempfile.TemporaryDirectory() as td:
        orig = _cs.Store.__init__
        closed = []

        def _patched(self, path=None, *a, **k):
            return orig(self, Path(td) / "p.db", *a, **k)
        _cs.Store.__init__ = _patched
        orig_close = _cs.Store.close
        _cs.Store.close = lambda self: (closed.append(True), orig_close(self))
        try:
            with mock.patch("core.ramp.ramp_status", side_effect=RuntimeError("kaboom")):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    cli.cmd_ramp(argparse.Namespace())
            out = buf.getvalue()
        finally:
            _cs.Store.__init__ = orig
            _cs.Store.close = orig_close
    assert closed, "store must be closed even when ramp_status raises"
    assert "kaboom" in out and "could not" in out.lower()


# ───────────────────────────── the seventh gate ─────────────────────────────

def _cond(cfg, store=None, name="graduated"):
    from core.golive import GoLiveGate
    d = GoLiveGate(cfg, store=store).evaluate(confirmation=None)
    return {c.name: c for c in d.conditions}[name], d


def test_tier_one_is_permitted_before_graduation():
    c, _ = _cond(_cfg(ramp_tier=1))
    assert c.ok is True and "validation" in c.detail


def test_tier_two_without_a_store_fails_closed():
    c, d = _cond(_cfg(ramp_tier=2), store=None)
    assert c.ok is False and "no store" in c.detail and d.allowed is False


def test_tier_two_is_refused_without_graduation_and_prior_tier_evidence():
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db")
        c, d = _cond(_cfg(ramp_tier=2), store=s); s.close()
        assert c.ok is False
        assert "not graduated" in c.detail
        assert "tier 1 live evidence not ready" in c.detail
        assert d.allowed is False


def test_tier_two_passes_only_when_both_halves_are_earned():
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "p.db")
        grad_yes = {"graduated": True, "reasons": []}
        ready = {"advancement": {"ready_to_advance": True, "reasons": [], "note": ""}}
        not_ready = {"advancement": {"ready_to_advance": False,
                                     "reasons": ["3/10 clean live days at tier 1"], "note": ""}}
        with mock.patch("core.performance.graduation_status", return_value=grad_yes), \
             mock.patch("core.ramp.ramp_status", return_value=not_ready):
            c1, _ = _cond(_cfg(ramp_tier=2), store=s)
        with mock.patch("core.performance.graduation_status", return_value=grad_yes), \
             mock.patch("core.ramp.ramp_status", return_value=ready):
            c2, _ = _cond(_cfg(ramp_tier=2), store=s)
        s.close()
        assert c1.ok is False and "3/10 clean live days" in c1.detail
        assert c2.ok is True and "tier 1 live evidence ready" in c2.detail


def test_gate_has_seven_conditions_and_graduated_is_last():
    from core.golive import GoLiveGate
    d = GoLiveGate(_cfg(ramp_tier=1)).evaluate(confirmation=None)
    names = [c.name for c in d.conditions]
    assert len(names) == 7 and names[-1] == "graduated", names


def test_tier_zero_has_nothing_to_graduate_into():
    c, _ = _cond(_cfg(ramp_tier=0))
    assert c.ok is False and "paper" in c.detail
