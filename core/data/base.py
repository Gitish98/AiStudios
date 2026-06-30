"""
DataProvider — the contract every external market-data source implements.

Design rules (so providers stay safe, testable, and swappable):
  - Pure stdlib HTTP. Each provider takes an injectable `http` transport
    (default: a urllib-based GET returning parsed JSON) so unit tests run fully
    OFFLINE with a fake transport — no network, no keys.
  - Keys come from the environment, lazily; a provider with no key raises
    DataUnavailable rather than guessing.
  - Every method returns NORMALIZED shapes (below) or raises NotSupported if the
    provider doesn't offer that data class. Callers degrade gracefully.
  - Read-only. A data provider can NEVER place an order or touch the risk gate.

Normalized shapes:
  bar        = {"date": "YYYY-MM-DD", "o": float, "h": float, "l": float, "c": float, "v": float}
  news item  = {"ts": ISO8601, "headline": str, "summary": str, "url": str,
                "source": str, "sentiment": float|None}   # sentiment in [-1, 1]
  earnings   = {"date": "YYYY-MM-DD", "time": "bmo|amc|unknown", "eps_estimate": float|None}
  fundamentals = {"market_cap": float|None, "pe": float|None, "sector": str|None,
                  "beta": float|None, "shares_out": float|None}
"""

from __future__ import annotations

import abc
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional


class DataError(RuntimeError):
    pass


class NotSupported(DataError):
    """The provider does not offer this data class."""


class DataUnavailable(DataError):
    """Missing key, network failure, or empty/invalid response."""


# An http transport is: (url: str, params: dict, headers: dict) -> parsed JSON (dict/list)
HttpFn = Callable[[str, Optional[dict], Optional[dict]], Any]


def urllib_get(url: str, params: Optional[dict] = None,
               headers: Optional[dict] = None, timeout: float = 15.0) -> Any:
    """Default transport: GET + JSON parse over stdlib (no httpx dependency)."""
    if params:
        url = url + "?" + urllib.parse.urlencode({k: v for k, v in params.items()
                                                  if v is not None})
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise DataUnavailable(f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}") from e
    except urllib.error.URLError as e:
        raise DataUnavailable(f"network error: {e.reason}") from e
    except (ValueError, json.JSONDecodeError) as e:
        raise DataUnavailable(f"bad response: {e}") from e


class DataProvider(abc.ABC):
    """Read-only external data source. Implement only what the source offers;
    leave the rest raising NotSupported (the default)."""

    name: str = "base"

    def __init__(self, api_key: Optional[str] = None, http: Optional[HttpFn] = None,
                 env_key: Optional[str] = None):
        self._key = api_key or (os.environ.get(env_key, "") if env_key else "")
        self._http: HttpFn = http or urllib_get

    @property
    def has_key(self) -> bool:
        return bool(self._key)

    def _require_key(self) -> None:
        if not self.has_key:
            raise DataUnavailable(f"{self.name}: API key not configured")

    # ── data classes (override what the provider supports) ───────────────────
    def get_bars(self, symbol: str, days: int = 252) -> list[dict]:
        raise NotSupported(f"{self.name}: bars")

    def get_news(self, symbol: str, limit: int = 20) -> list[dict]:
        raise NotSupported(f"{self.name}: news")

    def get_earnings(self, symbol: str) -> Optional[dict]:
        """Next upcoming earnings event (or None if none scheduled/known)."""
        raise NotSupported(f"{self.name}: earnings")

    def get_fundamentals(self, symbol: str) -> dict:
        raise NotSupported(f"{self.name}: fundamentals")

    # ── capability introspection (used by the aggregator to pick a source) ───
    def supports(self, data_class: str) -> bool:
        fn = getattr(self, f"get_{data_class}", None)
        if fn is None:
            return False
        # A provider "supports" a class if it overrode the base method.
        return getattr(type(self), f"get_{data_class}") is not getattr(DataProvider, f"get_{data_class}")
