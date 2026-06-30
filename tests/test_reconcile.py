"""Reconcile — our tracked book vs broker truth. Uses a stub broker so we can
control exactly what the 'broker' holds."""

import tempfile
from pathlib import Path

from core.brokers.base import Account, Position
from core.brokers.sim import SimAdapter
from core.reconcile import reconcile
from core.store import Store


class StubBroker:
    """Minimal adapter-like object exposing get_positions() for reconcile."""
    name = "ibkr_paper"  # anything but 'sim' so reconcile actually compares

    def __init__(self, positions):
        self._positions = positions

    def get_positions(self):
        return self._positions


def _short(underlying, strike, exp, qty):
    return Position(symbol=f"{underlying}{strike}P", qty=qty, avg_price=0.5,
                    market_value=0, asset_class="option", underlying=underlying,
                    option_expiration=exp, option_strike=strike, option_right="P")


def _store_with_spread(tmp, short=540, long=539, exp="2026-02-19", contracts=1):
    store = Store(Path(tmp) / "t.db")
    store.open_position({
        "client_order_id": "x", "strategy": "put_credit_spread", "underlying": "SPY",
        "status": "open", "opened_asof": "2026-01-15", "opened_ts": "t",
        "expiration": exp, "contracts": contracts, "short_strike": short,
        "long_strike": long, "width": short - long, "entry_credit_ps": 0.26,
        "max_loss": 74.0,
    })
    return store


def test_in_sync_when_broker_matches():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store_with_spread(tmp)
        broker = StubBroker([
            _short("SPY", 540, "2026-02-19", -1),  # short put: -1
            _short("SPY", 539, "2026-02-19", +1),  # long put: +1
        ])
        rep = reconcile(broker, store)
        assert rep["ok"], rep["drift"]
        store.close()


def test_missing_at_broker():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store_with_spread(tmp)
        broker = StubBroker([])  # broker holds nothing
        rep = reconcile(broker, store)
        assert not rep["ok"]
        kinds = {d["kind"] for d in rep["drift"]}
        assert "missing_at_broker" in kinds
        store.close()


def test_untracked_at_broker():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")  # we track nothing
        broker = StubBroker([_short("QQQ", 480, "2026-03-20", -2)])
        rep = reconcile(broker, store)
        assert not rep["ok"]
        assert any(d["kind"] == "untracked_at_broker" for d in rep["drift"])
        store.close()


def test_qty_mismatch():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store_with_spread(tmp, contracts=1)
        broker = StubBroker([
            _short("SPY", 540, "2026-02-19", -3),  # broker holds 3, we track 1
            _short("SPY", 539, "2026-02-19", +3),
        ])
        rep = reconcile(broker, store)
        assert not rep["ok"]
        assert any(d["kind"] == "qty_mismatch" for d in rep["drift"])
        store.close()


def test_broker_fetch_failure_fails_closed():
    class Broken:
        name = "ibkr_paper"
        def get_positions(self):
            raise RuntimeError("gateway down")
    with tempfile.TemporaryDirectory() as tmp:
        store = _store_with_spread(tmp)
        rep = reconcile(Broken(), store)
        assert not rep["ok"]
        assert any(d["kind"] == "broker_unreachable" for d in rep["drift"])
        store.close()


def test_sim_is_benign_noop():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store_with_spread(tmp)
        rep = reconcile(SimAdapter(asof="2026-01-15"), store)
        assert rep["ok"]            # not flagged as drift
        assert "sim" in rep["note"].lower()
        store.close()
