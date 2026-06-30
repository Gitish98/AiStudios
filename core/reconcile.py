"""
Reconcile — compare our TRACKED book (the SQLite store) against the BROKER's
actual positions. This is the safety check that must be clean before live: if
what we think we hold disagrees with what IBKR holds, every downstream risk
calculation is wrong.

Drift kinds:
  - missing_at_broker  : we track a leg the broker isn't holding (closed/expired/
                         assigned at the broker without us recording it).
  - untracked_at_broker: the broker holds an option leg we have no record of.
  - qty_mismatch       : same leg, different contract count.

A put credit spread we track expands to two expected broker legs:
  short put @ short_strike  -> signed qty  -contracts
  long  put @ long_strike   -> signed qty  +contracts
"""

from __future__ import annotations

from typing import Any

from .brokers.base import BrokerAdapter
from .store import Store


def _add(d: dict, key, qty: float) -> None:
    d[key] = d.get(key, 0.0) + qty


def _leg_key(underlying: str, expiration: str, strike: float, right: str):
    return (str(underlying).upper(), str(expiration), round(float(strike), 4),
            str(right).upper()[:1])


def expected_legs(store: Store) -> dict:
    """Signed expected option-leg quantities from our tracked open positions."""
    expected: dict = {}
    for p in store.get_open_positions():
        n = int(p["contracts"])
        u, exp = p["underlying"], p["expiration"]
        _add(expected, _leg_key(u, exp, p["short_strike"], "P"), -n)
        _add(expected, _leg_key(u, exp, p["long_strike"], "P"), +n)
    return expected


def broker_legs(adapter: BrokerAdapter) -> dict:
    """Signed option-leg quantities the broker actually holds."""
    legs: dict = {}
    for pos in adapter.get_positions():
        if pos.asset_class != "option" or pos.option_strike is None:
            continue
        key = _leg_key(pos.underlying or pos.symbol, pos.option_expiration,
                       pos.option_strike, pos.option_right or "")
        _add(legs, key, float(pos.qty))
    return legs


def reconcile(adapter: BrokerAdapter, store: Store) -> dict[str, Any]:
    report: dict[str, Any] = {
        "broker": adapter.name, "ok": True, "drift": [],
        "tracked_open": len(store.get_open_positions()), "note": "",
    }

    # The sim broker is ephemeral — it does not model a persistent holdings book,
    # so a position-level reconcile against it is not meaningful. Say so plainly
    # rather than emitting false drift.
    if adapter.name == "sim":
        report["note"] = ("sim broker holds no persistent book — position "
                          "reconcile is a no-op. Reconcile is meaningful against "
                          "IBKR (a real paper/live account).")
        return report

    try:
        exp = expected_legs(store)
        brk = broker_legs(adapter)
    except Exception as e:  # broker fetch failure -> fail closed (flag, don't pretend clean)
        report["ok"] = False
        report["note"] = f"could not fetch broker positions: {e}"
        report["drift"].append({"kind": "broker_unreachable", "detail": str(e),
                                "severity": "high"})
        return report

    for key in sorted(set(exp) | set(brk), key=lambda k: (k[0], k[1], k[2], k[3])):
        e, b = exp.get(key, 0.0), brk.get(key, 0.0)
        if abs(e - b) < 1e-9:
            continue
        if b == 0:
            kind = "missing_at_broker"
        elif e == 0:
            kind = "untracked_at_broker"
        else:
            kind = "qty_mismatch"
        u, expd, strike, right = key
        report["drift"].append({
            "kind": kind,
            "leg": f"{u} {strike:g}{right} {expd}",
            "expected_qty": e, "broker_qty": b, "severity": "high",
        })

    report["ok"] = len(report["drift"]) == 0
    return report
