"""Offline unit tests for AlphaVantageProvider. No network, no keys, no pytest import."""

from core.data.base import DataUnavailable, NotSupported
from core.data.providers.alphavantage import AlphaVantageProvider


def _fake_http(payload, captured=None):
    def http(url, params=None, headers=None):
        if captured is not None:
            captured["url"] = url
            captured["params"] = params
            captured["headers"] = headers
        return payload
    return http


def test_get_bars_normalizes_sorts_and_limits():
    captured = {}
    payload = {
        "Time Series (Daily)": {
            "2023-01-04": {"1. open": "2.0", "2. high": "3.0", "3. low": "1.0",
                           "4. close": "2.5", "5. volume": "200"},
            "2023-01-03": {"1. open": "1.0", "2. high": "2.0", "3. low": "0.5",
                           "4. close": "1.5", "5. volume": "100"},
            "2023-01-02": {"1. open": "0.5", "2. high": "1.0", "3. low": "0.2",
                           "4. close": "0.8", "5. volume": "50"},
        }
    }
    p = AlphaVantageProvider(api_key="k", http=_fake_http(payload, captured))
    bars = p.get_bars("AAPL", days=2)
    # sorted ascending, limited to last 2
    assert bars == [
        {"date": "2023-01-03", "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 100.0},
        {"date": "2023-01-04", "o": 2.0, "h": 3.0, "l": 1.0, "c": 2.5, "v": 200.0},
    ]
    assert captured["params"]["function"] == "TIME_SERIES_DAILY"
    assert captured["params"]["symbol"] == "AAPL"
    assert captured["params"]["outputsize"] == "full"
    assert captured["params"]["apikey"] == "k"


def test_get_bars_missing_fields():
    payload = {"Time Series (Daily)": {"2023-01-03": {}}}
    p = AlphaVantageProvider(api_key="k", http=_fake_http(payload))
    bars = p.get_bars("AAPL")
    assert bars[0]["o"] == 0.0 and bars[0]["c"] == 0.0


def test_get_news_normalizes_and_clamps():
    captured = {}
    payload = {
        "feed": [
            {"time_published": "20230103T120000", "title": "T", "summary": "S",
             "url": "http://x", "source": "src", "overall_sentiment_score": "0.3"},
            {"time_published": "20230104T120000", "title": "T2", "summary": "S2",
             "url": "http://y", "source": "src2", "overall_sentiment_score": 5.0},
        ]
    }
    p = AlphaVantageProvider(api_key="k", http=_fake_http(payload, captured))
    news = p.get_news("AAPL", limit=10)
    assert news[0] == {"ts": "20230103T120000", "headline": "T", "summary": "S",
                       "url": "http://x", "source": "src", "sentiment": 0.3}
    assert news[1]["sentiment"] == 1.0  # clamped to [-1, 1]
    assert captured["params"]["function"] == "NEWS_SENTIMENT"
    assert captured["params"]["tickers"] == "AAPL"


def test_news_limit_applied():
    payload = {"feed": [
        {"title": "a"}, {"title": "b"}, {"title": "c"},
    ]}
    p = AlphaVantageProvider(api_key="k", http=_fake_http(payload))
    news = p.get_news("AAPL", limit=2)
    assert len(news) == 2


def test_news_missing_fields():
    payload = {"feed": [{}]}
    p = AlphaVantageProvider(api_key="k", http=_fake_http(payload))
    news = p.get_news("AAPL")
    assert news[0]["headline"] == "" and news[0]["sentiment"] is None


def test_rate_limit_note_raises():
    note = {"Note": "Thank you for using Alpha Vantage! ... 5 calls per minute"}
    info = {"Information": "the standard API rate limit is 25 requests per day"}
    for payload in (note, info):
        p = AlphaVantageProvider(api_key="k", http=_fake_http(payload))
        for fn in (p.get_bars, p.get_news):
            try:
                fn("AAPL")
                assert False, "expected DataUnavailable"
            except DataUnavailable:
                pass


def test_no_key_raises():
    import os
    os.environ.pop("ALPHAVANTAGE_API_KEY", None)
    p = AlphaVantageProvider()
    try:
        p.get_bars("AAPL")
        assert False, "expected DataUnavailable"
    except DataUnavailable:
        pass


def test_empty_response_raises():
    p = AlphaVantageProvider(api_key="k", http=_fake_http({}))
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
    p = AlphaVantageProvider(api_key="k", http=_fake_http([1, 2, 3]))
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


def test_unsupported_classes():
    p = AlphaVantageProvider(api_key="k", http=_fake_http({}))
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
    print("all alphavantage tests passed")


if __name__ == "__main__":
    run()
