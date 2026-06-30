"""
AlphaVantageProvider — daily bars + news sentiment from Alpha Vantage
(https://www.alphavantage.co).

Read-only. HTTP goes through the injectable self._http transport so tests run
fully offline. Auth uses the `apikey` query parameter. Alpha Vantage signals
rate-limit / informational throttling with 'Note' or 'Information' keys in the
JSON body; those are mapped to DataUnavailable.
"""

from __future__ import annotations

from typing import Optional

from core.data.base import DataProvider, DataUnavailable


class AlphaVantageProvider(DataProvider):
    name = "alphavantage"

    BASE_URL = "https://www.alphavantage.co/query"

    def __init__(self, api_key: Optional[str] = None, http=None):
        super().__init__(api_key=api_key, http=http, env_key="ALPHAVANTAGE_API_KEY")

    def _check_throttle(self, data) -> None:
        """Alpha Vantage returns 200 with a 'Note'/'Information' body when
        rate-limited or otherwise refusing to serve data."""
        if isinstance(data, dict) and ("Note" in data or "Information" in data):
            msg = data.get("Note") or data.get("Information") or "rate limited"
            raise DataUnavailable(f"{self.name}: {msg}")

    def get_bars(self, symbol: str, days: int = 252) -> list[dict]:
        self._require_key()
        params = {
            "function": "TIME_SERIES_DAILY",
            "symbol": symbol,
            "outputsize": "full",
            "apikey": self._key,
        }
        data = self._http(self.BASE_URL, params, None)
        self._check_throttle(data)
        if not isinstance(data, dict):
            raise DataUnavailable(f"{self.name}: bad bars response for {symbol}")

        series = data.get("Time Series (Daily)")
        if not isinstance(series, dict) or not series:
            raise DataUnavailable(f"{self.name}: no bars for {symbol}")

        bars: list[dict] = []
        for date in sorted(series.keys()):
            row = series.get(date)
            if not isinstance(row, dict):
                continue
            try:
                bars.append({
                    "date": str(date)[:10],
                    "o": float(row.get("1. open", 0.0) or 0.0),
                    "h": float(row.get("2. high", 0.0) or 0.0),
                    "l": float(row.get("3. low", 0.0) or 0.0),
                    "c": float(row.get("4. close", 0.0) or 0.0),
                    "v": float(row.get("5. volume", 0.0) or 0.0),
                })
            except (TypeError, ValueError):
                continue

        if not bars:
            raise DataUnavailable(f"{self.name}: no bars for {symbol}")

        n = max(int(days), 1)
        return bars[-n:]

    def get_news(self, symbol: str, limit: int = 20) -> list[dict]:
        self._require_key()
        params = {
            "function": "NEWS_SENTIMENT",
            "tickers": symbol,
            "apikey": self._key,
        }
        data = self._http(self.BASE_URL, params, None)
        self._check_throttle(data)
        if not isinstance(data, dict):
            raise DataUnavailable(f"{self.name}: bad news response for {symbol}")

        feed = data.get("feed")
        if not isinstance(feed, list):
            raise DataUnavailable(f"{self.name}: bad news response for {symbol}")

        items: list[dict] = []
        for row in feed:
            if not isinstance(row, dict):
                continue
            score = row.get("overall_sentiment_score")
            sentiment: Optional[float]
            try:
                sentiment = float(score) if score is not None else None
            except (TypeError, ValueError):
                sentiment = None
            if sentiment is not None:
                sentiment = max(-1.0, min(1.0, sentiment))
            items.append({
                "ts": str(row.get("time_published", "") or ""),
                "headline": str(row.get("title", "") or ""),
                "summary": str(row.get("summary", "") or ""),
                "url": str(row.get("url", "") or ""),
                "source": str(row.get("source", "") or ""),
                "sentiment": sentiment,
            })
            if len(items) >= max(int(limit), 1):
                break

        if not items:
            raise DataUnavailable(f"{self.name}: no news for {symbol}")
        return items
