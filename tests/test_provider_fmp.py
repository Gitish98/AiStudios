"""Offline unit tests for FMPProvider. No network, no keys, no pytest import."""

from core.data.base import DataUnavailable, NotSupported
from core.data.providers.fmp import FMPProvider


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
    payload = {"symbol": "AAPL", "historical": [
        {"date": "2023-01-03", "open": 1.0, "high": 2.0,
         "low": 0.5, "close": 1.5, "volume": 1000},
    ]}
    p = FMPProvider(api_key="k", http=_fake_http(payload, captured))
    bars = p.get_bars("AAPL", days=30)
    assert bars == [{"date": "2023-01-03", "o": 1.0, "h": 2.0,
                     "l": 0.5, "c": 1.5, "v": 1000.0}]
    assert "/historical-price-full/AAPL" in captured["url"]
    assert captured["params"]["timeseries"] == 30
    assert captured["params"]["apikey"] == "k"


def test_get_bars_missing_fields():
    payload = {"historical": [{"date": "2023-01-03"}]}
    p = FMPProvider(api_key="k", http=_fake_http(payload))
    bars = p.get_bars("AAPL")
    assert bars[0]["o"] == 0.0 and bars[0]["c"] == 0.0


def test_get_bars_bad_response_raises():
    p = FMPProvider(api_key="k", http=_fake_http({"symbol": "AAPL"}))
    try:
        p.get_bars("AAPL")
        assert False
    except DataUnavailable:
        pass


def test_get_fundamentals_normalizes():
    captured = {}
    payload = [{
        "symbol": "AAPL", "mktCap": 2500000000000, "pe": 28.5,
        "sector": "Technology", "beta": 1.25, "sharesOutstanding": 16000000000,
    }]
    p = FMPProvider(api_key="k", http=_fake_http(payload, captured))
    f = p.get_fundamentals("AAPL")
    assert f == {
        "market_cap": 2500000000000.0, "pe": 28.5, "sector": "Technology",
        "beta": 1.25, "shares_out": 16000000000.0,
    }
    assert "/profile/AAPL" in captured["url"]


def test_get_fundamentals_missing_fields():
    payload = [{"symbol": "AAPL"}]
    p = FMPProvider(api_key="k", http=_fake_http(payload))
    f = p.get_fundamentals("AAPL")
    assert f["market_cap"] is None and f["pe"] is None
    assert f["sector"] is None and f["beta"] is None and f["shares_out"] is None


def test_get_fundamentals_empty_raises():
    p = FMPProvider(api_key="k", http=_fake_http([]))
    try:
        p.get_fundamentals("AAPL")
        assert False
    except DataUnavailable:
        pass


def test_get_earnings_next_upcoming():
    captured = {}
    payload = [
        {"date": "2020-01-01", "time": "amc", "epsEstimated": 1.0},
        {"date": "2099-05-10", "time": "bmo", "epsEstimated": 2.5},
        {"date": "2099-08-01", "time": "amc", "epsEstimated": 3.0},
    ]
    p = FMPProvider(api_key="k", http=_fake_http(payload, captured))
    e = p.get_earnings("AAPL")
    assert e == {"date": "2099-05-10", "time": "bmo", "eps_estimate": 2.5}
    assert "/historical/earning_calendar/AAPL" in captured["url"]


def test_get_earnings_unknown_time():
    payload = [{"date": "2099-05-10", "time": "", "epsEstimated": None}]
    p = FMPProvider(api_key="k", http=_fake_http(payload))
    e = p.get_earnings("AAPL")
    assert e["time"] == "unknown" and e["eps_estimate"] is None


def test_get_earnings_none_upcoming():
    payload = [{"date": "2000-01-01", "time": "bmo", "epsEstimated": 1.0}]
    p = FMPProvider(api_key="k", http=_fake_http(payload))
    assert p.get_earnings("AAPL") is None


def test_get_earnings_bad_response_raises():
    p = FMPProvider(api_key="k", http=_fake_http({"error": "x"}))
    try:
        p.get_earnings("AAPL")
        assert False
    except DataUnavailable:
        pass


def test_no_key_raises():
    import os
    os.environ.pop("FMP_API_KEY", None)
    p = FMPProvider()
    for fn in (p.get_bars, p.get_fundamentals, p.get_earnings):
        try:
            fn("AAPL")
            assert False, "expected DataUnavailable"
        except DataUnavailable:
            pass


def test_supports():
    p = FMPProvider(api_key="k", http=_fake_http([]))
    assert p.supports("bars") and p.supports("fundamentals")
    assert p.supports("earnings")
    try:
        p.get_news("AAPL")
        assert False
    except NotSupported:
        pass
    assert not p.supports("news")


def run():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("all fmp tests passed")


if __name__ == "__main__":
    run()
