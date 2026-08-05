"""
Tier 3 scenario-hunt fixes — the "latent, low-frequency" set, eliminated
structurally rather than watched:
  - rule-based holiday calendar (valid any year; the old list expired after 2027
    and then FAILED OPEN — 2028 New Year's Day would have been a trading day)
  - degraded-sim guard (a failed IB connect must never let a sim cycle pollute
    a store with real history)
  - FX fail-closed (unconverted CAD would loosen every USD cap by ~42%)
  - entry-time leg-overlap block (two positions on one contract make every
    broker-truth resolver ambiguous — so the state is made unrepresentable)
"""

from __future__ import annotations

import datetime as dt
import tempfile
from pathlib import Path

from core.store import Store


# ─────────────────────── rule-based calendar ───────────────────────

def test_rules_reproduce_the_hardcoded_2026_2027_sets_exactly():
    """The old hardcoded lists become the validation fixture for the rules."""
    from core.market_calendar import _FALLBACK_HOLIDAYS, us_market_holidays
    computed = {d.isoformat() for d in us_market_holidays(2026)} | \
               {d.isoformat() for d in us_market_holidays(2027)}
    assert computed == _FALLBACK_HOLIDAYS


def test_easter_computus():
    from core.market_calendar import easter
    assert easter(2026) == dt.date(2026, 4, 5)
    assert easter(2027) == dt.date(2027, 3, 28)
    assert easter(2028) == dt.date(2028, 4, 16)


def test_calendar_no_longer_fails_open_beyond_2027():
    """THE regression: every 2028 weekday used to count as a trading day."""
    from core.market_calendar import _fallback_is_trading_day, us_market_holidays
    h28 = us_market_holidays(2028)
    assert dt.date(2028, 1, 17) in h28          # MLK (3rd Monday)
    assert dt.date(2028, 4, 14) in h28          # Good Friday (Easter Apr 16)
    assert dt.date(2028, 11, 23) in h28         # Thanksgiving (4th Thursday)
    assert dt.date(2028, 12, 25) in h28         # Christmas (Monday)
    assert dt.date(2028, 7, 4) in h28           # July 4th (Tuesday)
    assert _fallback_is_trading_day(dt.date(2028, 11, 23)) is False
    assert _fallback_is_trading_day(dt.date(2028, 11, 24)) is True   # day after: half day, but open
    # NYSE quirk: a SATURDAY New Year's Day is not observed — Dec 31 2027 trades.
    assert dt.date(2028, 1, 1).isoformat() not in {d.isoformat() for d in h28}
    assert _fallback_is_trading_day(dt.date(2027, 12, 31)) is True
    # Sunday observation: July 4 2032 -> observed Monday July 5.
    from core.market_calendar import us_market_holidays as H
    assert dt.date(2032, 7, 5) in H(2032)


def test_early_close_day_after_thanksgiving_any_year():
    from core.market_calendar import early_close_et
    assert early_close_et(dt.date(2028, 11, 24)) == 13   # beyond the old list's reach


# ─────────────────────── degraded-sim guard ───────────────────────

def test_degraded_sim_detects_real_history():
    """A sim adapter over a store that has traded against a REAL broker is the
    failed-connect fallback — it must be refused, not run."""
    import types
    from core.execution import degraded_sim

    sim = types.SimpleNamespace(name="sim")
    real = types.SimpleNamespace(name="ibkr_paper")
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "p.db")
        # Fresh dev store: sim is fine.
        assert degraded_sim(st, sim) is False
        # The store has seen a real broker: sim is now a degraded fallback.
        st.set_kv("broker", "ibkr_paper")
        assert degraded_sim(st, sim) is True
        # A real adapter is never degraded.
        assert degraded_sim(st, real) is False
        st.close()


# ─────────────────────── FX fail-closed ───────────────────────

def test_unconverted_foreign_account_fails_closed():
    """Raw CAD figures would loosen every USD cap by the FX rate (~1.42x). The
    gate fails closed on equity<=0, so zeroing is the correct closed behavior."""
    from core.brokers.ibkr import fail_closed_if_unconverted

    cad_no_fx = {"equity": 982834.73, "cash": 982025.49, "buying_power": 3263892.78,
                 "base_currency": "CAD", "usd_rate": 0.0, "converted": False}
    out = fail_closed_if_unconverted(cad_no_fx)
    assert out["equity"] == 0.0 and out["cash"] == 0.0 and out["buying_power"] == 0.0
    assert out.get("fx_failed_closed") is True
    assert cad_no_fx["equity"] == 982834.73, "input must not be mutated"

    # Converted CAD and native USD pass through untouched.
    cad_ok = dict(cad_no_fx, converted=True, usd_rate=1.42)
    assert fail_closed_if_unconverted(cad_ok)["equity"] == 982834.73
    usd = {"equity": 1e6, "cash": 1e6, "buying_power": 2e6,
           "base_currency": "USD", "usd_rate": 1.0, "converted": False}
    assert fail_closed_if_unconverted(usd)["equity"] == 1e6


# ─────────────────────── leg-overlap block ───────────────────────

def _seed_position(store, short=735.0, long_=736.0, exp="2026-08-31"):
    return store.open_position({
        "client_order_id": "AIS-a", "strategy": "volatility_breakout",
        "structure": "put_debit_spread", "family": "put", "is_credit": 0,
        "underlying": "SPY", "status": "open", "opened_asof": "2026-07-29",
        "opened_ts": "t", "expiration": exp, "contracts": 1,
        "short_strike": short, "long_strike": long_, "width": 1.0,
        "legs_json": "[]", "entry_credit_ps": 0.31, "max_loss": 31.0})


