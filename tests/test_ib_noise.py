"""Regression tests for the IB delayed-data notice census and the sixth go-live
gate it led to.

The investigation (2026-09-08): ~2,330 `Error 10091` lines per cycle were log
NOISE — a read-only probe proved every field still arrives under delayed data —
but chasing them exposed a real money-path gap: the live factory read paper's
`ibkr_market_data_type` key (set to 3 in every real config), so arming live
would have priced live limit orders off 15-minute-stale option mids with the
gate reporting all clear.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from types import SimpleNamespace

from core import ib_noise
from core.ib_noise import (DelayedDataNoticeFilter, install_notice_filter,
                           summary_line, uninstall_notice_filter)
from core.store import Store


def _rec(msg: str, level=logging.ERROR) -> logging.LogRecord:
    return logging.LogRecord("ib_async.wrapper", level, __file__, 1, msg, (), None)


# ───────────────────────────── the filter ─────────────────────────────

def test_first_notice_prints_and_repeats_are_counted_not_printed():
    """Silence is a question here: the FACT that we are on delayed data must
    print once per cycle. The 2,329 repeats must not."""
    f = DelayedDataNoticeFilter()
    line = "Error 10091, reqId 7: Part of requested market data requires additional subscription for API. Delayed market data is available.SPY ARCA/TOP/ALL, contract: Option(...)"
    assert f.filter(_rec(line)) is True            # first: printed
    assert f.filter(_rec(line)) is False           # repeat: dropped
    assert f.filter(_rec(line)) is False
    assert f.census() == {10091: 3}                # ...but counted


def test_every_unclassified_ib_code_passes_untouched():
    """A code we have not classified is exactly the thing that must stay loud."""
    f = DelayedDataNoticeFilter()
    for msg in ("Error 200, reqId 9: No security definition has been found",
                "Error 10197, reqId -1: No market data during competing live session",
                "Error 354, reqId 3: Requested market data is not subscribed",
                "Warning 10167, reqId 4: Displaying delayed market data",
                "some unrelated log line"):
        assert f.filter(_rec(msg)) is True, msg
        assert f.filter(_rec(msg)) is True, msg     # and again
    assert f.census() == {}


def test_each_code_gets_its_own_first_line_and_own_count():
    f = DelayedDataNoticeFilter()
    a = "Error 10091, reqId 1: ...SPY ARCA/TOP/ALL"
    b = "Error 10089, reqId 2: ...SPY ARCA/TOP/ALL, contract: Stock(...)"
    assert f.filter(_rec(a)) and f.filter(_rec(b))
    assert not f.filter(_rec(a)) and not f.filter(_rec(b))
    assert f.census() == {10089: 2, 10091: 2}


def test_reset_starts_a_fresh_cycle():
    f = DelayedDataNoticeFilter()
    line = "Error 10091, reqId 1: x"
    f.filter(_rec(line)); f.filter(_rec(line))
    f.reset()
    assert f.census() == {}
    assert f.filter(_rec(line)) is True            # prints again next cycle


def test_filter_never_breaks_logging_on_a_bad_record():
    """A filter that raises takes the log line down with it."""
    f = DelayedDataNoticeFilter()
    bad = logging.LogRecord("ib_async.wrapper", logging.ERROR, __file__, 1,
                            "Error %d", ("not-an-int",), None)   # getMessage raises
    assert f.filter(bad) is True


def test_install_is_idempotent_and_attaches_to_both_library_loggers():
    uninstall_notice_filter()
    try:
        a = install_notice_filter()
        b = install_notice_filter()
        assert a is b
        for name in ("ib_async.wrapper", "ib_insync.wrapper"):
            lg = logging.getLogger(name)
            assert lg.filters.count(a) == 1, name
        # End to end through the real logging machinery: two emits, one record
        # reaches the handler.
        seen = []
        h = logging.Handler(); h.emit = lambda r: seen.append(r.getMessage())
        lg = logging.getLogger("ib_async.wrapper"); lg.addHandler(h)
        try:
            lg.error("Error 10091, reqId 5: delayed")
            lg.error("Error 10091, reqId 6: delayed")
            lg.error("Error 200, reqId 7: unrelated")
        finally:
            lg.removeHandler(h)
        assert seen == ["Error 10091, reqId 5: delayed", "Error 200, reqId 7: unrelated"]
        assert a.census() == {10091: 2}
    finally:
        uninstall_notice_filter()


def test_summary_line_is_readable_and_says_why():
    assert summary_line({}) == "none"
    # "expected" is only true under DELAYED data, so the type must be stated.
    s = summary_line({10091: 2330, 10089: 5}, 3)
    assert s.startswith("10089 x5, 10091 x2330")
    assert "expected" in s and "market_data_type=3" in s


# ───────────────────────── the sixth go-live gate ─────────────────────────

def _cfg(**brokers):
    base = {"execution": "ibkr_live", "ibkr_port": 4002, "ibkr_live_port": 4001,
            "ibkr_market_data_type": 3}
    base.update(brokers)
    return SimpleNamespace(brokers=base, mode="live",
                           go_live={"ramp_tier": 1,
                                    "ramp": [{"tier": 1, "max_risk_per_trade_pct": 0.1,
                                              "max_contracts": 1}]})


def test_live_market_data_key_missing_blocks():
    """The exact live bug: paper's key is 3, live's key absent. Before the fix
    the factory inherited the 3 and no gate looked."""
    from core.golive import GoLiveGate
    d = GoLiveGate(_cfg()).evaluate()
    c = {x.name: x for x in d.conditions}["market_data"]
    assert c.ok is False
    assert "unset" in c.detail and "NOT inherited" in c.detail
    assert d.allowed is False


def test_live_market_data_delayed_blocks_even_when_explicit():
    from core.golive import GoLiveGate
    d = GoLiveGate(_cfg(ibkr_live_market_data_type=3)).evaluate()
    c = {x.name: x for x in d.conditions}["market_data"]
    assert c.ok is False and "delayed" in c.detail


def test_live_market_data_realtime_passes_this_gate():
    from core.golive import GoLiveGate
    d = GoLiveGate(_cfg(ibkr_live_market_data_type=1)).evaluate()
    c = {x.name: x for x in d.conditions}["market_data"]
    assert c.ok is True


def test_gate_now_has_six_conditions():
    """Six, evaluated together, market_data sitting right after the endpoint
    check it belongs with. Fail-closed means ALL must pass."""
    from core.golive import GoLiveGate
    d = GoLiveGate(_cfg(ibkr_live_market_data_type=1)).evaluate()
    names = [x.name for x in d.conditions]
    assert len(names) == 7, names
    assert names[1] == "live_endpoint" and names[2] == "market_data", names
    assert {"live_enabled_file", "ramp_tier"} <= set(names)


def test_factory_live_path_never_reads_papers_key():
    """Source-level lock: the live builder must read the live-specific key."""
    src = (Path(__file__).resolve().parents[1] / "core" / "brokers"
           / "factory.py").read_text(encoding="utf-8")
    live_block = src[src.index("live_md ="):src.index("live_md =") + 200]
    assert "ibkr_live_market_data_type" in live_block
    assert 'get("ibkr_market_data_type"' not in live_block


# ───────────────────────── the auditor repeats the question ─────────────────────────

def test_audit_screams_if_live_is_armed_on_delayed_data():
    from unittest import mock
    from core.golive import ARMED_KV_KEY
    from core.selfaudit import run_self_audit
    fake = lambda: SimpleNamespace(mode="live", brokers={"ibkr_market_data_type": 3})
    with mock.patch("core.config.load_config", fake), tempfile.TemporaryDirectory() as td:
        store = Store(Path(td) / "p.db")
        store.set_kv(ARMED_KV_KEY, "I ARM LIVE 2026-09-08")   # armed NOW
        found = {f.code: f for f in run_self_audit(store)}
        assert "live_on_delayed_data" in found
        assert found["live_on_delayed_data"].severity == "critical"
        store.close()


def test_audit_is_quiet_about_data_type_in_paper_mode():
    from unittest import mock
    from core.selfaudit import run_self_audit
    fake = lambda: SimpleNamespace(mode="paper", brokers={"ibkr_market_data_type": 3})
    with mock.patch("core.config.load_config", fake), tempfile.TemporaryDirectory() as td:
        store = Store(Path(td) / "p.db")
        store.set_kv("mode", "paper")
        assert "live_on_delayed_data" not in [f.code for f in run_self_audit(store)]
        store.close()


def test_a_stale_live_kv_after_standing_down_does_not_cry_wolf():
    """Review finding: kv 'mode' reflects the last COMPLETED cycle's adapter.
    After `ais go-paper` + mode: paper, an off-hours skip exits before that kv
    is rewritten, and the heartbeat runs the audit anyway. That must not print
    a CRITICAL about live orders that cannot exist."""
    from unittest import mock
    from core.golive import ARMED_KV_KEY
    from core.selfaudit import run_self_audit
    fake = lambda: SimpleNamespace(mode="paper", brokers={"ibkr_market_data_type": 3})
    with mock.patch("core.config.load_config", fake), tempfile.TemporaryDirectory() as td:
        store = Store(Path(td) / "p.db")
        store.set_kv("mode", "live")          # stale, from the last live cycle
        store.set_kv(ARMED_KV_KEY, "")        # what go-paper writes
        assert "live_on_delayed_data" not in [f.code for f in run_self_audit(store)]
        store.close()


def test_summary_line_tells_the_truth_under_real_time_data():
    """Same codes, opposite meaning. Under type 1 they mean the component was
    NOT delivered — a live quote-quality failure, not an expected notice."""
    s3 = summary_line({10091: 2330}, 3)
    s1 = summary_line({10091: 2330}, 1)
    assert "expected" in s3 and "UNEXPECTED" not in s3
    assert "UNEXPECTED" in s1 and "NOT delivered" in s1
    assert "unknown" in summary_line({10091: 1}, None)


def test_filter_is_bound_to_the_logger_the_installed_library_actually_uses():
    """The filter works by logger NAME. If the installed IB library ever renames
    it, the filter counts nothing while the flood resumes — and this test is
    what turns that upgrade into a red suite instead of a silent regression.
    Skips (passes) where no IB library is installed."""
    try:
        import ib_async as lib
    except ImportError:
        try:
            import ib_insync as lib
        except ImportError:
            return
    w = lib.wrapper.Wrapper.__new__(lib.wrapper.Wrapper)
    try:
        w.__init__(None)              # dataclass-style init on some versions
    except Exception:
        # Fall back to reading the module source for the logger name.
        import inspect, re
        txt = open(inspect.getsourcefile(lib.wrapper), encoding="utf-8").read()
        names = re.findall(r"getLogger\(([^)]*)\)", txt)
        assert any(n.strip("\"\x27") in ib_noise._LOGGER_NAMES for n in names), names
        return
    assert getattr(w, "_logger").name in ib_noise._LOGGER_NAMES
