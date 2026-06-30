"""
TiingoProvider — market data + news from Tiingo (https://api.tiingo.com).

Read-only. HTTP goes through the injectable self._http transport so tests run
fully offline. Auth uses the Tiingo token header scheme.
"""

from __future__ import annotations

from typing import Optional

from core.data.base import DataProvider, DataUnavailable


class TiingoProvider(DataProvider):
    name = "tiingo"

    BARS_URL = "https://api.tiingo.com/tiingo/daily/{symbol}/prices"
    NEWS_URL = "https://api.tiingo.com/tiingo/news"

    def __init__(self, api_key: Optional[str] = None, http=None):
        super().__init__(api_key=api_key, http=http, env_key="TIINGO_API_KEY")

    def _headers(self) -> dict:
        return {"Authorization": f"Token {self._key}"}

    @staticmethod
    def _start_date(days: int) -> str:
        import datetime as _dt

        start = _dt.date.today() - _dt.timedelta(days=max(int(days), 1))
        return start.isoformat()

    def get_bars(self, symbol: str, days: int = 252) -> list[dict]:
        self._require_key()
        url = self.BARS_URL.format(symbol=symbol)
        params = {
            "startDate": self._start_date(days),
            "format": "json",
        }
        data = self._http(url, params, self._headers())
        if not isinstance(data, list):
            raise DataUnavailable(f"{self.name}: bad bars response for {symbol}")

        bars: list[dict] = []
        for row in data:
            if not isinstance(row, dict):
                continue
            date = row.get("date")
            if not date:
                continue
            try:
                bars.append({
                    "date": str(date)[:10],
                    "o": float(row.get("open", 0.0) or 0.0),
                    "h": float(row.get("high", 0.0) or 0.0),
                    "l": float(row.get("low", 0.0) or 0.0),
                    "c": float(row.get("close", 0.0) or 0.0),
                    "v": float(row.get("volume", 0.0) or 0.0),
                })
            except (TypeError, ValueError):
                continue

        if not bars:
            raise DataUnavailable(f"{self.name}: no bars for {symbol}")
        return bars

    def get_news(self, symbol: str, limit: int = 20) -> list[dict]:
        self._require_key()
        params = {"tickers": symbol, "limit": int(limit)}
        data = self._http(self.NEWS_URL, params, self._headers())
        if not isinstance(data, list):
            raise DataUnavailable(f"{self.name}: bad news response for {symbol}")

        items: list[dict] = []
        for row in data:
            if not isinstance(row, dict):
                continue
            items.append({
                "ts": str(row.get("publishedDate", "") or ""),
                "headline": str(row.get("title", "") or ""),
                "summary": str(row.get("description", "") or ""),
                "url": str(row.get("url", "") or ""),
                "source": str(row.get("source", "") or ""),
                "sentiment": None,
            })

        if not items:
            raise DataUnavailable(f"{self.name}: no news for {symbol}")
        return items
