"""
Fill economics — turn a broker's combo fill price into our per-share convention
and measure REALIZED slippage against what we intended to pay/receive.

Why this exists (docs/14 audit, critical finding): without this, every P&L number
in the system is computed from the INTENDED mid price, which makes a "paper track
record" a restatement of our own price assumptions rather than evidence. On a $26
credit spread, one tick of adverse fill on each leg crossing IS the entire
expectancy. Worse, IB's `trades()` feed is session-scoped — a fill price not
recorded the same day is gone forever, so this must be captured at fill time.

SIGN CONVENTIONS (the whole point of this module):
  - IB submits a spread as a BAG with action BUY. A net DEBIT is a POSITIVE price;
    a net CREDIT is NEGATIVE (see core.brokers.ibkr.build_order_plan).
  - Our codebase carries a credit as a POSITIVE number.
  So our signed per-share credit is simply the NEGATED broker combo price.

We never guess: an unusable price returns None so the slippage series stays clean.
"""

from __future__ import annotations

import math
from typing import Any, Optional


def _usable(x: Any) -> Optional[float]:
    """float(x), or None when the broker gave us nothing real.

    0.0 is deliberately treated as NO DATA, not a genuine zero fill: IB leaves
    avgFillPrice at 0.0 when it is unpopulated (the common case when an order is
    reconstructed in a new process). Recording a fabricated 0 would silently
    corrupt the slippage series — we would rather lose the rare genuinely-zero
    close than poison the measurement we are building all of this to trust."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v) or v == 0.0:
        return None
    return v


def signed_credit_ps(fill_price: Any) -> Optional[float]:
    """Broker combo fill price -> our signed per-share credit (credit POSITIVE).

    IB: negative combo price == net credit received; positive == net debit paid.
    So negating gives our convention directly, for entries and closes alike."""
    v = _usable(fill_price)
    return None if v is None else round(-v, 4)


def entry_slippage_ps(intended_credit_ps: Any, fill_price: Any) -> Optional[float]:
    """Adverse entry slippage per share. POSITIVE = worse than intended.

    For a credit spread we intend to RECEIVE `intended_credit_ps`; receiving less
    is adverse. Works for debit structures too, because both sides are expressed
    in the same signed-credit convention (a debit is a negative credit)."""
    realized = signed_credit_ps(fill_price)
    intended = _usable(intended_credit_ps)
    if realized is None or intended is None:
        return None
    return round(intended - realized, 4)


def exit_slippage_ps(intended_exit_ps: Any, fill_price: Any) -> Optional[float]:
    """Adverse exit slippage per share. POSITIVE = worse than intended.

    Closing a credit spread costs money: `intended_exit_ps` is what we expected to
    pay, and the realized cost is the broker price with our sign convention undone.
    Paying more than intended is adverse."""
    realized_credit = signed_credit_ps(fill_price)
    intended = _usable(intended_exit_ps)
    if realized_credit is None or intended is None:
        return None
    realized_cost = -realized_credit          # back to "what we paid"
    return round(realized_cost - intended, 4)


def summarize_slippage(rows: list[dict]) -> dict:
    """Aggregate realized slippage across closed positions. Reports the MEAN and
    the worst case, both per share and in dollars, plus how much of the book we
    could actually measure — an unmeasured majority makes the mean meaningless."""
    entry = [r["entry_slip_ps"] for r in rows if r.get("entry_slip_ps") is not None]
    exits = [r["exit_slip_ps"] for r in rows if r.get("exit_slip_ps") is not None]
    both = entry + exits
    n = len(rows)
    return {
        "legs_measured": len(both),
        "positions": n,
        "coverage": round(len(entry) / n, 3) if n else 0.0,
        "mean_entry_slip_ps": round(sum(entry) / len(entry), 4) if entry else None,
        "mean_exit_slip_ps": round(sum(exits) / len(exits), 4) if exits else None,
        "mean_roundtrip_slip_ps": round((sum(entry) / len(entry)) + (sum(exits) / len(exits)), 4)
                                  if entry and exits else None,
        "worst_slip_ps": round(max(both), 4) if both else None,
        "note": "POSITIVE = adverse (worse than intended). Per share; x100 per contract. "
                "Coverage < 1.0 means some fills were never reported by the broker.",
    }
