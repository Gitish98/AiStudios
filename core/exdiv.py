"""
Ex-dividend awareness — the assignment trap the calendar sets four times a year.

WHY THIS EXISTS: American-style short CALLS get exercised early when the dividend
is worth more than the option's remaining time value, which is reliably true for
short ITM calls in the last days before an ex-dividend date. For our watchlist
that is not an edge case: SPY/QQQ/IWM and the sector SPDRs go ex-div QUARTERLY
(historically the third Friday of Mar/Jun/Sep/Dec — expiration week!), and DIA
goes ex-div MONTHLY. A short ITM call held across ex-div is a near-certain
assignment: the spread becomes -100 shares plus a lone long call overnight, and
the account also owes the dividend. That silently breaches invariant #2
(defined-risk only), which is why this is handled and not merely documented.

HONESTY ABOUT THE DATA: these are ESTIMATED dates from the historical pattern
(third Friday), not a real corporate-actions feed. The estimate can be off by a
few days either way, so the risk window is padded and the rule is deliberately
conservative: close short ITM calls a few days early rather than model dividend
capture precisely. When a real calendar provider is wired in later, only
`estimated_ex_div_dates` needs replacing.

Puts are unaffected (no dividend motive to exercise early); the current
premium_harvest book (put spreads) never triggers any of this.
"""

from __future__ import annotations

import datetime as _dt
import json
from typing import Any, Optional

# Quarterly payers: estimated ex-div = third Friday of Mar/Jun/Sep/Dec.
QUARTERLY = {"SPY", "QQQ", "IWM", "XLK", "XLF", "XLE", "VOO", "IVV", "VTI"}
# Monthly payer: estimated ex-div = third Friday of EVERY month.
MONTHLY = {"DIA"}

# How close (calendar days) to an estimated ex-div a short ITM call becomes an
# exit signal. Padded because the dates are estimates.
DEFAULT_BUFFER_DAYS = 4


def third_friday(year: int, month: int) -> _dt.date:
    d = _dt.date(year, month, 15)                    # 3rd Friday is the 15th-21st
    return d + _dt.timedelta(days=(4 - d.weekday()) % 7)


def next_estimated_ex_div(symbol: str, on_or_after: _dt.date) -> Optional[_dt.date]:
    """Next ESTIMATED ex-dividend date for `symbol` on/after the given date, or
    None for symbols with no known dividend pattern (fail quiet, not closed:
    an unknown symbol simply gets no ex-div protection, and says so nowhere
    else than here — the watchlist symbols are all covered)."""
    sym = (symbol or "").upper()
    if sym in MONTHLY:
        months = range(12)
    elif sym in QUARTERLY:
        months = None                                # handled below
    else:
        return None
    y, m = on_or_after.year, on_or_after.month
    for _ in range(14):                              # scan up to ~14 months out
        if sym in MONTHLY or m in (3, 6, 9, 12):
            cand = third_friday(y, m)
            if cand >= on_or_after:
                return cand
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return None


def _short_call_strike(pos: dict) -> Optional[float]:
    """The SHORT call strike of a tracked position, or None if it has none.

    Verticals: the `short_strike` column is the short leg by definition, so any
    call-family structure has a short call. Iron condors carry theirs in
    legs_json['sc']. Put structures have no short call — no dividend risk."""
    structure = pos.get("structure") or ""
    if structure == "iron_condor":
        raw = pos.get("legs_json")
        if not raw:
            return None
        try:
            return float(json.loads(raw)["sc"])
        except Exception:
            return None
    family = pos.get("family") or ("call" if "call" in structure else "put")
    if family != "call":
        return None
    try:
        return float(pos["short_strike"])
    except Exception:
        return None


def exdiv_risk(pos: dict, spot: Optional[float], asof: str,
               buffer_days: int = DEFAULT_BUFFER_DAYS) -> Optional[str]:
    """ISO date of the estimated ex-div that makes this position an assignment
    risk, or None.

    Risk requires ALL of: a short call in the structure; the short call ITM
    (spot above the strike — OTM calls have time value and no exercise motive);
    an estimated ex-div within `buffer_days`; and the position still held
    through that date (ex-div before expiration)."""
    strike = _short_call_strike(pos)
    if strike is None or not spot or spot <= strike:
        return None
    try:
        today = _dt.date.fromisoformat(str(asof)[:10])
        expiry = _dt.date.fromisoformat(str(pos.get("expiration"))[:10])
    except (TypeError, ValueError):
        return None
    exd = next_estimated_ex_div(pos.get("underlying", ""), today)
    if exd is None or exd > expiry:
        return None
    if (exd - today).days <= max(0, int(buffer_days)):
        return exd.isoformat()
    return None
