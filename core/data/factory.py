"""
build_data_hub — construct a DataHub from config (or env).

config.data may be a dict like {"providers": ["finnhub", "tiingo", ...]}.
Each named provider is instantiated; any whose `has_key` is False (no key in
the environment) is SKIPPED. The surviving providers, in the requested order,
form the DataHub.

Read-only. Never touches the risk core.
"""

from __future__ import annotations

import os
from typing import Any, List

from core.data.aggregator import DataHub
from core.data.base import DataProvider
from core.data.providers.alphavantage import AlphaVantageProvider
from core.data.providers.finnhub import FinnhubProvider
from core.data.providers.fmp import FMPProvider
from core.data.providers.tiingo import TiingoProvider

_REGISTRY = {
    "alphavantage": AlphaVantageProvider,
    "finnhub": FinnhubProvider,
    "fmp": FMPProvider,
    "tiingo": TiingoProvider,
}

_DEFAULT_ORDER = ["finnhub", "tiingo", "fmp", "alphavantage"]


def _read_provider_names(config: Any) -> List[str]:
    data = None
    if config is not None:
        data = getattr(config, "data", None)
        if data is None and isinstance(config, dict):
            data = config.get("data")
    if isinstance(data, dict):
        names = data.get("providers")
        if names:
            return list(names)
    env = os.environ.get("DATA_PROVIDERS", "").strip()
    if env:
        return [n.strip() for n in env.split(",") if n.strip()]
    return list(_DEFAULT_ORDER)


def build_data_hub(config: Any = None) -> DataHub:
    providers: List[DataProvider] = []
    for raw in _read_provider_names(config):
        cls = _REGISTRY.get(str(raw).strip().lower())
        if cls is None:
            continue
        provider = cls()
        if not provider.has_key:
            continue
        providers.append(provider)
    return DataHub(providers)