def test_order_sharing_a_leg_with_the_book_is_refused():
    """Two positions on one contract make the broker's NET leg map ambiguous for
    every resolver (fills, closes, assignment checks). The state is refused at
    entry so it can never exist."""
    from core.brokers.base import OrderLeg, OrderRequest
    from core.execution import _order_overlaps_book

    def _order(short, long_, exp="2026-08-31"):
        return OrderRequest(
            client_order_id="AIS-new",
            legs=[OrderLeg("S", "sell", 1, "option", "put", strike=short, expiration=exp),
                  OrderLeg("L", "buy", 1, "option", "put", strike=long_, expiration=exp)],
            limit_price=0.30, strategy="put_credit_spread", underlying="SPY")

    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "p.db")
        _seed_position(st)
        # Shares the 735P leg -> refused, and the clash names the contract.
        clash = _order_overlaps_book(_order(735.0, 730.0), st)
        assert clash is not None and "735" in clash
        # Same strikes, DIFFERENT expiration -> different contracts, fine.
        assert _order_overlaps_book(_order(735.0, 730.0, exp="2026-09-18"), st) is None
        # Disjoint strikes, same expiration -> fine.
        assert _order_overlaps_book(_order(720.0, 715.0), st) is None
        st.close()


# ─────────────── preflight-review fixes (found BEFORE deployment) ───────────────

def test_within_rth_minutes_matter():
    """The hour-only check admitted 9:00-9:29 premarket — exactly where a
    UTC-written crontab lands after a DST transition."""
    from core.market_calendar import within_rth
    assert within_rth(9, 29) is False       # premarket
    assert within_rth(9, 30) is True        # the open
    assert within_rth(15, 59) is True
    assert within_rth(16, 0) is False       # the close
    assert within_rth(6, 0) is False        # UTC-drift morning cron
    # Half day: closes at 13:00.
    assert within_rth(12, 59, close_hour=13) is True
    assert within_rth(13, 0, close_hour=13) is False


def test_degraded_sim_survives_a_blinding_sim_cycle():
    """CRITICAL preflight finding: the guard read the rolling 'broker' kv, which
    every cycle overwrites — so one pre-guard sim-fallback cycle (kv broker='sim')
    blinded it exactly when it mattered. The sticky write-once marker cannot be
    blinded by the thing it guards against."""
    import types
    from core.execution import degraded_sim

    sim = types.SimpleNamespace(name="sim")
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "p.db")
        # The blinding scenario: a sim-fallback cycle ran LAST (kv broker='sim')
        # but the store HAS real history (sticky flag set by an earlier real cycle).
        st.set_kv("broker", "sim")
        st.set_kv("ever_real_broker", "1")
        assert degraded_sim(st, sim) is True, "sticky marker must survive the overwrite"
        st.close()


def test_factory_marks_fallback_as_degraded_but_not_explicit_sim():
    from core.brokers.factory import build_execution_adapter
    from core.config import Config

    def _cfg(brokers):
        return Config(raw={}, mode="paper",
                      account={"type": "margin", "region": "CA",
                               "starting_equity_usd": 30000,
                               "options_approval_level": 3},
                      brokers=brokers, risk={}, strategies={}, watchlist=[])

    explicit = build_execution_adapter(_cfg({"execution": "sim"}))
    assert explicit.adapter.name == "sim" and explicit.degraded is False

    # ibkr requested, nothing listens on the port -> FALLBACK, flagged degraded.
    fallen = build_execution_adapter(_cfg({"execution": "ibkr_paper",
                                           "ibkr_port": 4999}))
    assert fallen.adapter.name == "sim" and fallen.degraded is True

    typo = build_execution_adapter(_cfg({"execution": "no_such_broker"}))
    assert typo.degraded is True


def test_cycle_lock_is_exclusive_per_process():
    """The shell flock covers only cron; the documented manual path bypassed it.
    The Python lock covers every entrypoint; second acquisition must fail."""
    import cli
    assert cli._acquire_cycle_lock() is True
    try:
        assert cli._acquire_cycle_lock() is False, "second acquire must be refused"
    finally:
        if cli._CYCLE_LOCK_HANDLE is not None:      # release for other tests
            cli._CYCLE_LOCK_HANDLE.close()
            cli._CYCLE_LOCK_HANDLE = None


def test_overlap_check_fails_closed_on_corrupt_row():
    """One malformed active position (condor with no legs_json) must block ITS
    underlying rather than crashing every future entry or being ignored."""
    from core.brokers.base import OrderLeg, OrderRequest
    from core.execution import _order_overlaps_book

    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "p.db")
        st.open_position({
            "client_order_id": "AIS-c", "strategy": "earnings_vol",
            "structure": "iron_condor", "family": "put", "is_credit": 1,
            "underlying": "QQQ", "status": "open", "opened_asof": "2026-08-01",
            "opened_ts": "t", "expiration": "2026-09-18", "contracts": 1,
            "short_strike": 0.0, "long_strike": 0.0, "width": 5.0,
            "legs_json": None,          # corrupt: unparseable legs
            "entry_credit_ps": 1.0, "max_loss": 400.0})
        order = OrderRequest(
            client_order_id="AIS-new",
            legs=[OrderLeg("S", "sell", 1, "option", "put", strike=500.0,
                           expiration="2026-10-16")],
            limit_price=0.3, strategy="put_credit_spread", underlying="QQQ")
        clash = _order_overlaps_book(order, st)
        assert clash is not None and "failing closed" in clash
        # A different underlying is unaffected.
        other = OrderRequest(
            client_order_id="AIS-o",
            legs=[OrderLeg("S", "sell", 1, "option", "put", strike=500.0,
                           expiration="2026-10-16")],
            limit_price=0.3, strategy="put_credit_spread", underlying="SPY")
        assert _order_overlaps_book(other, st) is None
        st.close()
