"""Offline unit tests for TiingoProvider. No network, no keys, no pytest import."""

from core.data.base import DataUnavailable, NotSupported
from core.data.providers.tiingo import TiingoProvider


def _fake_http(payload, captured=None):
    def http(url, params=None, headers=None):
        if captured is not None:
            captured["url"] = url
            captured["params"] = params
            captured["headers"] = headers
        return payload
    return http


def test_get_bars_normalizes():
    captured = {}
    payload = [
        {"date": "2023-01-03T00:00:00.000Z", "open": 1.0, "high": 2.0,
         "low": 0.5, "close": 1.5, "volume": 1000},
    ]
    p = TiingoProvider(api_key="k", http=_fake_http(payload, captured))
    bars = p.get_bars("AAPL", days=30)
    assert bars == [{"date": "2023-01-03", "o": 1.0, "h": 2.0,
                     "l": 0.5, "c": 1.5, "v": 1000.0}]
    assert captured["headers"] == {"Authorization": "Token k"}
    assert "/tiingo/daily/AAPL/prices" in captured["url"]
    assert captured["params"]["format"] == "json"


def test_get_bars_missing_fields():
    payload = [{"date": "2023-01-03"}]  # only date
    p = TiingoProvider(api_key="k", http=_fake_http(payload))
    bars = p.get_bars("AAPL")
    assert bars[0]["o"] == 0.0 and bars[0]["c"] == 0.0


def test_get_news_normalizes():
    captured = {}
    payload = [
        {"publishedDate": "2023-01-03T12:00:00Z", "title": "T", "description": "D",
         "url": "http://x", "source": "src"},
    ]
    p = TiingoProvider(api_key="k", http=_fake_http(payload, captured))
    news = p.get_news("AAPL", limit=5)
    assert news == [{"ts": "2023-01-03T12:00:00Z", "headline": "T", "summary": "D",
                     "url": "http://x", "source": "src", "sentiment": None}]
    assert captured["params"] == {"tickers": "AAPL", "limit": 5}


def test_news_missing_fields():
    payload = [{}]
    p = TiingoProvider(api_key="k", http=_fake_http(payload))
    news = p.get_news("AAPL")
    assert news[0]["headline"] == "" and news[0]["sentiment"] is None


def test_no_key_raises():
    import os
    os.environ.pop("TIINGO_API_KEY", None)
    p = TiingoProvider()
    try:
        p.get_bars("AAPL")
        assert False, "expected DataUnavailable"
    except DataUnavailable:
        pass


def test_empty_response_raises():
    p = TiingoProvider(api_key="k", http=_fake_http([]))
    try:
        p.get_bars("AAPL")
        assert False
    except DataUnavailable:
        pass
    try:
        p.get_news("AAPL")
        assert False
    except DataUnavailable:
        pass


def test_bad_response_raises():
    p = TiingoProvider(api_key="k", http=_fake_http({"error": "x"}))
    try:
        p.get_bars("AAPL")
        assert False
    except DataUnavailable:
        pass


def test_unsupported_classes():
    p = TiingoProvider(api_key="k", http=_fake_http([]))
    for fn in (p.get_earnings, p.get_fundamentals):
        try:
            fn("AAPL")
            assert False
        except NotSupported:
            pass
    assert p.supports("bars") and p.supports("news")
    assert not p.supports("earnings") and not p.supports("fundamentals")


def run():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("all tiingo tests passed")


if __name__ == "__main__":
    run()
