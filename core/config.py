"""
Configuration loader.

Reads config/config.yaml (deterministic settings the core obeys) and the
environment (secrets, never in YAML). The LLM may READ these numbers but the
loader is the only writer. Mode resolves to PAPER unless every live gate passes
(Phase 0 forbids live entirely — see core.brokers.factory).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "config.yaml"
WATCHLIST_PATH = REPO_ROOT / "config" / "watchlist.yaml"


def _load_env_file(path: Path) -> None:
    """Minimal .env loader (no python-dotenv dependency). Does not override
    variables already set in the real environment."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


@dataclass
class Config:
    raw: dict[str, Any]
    mode: str
    account: dict[str, Any]
    brokers: dict[str, Any]
    risk: dict[str, Any]
    strategies: dict[str, Any]
    watchlist: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)   # external data-provider config

    @property
    def is_paper(self) -> bool:
        return self.mode != "live"

    def risk_value(self, key: str, default: Any = None) -> Any:
        return self.risk.get(key, default)


def load_config(config_path: Path | None = None) -> Config:
    """Load config.yaml + .env. Falls back to *.example.yaml so a fresh clone
    runs before the operator has copied the templates."""
    _load_env_file(REPO_ROOT / ".env")

    path = config_path or CONFIG_PATH
    if not path.exists():
        path = path.with_name(path.name.replace(".yaml", ".example.yaml"))
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    wpath = WATCHLIST_PATH
    if not wpath.exists():
        wpath = wpath.with_name(wpath.name.replace(".yaml", ".example.yaml"))
    watch_raw = yaml.safe_load(wpath.read_text(encoding="utf-8")) if wpath.exists() else {}
    watchlist = _extract_watchlist(watch_raw)

    # Environment override for mode is intentionally NOT trusted to enable live;
    # the factory makes the final paper/live decision. We only read what YAML says.
    mode = str(data.get("mode", "paper")).lower()

    return Config(
        raw=data,
        mode=mode,
        account=data.get("account", {}) or {},
        brokers=data.get("brokers", {}) or {},
        risk=data.get("risk", {}) or {},
        strategies=data.get("strategies", {}) or {},
        watchlist=watchlist,
        data=data.get("data", {}) or {},
    )


def _extract_watchlist(watch_raw: Any) -> list[str]:
    """Accept several shapes: a bare list, {tickers: [...]}, or
    {equities: [...], etfs: [...]}."""
    if isinstance(watch_raw, list):
        return [str(t).upper() for t in watch_raw]
    if isinstance(watch_raw, dict):
        out: list[str] = []
        # Phase 0 is ETF-only by the operator's choice (no single-stock earnings
        # binary). The `equities:` block is intentionally NOT loaded here — it is
        # picked up later when the earnings engine ships (see docs/07-roadmap.md).
        for key in ("tickers", "etfs", "universe", "symbols"):
            v = watch_raw.get(key)
            if isinstance(v, list):
                out.extend(str(t).upper() for t in v)
        deny = {str(t).upper() for t in (watch_raw.get("deny") or []) if t}
        out = [t for t in out if t not in deny]
        # de-dup, preserve order
        seen: set[str] = set()
        result = []
        for t in out:
            if t not in seen:
                seen.add(t)
                result.append(t)
        return result
    return []
