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
    import json
    expected: dict = {}
    for p in store.get_open_positions():
        n = int(p["contracts"])
        u, exp = p["underlying"], p["expiration"]
        if p.get("structure") == "iron_condor":
            # Fail CLOSED: a condor with no legs_json must never be silently
            # reinterpreted as a 2-leg put vertical (that would drop both call
            # legs and hide real call-side drift).
            if not p.get("legs_json"):
                raise ValueError(
                    f"iron_condor position for {u} {exp} is missing legs_json — "
                    "cannot reconcile its 4 legs.")
            j = json.loads(p["legs_json"])
            _add(expected, _leg_key(u, exp, j["sp"], "P"), -n)
            _add(expected, _leg_key(u, exp, j["lp"], "P"), +n)
            _add(expected, _leg_key(u, exp, j["sc"], "C"), -n)
            _add(expected, _leg_key(u, exp, j["lc"], "C"), +n)
            continue
        family = p.get("family") or ("call" if "call" in (p.get("structure") or "") else "put")
        right = "C" if family == "call" else "P"
        # The SHORT leg is held −N, the LONG leg +N, regardless of credit/debit.
        _add(expected, _leg_key(u, exp, p["short_strike"], right), -n)
        _add(expected, _leg_key(u, exp, p["long_strike"], right), +n)
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

    # A corrupt/incomplete tracked position (e.g. a condor missing legs_json) must
    # fail closed and be labelled as such — not silently degraded or mislabelled.
    try:
        exp = expected_legs(store)
    except Exception as e:
        report["ok"] = False
        report["note"] = f"corrupt tracked position: {e}"
        report["drift"].append({"kind": "corrupt_position", "detail": str(e),
                                "severity": "high"})
        return report

    try:
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

    # ── ASSIGNMENT DETECTION ─────────────────────────────────────────────────
    # An exercised/assigned short leg turns into ±100 SHARES per contract — an
    # asset class the option-leg diff above is structurally blind to. This
    # account exists only to trade defined-risk option structures, so ANY equity
    # position at the broker is unexplained by construction and is the classic
    # post-assignment signature. Severity is critical because shares are
    # unbounded risk the gate never sized.
    try:
        for pos in adapter.get_positions():
            if pos.asset_class == "option":
                continue
            qty = float(pos.qty or 0.0)
            if abs(qty) < 1e-9:
                continue
            report["drift"].append({
                "kind": "untracked_equity_at_broker",
                "underlying": (pos.underlying or pos.symbol or "").upper(),
                "leg": f"{pos.symbol} {qty:+g} shares",
                "expected_qty": 0.0, "broker_qty": qty,
                "severity": "critical",
            })
    except Exception:
        pass  # the option-leg scan above already failed closed on fetch errors

    report["ok"] = len(report["drift"]) == 0
    return report
