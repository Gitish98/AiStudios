"""Tests for core.data.research.build_research_brief — OFFLINE, stdlib-runnable."""

from __future__ import annotations

from core.data.research import build_research_brief


class FakeHub:
    """A canned DataHub stand-in. Any field left None falls back to the hub's
    safe-empty value ([] / {} / None), mirroring the real DataHub contract."""

    def __init__(self, bars=None, news=None, fundamentals=None, earnings=None):
        self._bars = bars if bars is not None else []
        self._news = news if news is not None else []
        self._fund = fundamentals if fundamentals is not None else {}
        self._earn = earnings

    def get_bars(self, symbol, days=252):
        return self._bars

    def get_news(self, symbol, limit=20):
        return self._news

    def get_fundamentals(self, symbol):
        return self._fund

    def get_earnings(self, symbol):
        return self._earn


def _bars(closes):
    return [{"date": f"2026-01-{i+1:02d}", "o": c, "h": c, "l": c, "c": c, "v": 1000.0}
            for i, c in enumerate(closes)]


def test_brief_shape_full():
    closes = [100.0 + i for i in range(60)]
    news = [
        {"ts": "2026-06-01T00:00:00Z", "headline": "Up", "summary": "", "url": "",
         "source": "x", "sentiment": 0.5},
        {"ts": "2026-06-02T00:00:00Z", "headline": "Down", "summary": "", "url": "",
         "source": "x", "sentiment": -0.1},
    ]
    fund = {"market_cap": 1.0e12, "pe": 25.0, "sector": "Tech", "beta": 1.2,
            "shares_out": 1.0e9}
    earn = {"date": "2026-07-15", "time": "amc", "eps_estimate": 1.5}
    hub = FakeHub(bars=_bars(closes), news=news, fundamentals=fund, earnings=earn)

    brief = build_research_brief("AAPL", hub)

    assert brief["symbol"] == "AAPL"

    pt = brief["price_trend"]
    assert set(pt) == {"last", "sma20", "sma50", "pct_20d"}
    assert pt["last"] == 159.0
    assert abs(pt["sma20"] - sum(closes[-20:]) / 20) < 1e-9
    assert abs(pt["sma50"] - sum(closes[-50:]) / 50) < 1e-9
    expected_pct = (closes[-1] - closes[-21]) / closes[-21] * 100.0
    assert abs(pt["pct_20d"] - expected_pct) < 1e-9

    assert brief["realized_vol_annualized"] is not None
    assert brief["realized_vol_annualized"] > 0

    assert len(brief["news"]) == 2
    assert set(brief["news"][0]) == {"headline", "sentiment", "ts"}
    assert brief["news"][0]["headline"] == "Up"
    assert abs(brief["avg_news_sentiment"] - 0.2) < 1e-9

    f = brief["fundamentals"]
    assert set(f) == {"market_cap", "pe", "sector", "beta"}
    assert f["market_cap"] == 1.0e12
    assert f["sector"] == "Tech"

    assert brief["earnings"] == {"date": "2026-07-15", "time": "amc"}


def test_news_capped_at_five():
    news = [{"ts": str(i), "headline": f"h{i}", "summary": "", "url": "",
             "source": "x", "sentiment": 0.0} for i in range(8)]
    hub = FakeHub(bars=_bars([100.0] * 60), news=news)
    brief = build_research_brief("X", hub)
    assert len(brief["news"]) == 5


def test_empty_hub_yields_valid_null_brief():
    hub = FakeHub()  # all empty: [] / {} / None
    brief = build_research_brief("EMPTY", hub)

    assert brief["symbol"] == "EMPTY"
    assert brief["price_trend"] == {
        "last": None, "sma20": None, "sma50": None, "pct_20d": None}
    assert brief["realized_vol_annualized"] is None
    assert brief["news"] == []
    assert brief["avg_news_sentiment"] is None
    assert brief["fundamentals"] == {
        "market_cap": None, "pe": None, "sector": None, "beta": None}
    assert brief["earnings"] is None


class RaisingHub:
    """Every call raises — the brief must still degrade, never propagate."""

    def get_bars(self, symbol, days=252):
        raise RuntimeError("boom")

    def get_news(self, symbol, limit=20):
        raise RuntimeError("boom")

    def get_fundamentals(self, symbol):
        raise RuntimeError("boom")

    def get_earnings(self, symbol):
        raise RuntimeError("boom")


def test_raising_hub_does_not_raise():
    brief = build_research_brief("SAFE", RaisingHub())
    assert brief["symbol"] == "SAFE"
    assert brief["realized_vol_annualized"] is None
    assert brief["news"] == []
    assert brief["earnings"] is None


def test_malformed_data_degrades():
    hub = FakeHub(
        bars=[{"c": "not-a-number"}, {"c": None}, 42, {"c": 101.0}],
        news=["bad", {"headline": "ok", "sentiment": "x", "ts": "t"}],
        fundamentals={"market_cap": "huge", "pe": 12.0, "sector": 5, "beta": None},
        earnings={"eps_estimate": 1.0},  # no date/time
    )
    brief = build_research_brief("M", hub)
    assert brief["price_trend"]["last"] == 101.0
    assert brief["news"][0]["headline"] == "ok"
    assert brief["news"][0]["sentiment"] is None
    assert brief["avg_news_sentiment"] is None
    assert brief["fundamentals"]["market_cap"] is None
    assert brief["fundamentals"]["pe"] == 12.0
    assert brief["fundamentals"]["sector"] is None
    assert brief["earnings"] is None
