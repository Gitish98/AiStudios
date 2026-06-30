"""
FMPProvider — market data, fundamentals, and earnings from Financial Modeling
Prep (https://financialmodelingprep.com).

Read-only. HTTP goes through the injectable self._http transport so tests run
fully offline. Auth uses the ?apikey= query parameter scheme.
"""

from __future__ import annotations

from typing import Optional

from core.data.base import DataProvider, DataUnavailable


class FMPProvider(DataProvider):
    name = "fmp"

    BASE = "https://financialmodelingprep.com/api/v3"
    BARS_URL = BASE + "/historical-price-full/{symbol}"
    PROFILE_URL = BASE + "/profile/{symbol}"
    EARNINGS_URL = BASE + "/historical/earning_calendar/{symbol}"

    def __init__(self, api_key: Optional[str] = None, http=None):
        super().__init__(api_key=api_key, http=http, env_key="FMP_API_KEY")

    @staticmethod
    def _f(value) -> Optional[float]:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def get_bars(self, symbol: str, days: int = 252) -> list[dict]:
        self._require_key()
        url = self.BARS_URL.format(symbol=symbol)
        params = {"timeseries": int(days), "apikey": self._key}
        data = self._http(url, params, None)
        if not isinstance(data, dict):
            raise DataUnavailable(f"{self.name}: bad bars response for {symbol}")
        rows = data.get("historical")
        if not isinstance(rows, list):
            raise DataUnavailable(f"{self.name}: no bars for {symbol}")

        bars: list[dict] = []
        for row in rows:
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

    def get_fundamentals(self, symbol: str) -> dict:
        self._require_key()
        url = self.PROFILE_URL.format(symbol=symbol)
        params = {"apikey": self._key}
        data = self._http(url, params, None)
        # /profile returns a list with one object.
        if isinstance(data, list):
            data = data[0] if data and isinstance(data[0], dict) else None
        if not isinstance(data, dict) or not data:
            raise DataUnavailable(f"{self.name}: no fundamentals for {symbol}")

        sector = data.get("sector")
        return {
            "market_cap": self._f(data.get("mktCap")),
            "pe": self._f(data.get("pe")),
            "sector": str(sector) if sector else None,
            "beta": self._f(data.get("beta")),
            "shares_out": self._f(data.get("sharesOutstanding")),
        }

    def get_earnings(self, symbol: str) -> Optional[dict]:
        self._require_key()
        url = self.EARNINGS_URL.format(symbol=symbol)
        params = {"apikey": self._key}
        data = self._http(url, params, None)
        if not isinstance(data, list):
            raise DataUnavailable(f"{self.name}: bad earnings response for {symbol}")

        import datetime as _dt
        today = _dt.date.today().isoformat()

        upcoming: list[dict] = []
        for row in data:
            if not isinstance(row, dict):
                continue
            date = row.get("date")
            if not date:
                continue
            if str(date)[:10] >= today:
                upcoming.append(row)

        if not upcoming:
            return None

        # Pick the nearest upcoming event.
        best = min(upcoming, key=lambda r: str(r.get("date"))[:10])

        raw_time = str(best.get("time", "") or "").lower()
        if raw_time in ("bmo", "amc"):
            time = raw_time
        else:
            time = "unknown"

        return {
            "date": str(best.get("date"))[:10],
            "time": time,
            "eps_estimate": self._f(best.get("epsEstimated")),
        }
