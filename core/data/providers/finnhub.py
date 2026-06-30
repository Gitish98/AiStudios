"""Finnhub data provider (read-only).

Implements bars, news, earnings, and fundamentals over Finnhub's REST API.
All HTTP goes through the injectable ``self._http`` transport so tests run
fully offline. Keys come lazily from the FINNHUB_API_KEY environment variable.
"""

from __future__ import annotations

import datetime as _dt
from typing import Optional

from core.data.base import DataProvider, DataUnavailable

_BASE = "https://finnhub.io/api/v1"


def _num(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


class FinnhubProvider(DataProvider):
    name = "finnhub"

    def __init__(self, api_key: Optional[str] = None, http=None):
        super().__init__(api_key=api_key, http=http, env_key="FINNHUB_API_KEY")

    # ── bars ────────────────────────────────────────────────────────────────
    def get_bars(self, symbol: str, days: int = 252) -> list[dict]:
        self._require_key()
        now = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
        frm = now - int(days) * 86400
        data = self._http(
            f"{_BASE}/stock/candle",
            {"symbol": symbol, "resolution": "D", "from": frm, "to": now,
             "token": self._key},
            None,
        )
        if not isinstance(data, dict) or data.get("s") != "ok":
            raise DataUnavailable(f"{self.name}: no candle data for {symbol}")
        t = data.get("t") or []
        o = data.get("o") or []
        h = data.get("h") or []
        l = data.get("l") or []
        c = data.get("c") or []
        v = data.get("v") or []
        if not t:
            raise DataUnavailable(f"{self.name}: empty candles for {symbol}")
        bars = []
        for i in range(len(t)):
            try:
                date = _dt.datetime.fromtimestamp(
                    t[i], _dt.timezone.utc).strftime("%Y-%m-%d")
            except (TypeError, ValueError, OSError):
                continue
            bars.append({
                "date": date,
                "o": _num(o[i] if i < len(o) else None),
                "h": _num(h[i] if i < len(h) else None),
                "l": _num(l[i] if i < len(l) else None),
                "c": _num(c[i] if i < len(c) else None),
                "v": _num(v[i] if i < len(v) else None),
            })
        if not bars:
            raise DataUnavailable(f"{self.name}: no usable candles for {symbol}")
        return bars

    # ── news ────────────────────────────────────────────────────────────────
    def get_news(self, symbol: str, limit: int = 20) -> list[dict]:
        self._require_key()
        today = _dt.datetime.now(_dt.timezone.utc).date()
        frm = today - _dt.timedelta(days=30)
        data = self._http(
            f"{_BASE}/company-news",
            {"symbol": symbol, "from": frm.strftime("%Y-%m-%d"),
             "to": today.strftime("%Y-%m-%d"), "token": self._key},
            None,
        )
        if not isinstance(data, list):
            raise DataUnavailable(f"{self.name}: bad news response for {symbol}")
        out = []
        for item in data[:limit]:
            if not isinstance(item, dict):
                continue
            ts = item.get("datetime")
            iso = None
            if ts:
                try:
                    iso = _dt.datetime.fromtimestamp(
                        ts, _dt.timezone.utc).isoformat()
                except (TypeError, ValueError, OSError):
                    iso = None
            out.append({
                "ts": iso,
                "headline": item.get("headline", "") or "",
                "summary": item.get("summary", "") or "",
                "url": item.get("url", "") or "",
                "source": item.get("source", "") or "",
                "sentiment": None,
            })
        return out

    # ── earnings ────────────────────────────────────────────────────────────
    def get_earnings(self, symbol: str) -> Optional[dict]:
        self._require_key()
        data = self._http(
            f"{_BASE}/calendar/earnings",
            {"symbol": symbol, "token": self._key},
            None,
        )
        if not isinstance(data, dict):
            raise DataUnavailable(f"{self.name}: bad earnings response for {symbol}")
        rows = data.get("earningsCalendar") or []
        if not isinstance(rows, list) or not rows:
            return None
        today = _dt.datetime.now(_dt.timezone.utc).date().strftime("%Y-%m-%d")
        upcoming = [r for r in rows if isinstance(r, dict)
                    and r.get("date") and r.get("date") >= today]
        pool = upcoming if upcoming else [r for r in rows if isinstance(r, dict)]
        if not pool:
            return None
        nxt = min(pool, key=lambda r: r.get("date") or "9999-99-99")
        hour = (nxt.get("hour") or "").lower()
        if hour == "bmo":
            time = "bmo"
        elif hour == "amc":
            time = "amc"
        else:
            time = "unknown"
        return {
            "date": nxt.get("date"),
            "time": time,
            "eps_estimate": _num(nxt.get("epsEstimate")),
        }

    # ── fundamentals ────────────────────────────────────────────────────────
    def get_fundamentals(self, symbol: str) -> dict:
        self._require_key()
        profile = self._http(
            f"{_BASE}/stock/profile2",
            {"symbol": symbol, "token": self._key},
            None,
        )
        metrics = self._http(
            f"{_BASE}/stock/metric",
            {"symbol": symbol, "metric": "all", "token": self._key},
            None,
        )
        if not isinstance(profile, dict) and not isinstance(metrics, dict):
            raise DataUnavailable(f"{self.name}: no fundamentals for {symbol}")
        profile = profile if isinstance(profile, dict) else {}
        metrics = metrics if isinstance(metrics, dict) else {}
        m = metrics.get("metric") if isinstance(metrics.get("metric"), dict) else {}

        # market cap: Finnhub profile2 reports in millions.
        mc = _num(profile.get("marketCapitalization"))
        if mc is not None:
            mc = mc * 1_000_000.0

        return {
            "market_cap": mc,
            "pe": _num(m.get("peTTM") or m.get("peNormalizedAnnual")),
            "sector": profile.get("finnhubIndustry") or None,
            "beta": _num(m.get("beta")),
            "shares_out": _num(profile.get("shareOutstanding")),
        }
