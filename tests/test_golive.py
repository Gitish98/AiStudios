"""
Tests for THE GO-LIVE GATE — the most safety-critical addition in the project.

Locks every one of the five independent conditions (core.golive.GoLiveGate), the
fail-closed combination (missing ANY one keeps it paper), the factory's live
unlock, the risk gate's ramp cap, and the honest "never masquerade as live" path.
No IB library or Gateway is required: a live build can never actually connect here,
which is precisely the safe outcome we assert.
"""

import tempfile
from datetime import date, timedelta
from pathlib import Path

from core.config import Config
from core.golive import (
    ARMED_KV_KEY, GoLiveGate, confirmation_phrase, current_ramp_tier,
    live_ramp_caps, ramp_advancement_status,
)
from core.brokers.factory import build_execution_adapter
from core.brokers.ibkr import IBKRAdapter
from core.brokers.base import Account, OrderLeg, OrderRequest, Position
from core.risk import RiskContext, RiskGate, RiskLimits

TODAY = date(2026, 6, 30)
DEFAULT_RAMP = [
    {"tier": 1, "max_notional_usd": 250, "max_positions": 2, "clean_days_to_advance": 10},
    {"tier": 2, "max_notional_usd": 1000, "max_positions": 4, "clean_days_to_advance": 10},
]


def _config(mode="live", exec_broker="ibkr_live", paper_port=4002, live_port=4001,
            ramp_tier=1, ramp=None, live_enabled_file=None) -> Config:
    brokers = {"execution": exec_broker, "ibkr_port": paper_port,
               "ibkr_live_port": live_port, "ibkr_host": "127.0.0.1", "ibkr_client_id": 7,
               # The sixth gate: live must DECLARE real-time data under its own key.
               "ibkr_live_market_data_type": 1}
    go_live = {"ramp_tier": ramp_tier, "ramp": ramp if ramp is not None else DEFAULT_RAMP}
    if live_enabled_file is not None:
        go_live["live_enabled_file"] = str(live_enabled_file)
    raw = {"mode": mode, "brokers": brokers, "go_live": go_live,
           "account": {"starting_equity_usd": 30000, "type": "margin", "region": "CA"}}
    return Config(raw=raw, mode=mode, account=raw["account"], brokers=brokers,
                  risk={}, strategies={})


def _enabled_file(tmp: str, when: date = TODAY) -> Path:
    p = Path(tmp) / "LIVE_ENABLED"
    p.write_text(when.isoformat() + "\n", encoding="utf-8")
    return p


# ── the seven gates, all satisfied ────────────────────────────────────────────
# (five until 2026-09-08; the sixth — market_data — after the live factory was
# found reading paper's DELAYED key; the seventh — graduated — after the docs'
# "graduated only" was found to be enforced by no code at all)

def test_all_seven_gates_pass():
    with tempfile.TemporaryDirectory() as tmp:
        f = _enabled_file(tmp, TODAY)
        gate = GoLiveGate(_config(live_enabled_file=f), today=TODAY)
        d = gate.evaluate(confirmation=confirmation_phrase(TODAY))
        assert d.allowed, d.reasons
        assert set(d.satisfied) == {"mode:live", "live_endpoint", "market_data",
                                    "live_enabled_file", "session_confirmation",
                                    "ramp_tier", "graduated"}


# ── each single missing condition keeps it PAPER (fail-closed) ─────────────────

def test_mode_not_live_blocks():
    with tempfile.TemporaryDirectory() as tmp:
        f = _enabled_file(tmp)
        d = GoLiveGate(_config(mode="paper", live_enabled_file=f), today=TODAY).evaluate(
            confirmation_phrase(TODAY))
        assert not d.allowed
        assert any("mode:live" in r for r in d.reasons)


def test_paper_port_as_live_endpoint_blocks():
    with tempfile.TemporaryDirectory() as tmp:
        f = _enabled_file(tmp)
        # live_port set to a PAPER port must be refused (can't treat paper as live).
        d = GoLiveGate(_config(live_port=4002, live_enabled_file=f), today=TODAY).evaluate(
            confirmation_phrase(TODAY))
        assert not d.allowed
        assert any("live_endpoint" in r for r in d.reasons)


def test_non_ibkr_broker_blocks_live_endpoint():
    with tempfile.TemporaryDirectory() as tmp:
        f = _enabled_file(tmp)
        d = GoLiveGate(_config(exec_broker="alpaca_paper", live_enabled_file=f),
                       today=TODAY).evaluate(confirmation_phrase(TODAY))
        assert not d.allowed
        assert any("live_endpoint" in r for r in d.reasons)


def test_missing_enabled_file_blocks():
    missing = Path(tempfile.gettempdir()) / "definitely_not_here_LIVE_ENABLED_xyz"
    d = GoLiveGate(_config(live_enabled_file=missing), today=TODAY).evaluate(
        confirmation_phrase(TODAY))
    assert not d.allowed
    assert any("live_enabled_file" in r for r in d.reasons)


