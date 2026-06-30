"""
Broker factory — the ONLY place an adapter is constructed.

Enforces the cardinal safety rule: Phase 0 is PAPER-ONLY. There is no code path
here that returns a live adapter. If config requests Alpaca paper but no keys are
present, we fall back to the deterministic Sim adapter and say so loudly — so the
system always runs, and never silently does nothing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from ..config import Config
from .alpaca import AlpacaAdapter
from .base import BrokerAdapter
from .ibkr import IBKRAdapter
from .sim import SimAdapter


@dataclass
class BrokerBuild:
    adapter: BrokerAdapter
    note: str  # human-readable explanation of what was built and why


def build_execution_adapter(config: Config, asof: Optional[str] = None) -> BrokerBuild:
    # Hard stop: live is not buildable in Phase 0, regardless of config/env.
    if not config.is_paper:
        raise RuntimeError(
            "LIVE trading is not available in Phase 0. The factory only builds "
            "paper/sim adapters. See docs/05-risk-and-safety.md and docs/07-roadmap.md."
        )

    requested = str(config.brokers.get("execution", "alpaca_paper")).lower()
    equity = float(config.account.get("starting_equity_usd", 30000))
    acct_type = str(config.account.get("type", "margin")).lower()

    if requested == "sim":
        return BrokerBuild(
            SimAdapter(asof=asof, equity=equity, account_type=acct_type),
            "Using SIM broker (deterministic, no network). Set brokers.execution: "
            "alpaca_paper and add ALPACA_PAPER_* keys to trade Alpaca paper.",
        )

    if requested in ("ibkr", "ibkr_paper"):
        host = str(config.brokers.get("ibkr_host", "127.0.0.1"))
        port = int(config.brokers.get("ibkr_port", 4002))   # 4002 = Gateway paper
        cid = int(config.brokers.get("ibkr_client_id", 7))
        region = str(config.account.get("region", "CA"))
        adapter = IBKRAdapter(host=host, port=port, client_id=cid,
                              paper=True, region=region)
        if adapter.try_connect():
            return BrokerBuild(adapter, f"Using IBKR PAPER via Gateway at {host}:{port}.")
        return BrokerBuild(
            SimAdapter(asof=asof, equity=equity, account_type=acct_type),
            f"ibkr_paper requested but no IB Gateway answered at {host}:{port} "
            "(or ib_async/ib_insync isn't installed) — falling back to SIM so the "
            "cycle still runs. Start IB Gateway on a host and re-run. "
            "See docs/08-ibkr-gateway-runbook.md.",
        )

    if requested in ("alpaca", "alpaca_paper"):
        key = os.environ.get("ALPACA_PAPER_KEY", "")
        secret = os.environ.get("ALPACA_PAPER_SECRET", "")
        if key and secret:
            return BrokerBuild(
                AlpacaAdapter(key=key, secret=secret, paper=True),
                "Using ALPACA PAPER (real paper account).",
            )
        return BrokerBuild(
            SimAdapter(asof=asof, equity=equity, account_type=acct_type),
            "alpaca_paper requested but ALPACA_PAPER_KEY/SECRET are missing — "
            "falling back to SIM so the cycle still runs. Add keys to .env to use "
            "the real paper account.",
        )

    # Unknown broker name -> safest default.
    return BrokerBuild(
        SimAdapter(asof=asof, equity=equity, account_type=acct_type),
        f"Unknown broker '{requested}'; defaulted to SIM.",
    )
