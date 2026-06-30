"""
Offline tests for the DataHub aggregator and build_data_hub factory.

No network, no keys, no pytest import at module top — plain asserts so the
repo's stdlib runner (tests/run.py) can execute them too.
"""

from __future__ import annotations

from core.data.aggregator import DataHub
from core.data.base import DataProvider, DataUnavailable
from core.data import factory


# ── fakes ────────────────────────────────────────────────────────────────────
class _FakeBase(DataProvider):
    def __init__(self, key=True):
        super().__init__(api_key="x" if key else None)


class FakeBarsA(_FakeBase):
    name = "fakeA"

    def get_bars(self, symbol, days=252):
        return [{"date": "2026-01-02", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100,
                 "src": "A"}]

    def get_news(self, symbol, limit=20):
        return [{"headline": "Shared headline", "src": "A"},
                {"headline": "Only A", "src": "A"}]


class FakeBarsB(_FakeBase):
    name = "fakeB"

    def get_bars(self, symbol, days=252):
        return [{"date": "2026-01-02", "c": 9.9, "src": "B"}]

    def get_news(self, symbol, limit=20):
        return [{"headline": "shared HEADLINE", "src": "B"},
                {"headline": "Only B", "src": "B"}]


class FakeBarsUnavailable(_FakeBase):
    name = "fakeU"

    def get_bars(self, symbol, days=252):
        raise DataUnavailable("boom")


class FakeKeyless(_FakeBase):
    name = "fakeKeyless"

    def __init__(self):
        super().__init__(key=False)

    def get_bars(self, symbol, days=252):
        raise AssertionError("keyless provider must never be called")


# ── routing ──────────────────────────────────────────────────────────────────
def test_get_bars_uses_first_capable_provider():
    hub = DataHub([FakeBarsA(), FakeBarsB()])
    bars = hub.get_bars("AAPL")
    assert bars[0]["src"] == "A"


def test_keyless_provider_skipped_in_routing():
    hub = DataHub([FakeKeyless(), FakeBarsB()])
    bars = hub.get_bars("AAPL")
    assert bars[0]["src"] == "B"


# ── fallback on DataUnavailable ──────────────────────────────────────────────
def test_fallback_on_data_unavailable():
    hub = DataHub([FakeBarsUnavailable(), FakeBarsA()])
    bars = hub.get_bars("AAPL")
    assert bars[0]["src"] == "A"


def test_empty_defaults_when_none_succeed():
    hub = DataHub([FakeBarsUnavailable()])
    assert hub.get_bars("AAPL") == []
    assert hub.get_earnings("AAPL") is None
    assert hub.get_fundamentals("AAPL") == {}
    assert hub.get_news("AAPL") == []


# ── news aggregation + dedupe ────────────────────────────────────────────────
def test_news_aggregated_and_deduped_by_headline():
    hub = DataHub([FakeBarsA(), FakeBarsB()])
    news = hub.get_news("AAPL")
    headlines = [n["headline"] for n in news]
    # "Shared headline" / "shared HEADLINE" collapse to one entry.
    assert len(news) == 3
    assert "Only A" in headlines and "Only B" in headlines


def test_news_respects_limit():
    hub = DataHub([FakeBarsA(), FakeBarsB()])
    news = hub.get_news("AAPL", limit=1)
    assert len(news) == 1


# ── factory ──────────────────────────────────────────────────────────────────
def test_factory_skips_keyless_providers(monkeypatch=None):
    import os

    # Ensure no real keys leak in from the environment.
    for var in ("FINNHUB_API_KEY", "TIINGO_API_KEY", "FMP_API_KEY",
                "ALPHAVANTAGE_API_KEY"):
        os.environ.pop(var, None)

    cfg = {"data": {"providers": ["finnhub", "tiingo"]}}
    hub = factory.build_data_hub(cfg)
    assert hub.providers == []  # both keyless -> skipped


def test_factory_keeps_keyed_provider():
    import os

    os.environ["FINNHUB_API_KEY"] = "test-key"
    os.environ.pop("TIINGO_API_KEY", None)
    try:
        cfg = {"data": {"providers": ["finnhub", "tiingo"]}}
        hub = factory.build_data_hub(cfg)
        assert len(hub.providers) == 1
        assert hub.providers[0].name == "finnhub"
    finally:
        os.environ.pop("FINNHUB_API_KEY", None)


if __name__ == "__main__":
    for _n, _f in sorted(globals().items()):
        if _n.startswith("test_") and callable(_f):
            _f()
            print("PASS", _n)
