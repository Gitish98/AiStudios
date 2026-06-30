#!/usr/bin/env python3
"""
Backtest / replay harness — evaluate the strategies on historical data using the
EXACT same hardened machinery as live: it replays `run_cycle` over historical
trading days with a history-driven broker, so the risk gate, fill lifecycle,
position management, and P&L math are identical to production. No separate,
divergent backtest engine to drift out of sync.

HONEST LIMITATION (read this): option prices here are MODELED (Black-Scholes from
the day's close + an IV path), not real historical option quotes. Equity bar
history is real (or whatever you feed it); the option chain is a model. So this
measures whether the STRATEGY LOGIC has an edge under a clean model — it does NOT
capture real bid/ask, fills, early assignment, or IV surface skew. A positive
backtest is necessary, not sufficient. Validate on paper before any live capital.

Usage (programmatic):
    from backtest import run_backtest
    report = run_backtest(strategies, bars_by_symbol, config, start, end)
"""

from __future__ import annotations

import math
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

from core.brokers.sim import SimAdapter
from core.costs import CostModel
from core.execution import run_cycle
from core.options_math import realized_vol
from core.performance import compute_metrics as _metrics
from core.store import Store


class BacktestBroker(SimAdapter):
    """A history-driven broker: reuses sim's BS option-chain / mark machinery but
    sources spot, IV, history, and earnings from REAL (provided) bar data at the
    current `asof`. Fills instantly (name='sim' so reconcile no-ops)."""

    def __init__(self, bars_by_symbol: dict[str, list[dict]],
                 iv_by_symbol: Optional[dict[str, dict[str, float]]] = None,
                 earnings_by_symbol: Optional[dict[str, str]] = None,
                 equity: float = 30000.0, account_type: str = "margin"):
        super().__init__(asof=None, equity=equity, account_type=account_type)
        self._bars = {s.upper(): b for s, b in bars_by_symbol.items()}
        self._iv = {s.upper(): v for s, v in (iv_by_symbol or {}).items()}
        self._earn = {s.upper(): v for s, v in (earnings_by_symbol or {}).items()}
        self.asof = None  # set per replay day

    def _closes_through(self, symbol: str) -> list[float]:
        bars = self._bars.get(symbol.upper(), [])
        if not self.asof:
            return [b["c"] for b in bars]
        return [b["c"] for b in bars if b["date"] <= self.asof]

    def _spot(self, symbol: str) -> float:
        cs = self._closes_through(symbol)
        return round(cs[-1], 2) if cs else 0.0

    def _base_iv(self, symbol: str) -> float:
        # Provided IV path if available, else a realized-vol proxy (honest model).
        path = self._iv.get(symbol.upper())
        if path and self.asof and self.asof in path:
            return float(path[self.asof])
        rv = realized_vol(self._closes_through(symbol)[-30:])
        return round(rv if rv and rv > 0 else 0.20, 4)

    def get_history(self, symbol: str, days: int = 120, asof=None) -> list[dict]:
        bars = self._bars.get(symbol.upper(), [])
        cut = asof or self.asof
        out = [b for b in bars if (not cut or b["date"] <= cut)]
        return out[-days:]

    def iv_history(self, symbol: str, days: int = 252) -> list[float]:
        cs = self._closes_through(symbol)
        # A modeled IV history = rolling realized vol, so IV rank is computable.
        out = []
        for i in range(20, len(cs) + 1):
            rv = realized_vol(cs[max(0, i - 30):i])
            if rv and rv > 0:
                out.append(round(rv, 4))
        return out[-days:] if out else []

    def get_earnings_date(self, symbol: str, asof=None) -> Optional[str]:
        return self._earn.get(symbol.upper())


def _trading_days(bars_by_symbol: dict[str, list[dict]], start: str, end: str) -> list[str]:
    days: set[str] = set()
    for bars in bars_by_symbol.values():
        for b in bars:
            if start <= b["date"] <= end:
                days.add(b["date"])
    return sorted(days)


def run_backtest(strategies: list, bars_by_symbol: dict[str, list[dict]], config,
                 start: str, end: str,
                 iv_by_symbol: Optional[dict] = None,
                 earnings_by_symbol: Optional[dict] = None) -> dict[str, Any]:
    """Replay the cycle day-by-day over [start, end]. Returns a metrics report.

    WARM-UP: indicators are computed from every bar with date <= the current day,
    so to have warm indicators (≈50+ bars for the breakout SMAs, ~20+ for IV
    rank) on the FIRST trading day, provide `bars_by_symbol` that includes history
    BEFORE `start`. There is no separate warmup phase — pre-`start` bars ARE the
    warmup, used automatically with strict no-lookahead slicing."""
    broker = BacktestBroker(bars_by_symbol, iv_by_symbol, earnings_by_symbol,
                            equity=float(config.account.get("starting_equity_usd", 30000)),
                            account_type=str(config.account.get("type", "margin")))
    days = _trading_days(bars_by_symbol, start, end)

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "bt.db")
        for day in days:
            broker.asof = day
            run_cycle(broker, strategies, store, config, asof=day)
        closed = store.get_closed_positions()
        report = _metrics(closed, days, CostModel.from_config(config))
        store.close()
    return report


if __name__ == "__main__":
    # Smoke run on sim-generated bars (replace with real provider bars).
    from core.config import load_config
    from cli import _strategies
    cfg = load_config()
    seed = SimAdapter(asof="2026-06-30")
    bars = {s: seed.get_history(s, 250) for s in (cfg.watchlist or ["SPY", "QQQ", "IWM"])}
    rep = run_backtest(_strategies(cfg), bars, cfg, start="2026-01-01", end="2026-06-30")
    import json
    print(json.dumps(rep, indent=2))
