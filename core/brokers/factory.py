"""
Broker factory — the ONLY place an adapter is constructed.

Paper is the default and the safe fallback: if a paper broker is requested but
its keys/Gateway are absent, we fall back to the deterministic Sim adapter and
say so loudly — so the system always runs and never silently does nothing.

LIVE is reachable through EXACTLY ONE path: `mode: live` AND all five conditions
of `core.golive.GoLiveGate` pass (see docs/05 §1.2, docs/14). If live is requested
but any gate is unmet, we keep the paper-only refusal — we stay PAPER, loudly, and
itemize what's missing. We NEVER masquerade a paper adapter as live: if the gates
pass but no live Gateway answers, we stay paper rather than pretend.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

from ..config import Config
from ..golive import ARMED_KV_KEY, GoLiveGate, current_ramp_tier
from .alpaca import AlpacaAdapter
from .base import BrokerAdapter
from .ibkr import IBKRAdapter
from .sim import SimAdapter


@dataclass
class BrokerBuild:
    adapter: BrokerAdapter
    note: str  # human-readable explanation of what was built and why


def build_execution_adapter(config: Config, asof: Optional[str] = None,
                            store: Optional[Any] = None) -> BrokerBuild:
    equity = float(config.account.get("starting_equity_usd", 30000))
    acct_type = str(config.account.get("type", "margin")).lower()

    def _sim(note: str) -> BrokerBuild:
        return BrokerBuild(SimAdapter(asof=asof, equity=equity, account_type=acct_type), note)

    def _build_paper() -> BrokerBuild:
        requested = str(config.brokers.get("execution", "alpaca_paper")).lower()

        if requested == "sim":
            return _sim(
                "Using SIM broker (deterministic, no network). Set brokers.execution: "
                "ibkr_paper (or alpaca_paper + keys) to trade a real paper account.")

        if requested in ("ibkr", "ibkr_paper", "ibkr_live"):
            host = str(config.brokers.get("ibkr_host", "127.0.0.1"))
            port = int(config.brokers.get("ibkr_port", 4002))   # 4002 = Gateway paper
            cid = int(config.brokers.get("ibkr_client_id", 7))
            region = str(config.account.get("region", "CA"))
            adapter = IBKRAdapter(host=host, port=port, client_id=cid,
                                  paper=True, region=region)
            if adapter.try_connect():
                return BrokerBuild(adapter, f"Using IBKR PAPER via Gateway at {host}:{port}.")
            return _sim(
                f"ibkr_paper requested but no IB Gateway answered at {host}:{port} "
                "(or ib_async/ib_insync isn't installed) — falling back to SIM so the "
                "cycle still runs. Start IB Gateway on a host and re-run. "
                "See docs/08-ibkr-gateway-runbook.md.")

        if requested in ("alpaca", "alpaca_paper"):
            key = os.environ.get("ALPACA_PAPER_KEY", "")
            secret = os.environ.get("ALPACA_PAPER_SECRET", "")
            if key and secret:
                return BrokerBuild(
                    AlpacaAdapter(key=key, secret=secret, paper=True),
                    "Using ALPACA PAPER (real paper account).")
            return _sim(
                "alpaca_paper requested but ALPACA_PAPER_KEY/SECRET are missing — "
                "falling back to SIM so the cycle still runs. Add keys to .env to use "
                "the real paper account.")

        return _sim(f"Unknown broker '{requested}'; defaulted to SIM.")

    # ── PAPER (the default) ───────────────────────────────────────────────────
    if config.is_paper:
        return _build_paper()

    # ── LIVE requested — the ONLY path to a live adapter ──────────────────────
    # Gate 4 (the per-session typed confirmation) is collected interactively by
    # `cli.py go-live`, which persists the dated phrase to the store when it arms
    # live for the day. Here (run-cycle / status / reconcile) we read that armed
    # marker; it goes stale at the date rollover so a prior day's "yes" can't ride.
    armed = store.get_kv(ARMED_KV_KEY) if store is not None else None
    decision = GoLiveGate(config).evaluate(confirmation=armed)

    if not decision.allowed:
        # Keep the paper-only refusal: stay PAPER, loudly, and say what's missing.
        b = _build_paper()
        b.note = ("LIVE requested but the go-live gate is NOT satisfied — staying PAPER. "
                  "Unmet: " + "; ".join(decision.reasons) + ".  Arm live (when truly "
                  "ready) with: python cli.py go-live.\n  " + b.note)
        return b

    # All five gates pass — build the LIVE IBKR adapter on the live port. The
    # adapter's own __init__ refuses paper=False against a paper port, so this
    # cannot be the paper endpoint.
    ramp = current_ramp_tier(config) or {}
    host = str(config.brokers.get("ibkr_host", "127.0.0.1"))
    live_port = int(config.brokers.get("ibkr_live_port", 4001))
    cid = int(config.brokers.get("ibkr_client_id", 7))
    region = str(config.account.get("region", "CA"))
    live = IBKRAdapter(host=host, port=live_port, client_id=cid,
                       paper=False, region=region)
    if live.try_connect():
        return BrokerBuild(
            live,
            f"⚠️  LIVE via IB Gateway at {host}:{live_port}. Ramp tier {ramp.get('tier')}: "
            f"max ${float(ramp.get('max_notional_usd', 0)):.0f}/position, "
            f"{ramp.get('max_positions')} concurrent. All five go-live gates PASS; the "
            "risk gate enforces the ramp cap as a hard limit.")
    # Gates pass but no LIVE Gateway answered — do NOT pretend to be live. Stay paper.
    b = _build_paper()
    b.note = (f"LIVE gates pass but no LIVE IB Gateway answered at {host}:{live_port} — "
              "staying PAPER until the live Gateway is up (we never masquerade a paper "
              "adapter as live).\n  " + b.note)
    return b