def test_stale_enabled_file_blocks():
    with tempfile.TemporaryDirectory() as tmp:
        f = _enabled_file(tmp, when=TODAY - timedelta(days=1))  # yesterday's date
        d = GoLiveGate(_config(live_enabled_file=f), today=TODAY).evaluate(
            confirmation_phrase(TODAY))
        assert not d.allowed
        assert any("live_enabled_file" in r for r in d.reasons)


def test_wrong_confirmation_blocks():
    with tempfile.TemporaryDirectory() as tmp:
        f = _enabled_file(tmp)
        gate = GoLiveGate(_config(live_enabled_file=f), today=TODAY)
        assert not gate.evaluate(confirmation="yes").allowed
        assert not gate.evaluate(confirmation=None).allowed
        # Yesterday's phrase can't be replayed today.
        assert not gate.evaluate(confirmation=confirmation_phrase(TODAY - timedelta(days=1))).allowed


def test_ramp_tier_zero_blocks():
    with tempfile.TemporaryDirectory() as tmp:
        f = _enabled_file(tmp)
        d = GoLiveGate(_config(ramp_tier=0, live_enabled_file=f), today=TODAY).evaluate(
            confirmation_phrase(TODAY))
        assert not d.allowed
        assert any("ramp_tier" in r for r in d.reasons)


def test_undefined_ramp_tier_blocks():
    with tempfile.TemporaryDirectory() as tmp:
        f = _enabled_file(tmp)
        d = GoLiveGate(_config(ramp_tier=9, live_enabled_file=f), today=TODAY).evaluate(
            confirmation_phrase(TODAY))
        assert not d.allowed
        assert any("ramp_tier" in r for r in d.reasons)


def test_confirmation_phrase_is_date_bound():
    assert confirmation_phrase(TODAY) == "CONFIRM LIVE 2026-06-30"
    assert confirmation_phrase(TODAY) != confirmation_phrase(TODAY - timedelta(days=1))


# ── the factory: the ONLY path to a live adapter ──────────────────────────────

class _StoreStub:
    def __init__(self, armed=None):
        self._armed = armed

    def get_kv(self, key, default=None):
        return self._armed if key == ARMED_KV_KEY else default


def test_factory_live_requested_but_gate_unsatisfied_stays_paper():
    # mode live, but no LIVE_ENABLED file and no armed confirmation -> PAPER.
    cfg = _config(live_enabled_file=Path(tempfile.gettempdir()) / "nope_LIVE_ENABLED")
    build = build_execution_adapter(cfg, store=_StoreStub(armed=None))
    assert build.adapter.is_paper is True
    assert "NOT satisfied" in build.note


def test_factory_all_gates_pass_but_no_gateway_never_masquerades_as_live():
    # All five gates pass (real today), but no live Gateway answers -> stay PAPER,
    # never a paper adapter pretending to be live. This is the safe outcome.
    with tempfile.TemporaryDirectory() as tmp:
        f = _enabled_file(tmp, when=date.today())          # real today
        cfg = _config(live_enabled_file=f)
        armed = confirmation_phrase(date.today())          # operator's dated phrase
        build = build_execution_adapter(cfg, store=_StoreStub(armed=armed))
        assert build.adapter.is_paper is True, "no Gateway -> must NOT be live"
        assert "LIVE gates pass" in build.note
        assert "no LIVE IB Gateway" in build.note


def test_factory_paper_mode_unaffected():
    cfg = _config(mode="paper", exec_broker="sim")
    build = build_execution_adapter(cfg, store=_StoreStub())
    assert build.adapter.is_paper is True
    assert build.adapter.name == "sim"


# ── the IBKR adapter is honest about live vs paper ────────────────────────────

def test_ibkr_live_adapter_name_and_paper_flag():
    live = IBKRAdapter(port=4001, paper=False)
    assert live.name == "ibkr_live"
    assert live.is_paper is False
    paper = IBKRAdapter(port=4002, paper=True)
    assert paper.name == "ibkr_paper"
    assert paper.is_paper is True


# ── the risk gate enforces the ramp cap as a HARD additional limit ────────────

def _account(equity=30000.0):
    return Account(equity=equity, cash=equity, buying_power=equity * 2,
                   account_type="margin", is_paper=False)


def _live_ctx(positions=None, max_notional=250.0, max_positions=2):
    return RiskContext(
        account=_account(), positions=positions or [], is_paper=False,
        kill_switch=False, options_approval_level=3, region="CA",
        live_ramp_max_notional=max_notional, live_ramp_max_positions=max_positions)


