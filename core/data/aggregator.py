"""
DataHub — ordered fan-out over DataProvider instances.

Routes each data-class request to providers in registration order, using the
FIRST provider that .supports(<class>) AND has_key. DataUnavailable/NotSupported
fall through to the next candidate. If none succeed, a safe empty value is
returned (never raises): [] for bars, {} for fundamentals, None for earnings.

News is special: it is aggregated across ALL capable+keyed providers and
deduped by headline, up to `limit`.

Read-only. Never touches the risk core.
"""

from __future__ import annotations

from typing import List, Optional

from core.data.base import DataProvider, DataUnavailable, NotSupported


class DataHub:
    def __init__(self, providers: Optional[List[DataProvider]] = None):
        self.providers: List[DataProvider] = list(providers or [])

    # ── internal helpers ─────────────────────────────────────────────────────
    def _candidates(self, data_class: str):
        for p in self.providers:
            try:
                if p.supports(data_class) and p.has_key:
                    yield p
            except Exception:
                continue

    def _first(self, data_class: str, call, default):
        for p in self._candidates(data_class):
            try:
                return call(p)
            except (DataUnavailable, NotSupported):
                continue
        return default

    # ── data classes ─────────────────────────────────────────────────────────
    def get_bars(self, symbol: str, days: int = 252) -> list:
        return self._first("bars", lambda p: p.get_bars(symbol, days), [])

    def get_earnings(self, symbol: str) -> Optional[dict]:
        return self._first("earnings", lambda p: p.get_earnings(symbol), None)

    def get_fundamentals(self, symbol: str) -> dict:
        return self._first("fundamentals", lambda p: p.get_fundamentals(symbol), {})

    def get_news(self, symbol: str, limit: int = 20) -> list:
        out: list = []
        seen: set = set()
        for p in self._candidates("news"):
            try:
                items = p.get_news(symbol, limit)
            except (DataUnavailable, NotSupported):
                continue
            for item in items or []:
                headline = (item or {}).get("headline")
                key = headline.strip().lower() if isinstance(headline, str) else headline
                if key in seen:
                    continue
                seen.add(key)
                out.append(item)
                if len(out) >= limit:
                    return out
        return out
