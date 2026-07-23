"""Offline tests for FinnhubProvider using a fake http transport."""

import datetime as _dt

from core.data.base import DataUnavailable
from core.data.providers.finnhub import FinnhubProvider


def _fake_http(routes):
    """Build a transport that dispatches by URL substring."""
    def http(url, params=None, headers=None):
        for key, payload in routes.items():
            if key in url:
                return payload
        raise AssertionError(f"unexpected url: {url}")
    return http


_DAY = 86400
_NOW = int(_dt.datetime(2026, 6, 30, tzinfo=_dt.timezone.utc).timestamp())

CANDLES = {
    "s": "ok",
    "t": [_NOW - _DAY, _NOW],
    "o": [10.0, 11.0],
    "h": [12.0, 13.0],
    "l": [9.0, 10.5],
    "c": [11.0, 12.5],
    "v": [1000, 2000],
}

NEWS = [
    {"datetime": _NOW, "headline": "Big news", "summary": "stuff",
     "url": "http://x", "source": "Reuters"},
    {"datetime": _NOW, "headline": "More", "summary": "", "url": "", "source": ""},
]

def _future(days: int) -> str:
    import datetime as _dt
    return (_dt.datetime.now(_dt.timezone.utc).date()
            + _dt.timedelta(days=days)).strftime("%Y-%m-%d")


# Dates are RELATIVE to today: get_earnings filters to `date >= today`, so a
# hard-coded future date silently rots into a past one and the test starts
# failing for calendar reasons rather than code reasons.
EARNINGS = {
    "earningsCalendar": [
        {"date": _future(14), "hour": "amc", "epsEstimate": 1.23},
        {"date": _future(-180), "hour": "bmo", "epsEstimate": 0.5},
    ]
}

PROFILE = {"marketCapitalization": 2500.0, "finnhubIndustry": "Technology",
           "shareOutstanding": 100.0}
METRIC = {"metric": {"peTTM": 30.5, "beta": 1.4}}


def test_get_bars():
    p = FinnhubProvider(api_key="k", http=_fake_http({"stock/candle": CANDLES}))
    bars = p.get_bars("AAPL", days=5)
    assert len(bars) == 2
    assert bars[0] == {"date": "2026-06-29", "o": 10.0, "h": 12.0,
                       "l": 9.0, "c": 11.0, "v": 1000.0}
    assert bars[1]["date"] == "2026-06-30"


def test_get_bars_no_data():
    p = FinnhubProvider(api_key="k",
                        http=_fake_http({"stock/candle": {"s": "no_data"}}))
    try:
        p.get_bars("AAPL")
        assert False, "expected DataUnavailable"
    except DataUnavailable:
        pass


def test_get_news():
    p = FinnhubProvider(api_key="k", http=_fake_http({"company-news": NEWS}))
    news = p.get_news("AAPL")
    assert len(news) == 2
    assert news[0]["headline"] == "Big news"
    assert news[0]["source"] == "Reuters"
    assert news[0]["sentiment"] is None
    assert news[0]["ts"].startswith("2026-06-30")


def test_get_earnings():
    p = FinnhubProvider(api_key="k",
                        http=_fake_http({"calendar/earnings": EARNINGS}))
    e = p.get_earnings("AAPL")
    assert e == {"date": _future(14), "time": "amc", "eps_estimate": 1.23}


def test_get_earnings_none():
    p = FinnhubProvider(
        api_key="k",
        http=_fake_http({"calendar/earnings": {"earningsCalendar": []}}))
    assert p.get_earnings("AAPL") is None


def test_get_fundamentals():
    def http(url, params=None, headers=None):
        if "profile2" in url:
            return PROFILE
        if "metric" in url:
            return METRIC
        raise AssertionError(url)
    p = FinnhubProvider(api_key="k", http=http)
    f = p.get_fundamentals("AAPL")
    assert f["market_cap"] == 2500.0 * 1_000_000.0
    assert f["pe"] == 30.5
    assert f["sector"] == "Technology"
    assert f["beta"] == 1.4
    assert f["shares_out"] == 100.0


def test_requires_key():
    p = FinnhubProvider(api_key="", http=_fake_http({}))
    try:
        p.get_bars("AAPL")
        assert False, "expected DataUnavailable"
    except DataUnavailable:
        pass


def test_supports():
    p = FinnhubProvider(api_key="k")
    assert p.supports("bars")
    assert p.supports("news")
    assert p.supports("earnings")
    assert p.supports("fundamentals")


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"ok: {len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