def _spread(width=1.0, credit=0.30):
    return OrderRequest(
        client_order_id="live1",
        legs=[OrderLeg("XLF_SP", "sell", 1, "option", "put", strike=40),
              OrderLeg("XLF_LP", "buy", 1, "option", "put", strike=40 - width)],
        limit_price=credit, strategy="put_credit_spread",
        max_loss=round((width - credit) * 100, 2), est_credit=credit * 100,
        underlying="XLF", required_approval_level=3)


def test_live_without_ramp_cap_fails_closed():
    ctx = _live_ctx(max_notional=None, max_positions=None)
    d = RiskGate(RiskLimits()).evaluate(_spread(), ctx)
    assert not d.approved
    assert any("without an active ramp cap" in r.lower() for r in d.reasons)


def test_live_within_ramp_cap_approved():
    # $1-wide spread => $100 collateral, under the $250 tier cap; all other caps fit.
    d = RiskGate(RiskLimits()).evaluate(_spread(), _live_ctx(max_notional=250.0))
    assert d.approved, d.reasons


def test_live_ramp_notional_cap_binds():
    d = RiskGate(RiskLimits()).evaluate(_spread(), _live_ctx(max_notional=50.0))
    assert not d.approved
    assert any("ramp cap" in r.lower() and "notional" in r.lower() for r in d.reasons)


def test_live_ramp_position_cap_binds():
    held = [Position(symbol="A", qty=1, avg_price=0, market_value=100, max_loss=0.0),
            Position(symbol="B", qty=1, avg_price=0, market_value=100, max_loss=0.0)]
    d = RiskGate(RiskLimits()).evaluate(_spread(), _live_ctx(positions=held, max_positions=2))
    assert not d.approved
    assert any("ramp cap" in r.lower() and "position" in r.lower() for r in d.reasons)


# ── ramp helpers + advisory advancement (never self-promotes) ─────────────────

def test_current_ramp_tier_and_caps():
    cfg = _config(ramp_tier=2)
    assert current_ramp_tier(cfg)["max_notional_usd"] == 1000
    assert live_ramp_caps(cfg) == (1000.0, 4)
    paper = _config(ramp_tier=0)
    assert current_ramp_tier(paper) is None
    assert live_ramp_caps(paper) == (None, None)


def test_malformed_config_degrades_to_paper_not_crash():
    # Regression (review #1): a config typo must DEGRADE to paper, never throw out
    # of the gate / factory. Both a non-list ramp table and a non-numeric live port.
    with tempfile.TemporaryDirectory() as tmp:
        f = _enabled_file(tmp)
        bad_ramp = _config(ramp="oops-not-a-list", live_enabled_file=f)
        d = GoLiveGate(bad_ramp, today=TODAY).evaluate(confirmation_phrase(TODAY))
        assert not d.allowed  # no exception
        assert any("ramp_tier" in r for r in d.reasons)
        assert current_ramp_tier(bad_ramp) is None
        assert live_ramp_caps(bad_ramp) == (None, None)

        bad_port = _config(live_port="notaport", live_enabled_file=f)
        # _config coerces live_port via the dict; pass a raw string straight through.
        bad_port.brokers["ibkr_live_port"] = "notaport"
        d2 = GoLiveGate(bad_port, today=TODAY).evaluate(confirmation_phrase(TODAY))
        assert not d2.allowed
        assert any("live_endpoint" in r for r in d2.reasons)

    # And through the factory: mode live + malformed config stays PAPER, no raise.
    cfg = _config(ramp="oops-not-a-list")
    build = build_execution_adapter(cfg, store=_StoreStub(armed=None))
    assert build.adapter.is_paper is True


def test_is_paper_normalizes_case_and_whitespace():
    # Regression (review #2): is_paper must agree with the gate's lowercased mode
    # check so the two sources of truth can't drift.
    assert Config(raw={}, mode="LIVE", account={}, brokers={}, risk={},
                  strategies={}).is_paper is False
    assert Config(raw={}, mode="  live  ", account={}, brokers={}, risk={},
                  strategies={}).is_paper is False
    assert Config(raw={}, mode="paper", account={}, brokers={}, risk={},
                  strategies={}).is_paper is True


def test_ramp_advancement_is_advisory_only():
    tier = DEFAULT_RAMP[0]  # needs 10 clean days
    good = {"trades": 12, "expectancy_net": 4.0}
    assert ramp_advancement_status(tier, good, clean_days=10)["ready_to_advance"] is True
    short = ramp_advancement_status(tier, good, clean_days=3)
    assert not short["ready_to_advance"]
    assert any("clean live days" in r for r in short["reasons"])
    flat = ramp_advancement_status(tier, {"trades": 12, "expectancy_net": 0.0}, clean_days=10)
    assert not flat["ready_to_advance"]
    assert "never self-promotes" in ramp_advancement_status(None, {}, 0)["note"]
