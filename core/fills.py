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


def signed_intent_ps(magnitude: Any, is_credit: bool, closing: bool) -> Optional[float]:
    """A stored per-share MAGNITUDE -> our signed convention (credit POSITIVE).

    The store keeps `entry_credit_ps` and `exit_value_ps` as unsigned magnitudes
    (core/positions.py rounds abs(est_credit)); the sign lives in `is_credit` and
    in whether we are opening or closing. Getting this wrong is not a rounding
    error — it flips an adverse fill into a favourable one:

      OPEN  a credit spread  -> we RECEIVE  -> +magnitude
      OPEN  a debit spread   -> we PAY      -> -magnitude
      CLOSE a credit spread  -> we PAY      -> -magnitude   (closing reverses)
      CLOSE a debit spread   -> we RECEIVE  -> +magnitude

    A preflight review found three of the four fill call sites passing the raw
    magnitude, which made a debit spread's exit slippage read as a large
    FAVOURABLE number — and the cost model would have believed it."""
    m = _usable(magnitude)
    if m is None:
        return None
    receiving = (is_credit != bool(closing))   # XOR: closing flips the direction
    return round(abs(m) if receiving else -abs(m), 4)


def _adverse(signed_intent: Optional[float], fill_price: Any) -> Optional[float]:
    """Adverse slippage per share: POSITIVE = worse than intended, for every
    structure and both directions. One formula, because both sides are expressed
    in the same signed-credit convention."""
    realized = signed_credit_ps(fill_price)
    if realized is None or signed_intent is None:
        return None
    return round(signed_intent - realized, 4)


def entry_slippage_ps(intended_credit_ps: Any, fill_price: Any,
                      is_credit: bool = True) -> Optional[float]:
    """Adverse ENTRY slippage per share. POSITIVE = worse than intended.

    `intended_credit_ps` is the stored MAGNITUDE; `is_credit` supplies its sign
    (see signed_intent_ps). A credit spread receiving less, or a debit spread
    paying more, are both positive."""
    return _adverse(signed_intent_ps(intended_credit_ps, is_credit, closing=False),
                    fill_price)


def exit_slippage_ps(intended_exit_ps: Any, fill_price: Any,
                     is_credit: bool = True) -> Optional[float]:
    """Adverse EXIT slippage per share. POSITIVE = worse than intended.

    Closing REVERSES the direction: buying back a credit spread is money out,
    selling a debit spread is money in. Both cases route through the same signed
    comparison, so paying more (or receiving less) than the mark is positive."""
    return _adverse(signed_intent_ps(intended_exit_ps, is_credit, closing=True),
                    fill_price)


def plausible_slip_ps(slip_ps: Any, width: Any) -> bool:
    """Is a measured slippage physically believable for this structure?

    A vertical's whole round trip cannot slip by more than the spread's width —
    that is an arbitrage bound, not a preference. A value beyond it means a bad
    price, a convention mismatch, or a corrupt row, and such a number must NEVER
    become "evidence" in the cost model: the honest response is to fall back to
    the model and say so, not to clamp a garbage figure into a plausible-looking
    one. (The project's standing rule: absence and ambiguity must be
    unrepresentable as a confident number.)"""
    # A measured slippage of EXACTLY zero is a real measurement — the fill hit
    # the intended price — not absence. The store records absence as NULL, so
    # 0.0 here is unambiguous. Routing it through _usable() (which must treat
    # 0.0 as "no data" for BROKER-reported prices) made every perfect fill in
    # the record read as unmeasured, charged with modeled slippage while the
    # journal called it measured (preflight review, 2026-09-09).
    if isinstance(slip_ps, (int, float)) and not isinstance(slip_ps, bool) and float(slip_ps) == 0.0:
        return True
    v = _usable(slip_ps)
    if v is None:
        return False
    w = _usable(width)
    if w is None or w <= 0:
        return abs(v) <= 1.0        # no width known: one dollar/share is generous
    return abs(v) <= abs(w)


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


def net_debit_from_positions(legs: list, contracts: int = 1) -> Optional[float]:
    """Derive the realized NET per-share price of a spread from the broker's
    per-leg average costs, in our signed-credit convention (credit POSITIVE).

    Why this exists: when an order dies after a PARTIAL fill, IB reports
    avgFillPrice=0.0 on the order even though the account clearly holds the
    position — so the order feed yields no fill price at all. The POSITIONS feed
    still carries each leg's average cost, which is durable across sessions. That
    is the only surviving record of what we actually paid, and it is the input to
    every slippage number, so it is worth reconstructing.

    `legs` is a list of broker Position objects for ONE structure. IB quotes option
    avgCost per contract INCLUDING the 100x multiplier, so we divide it out.
    Longs are money out, shorts are money in; the net is expressed as a credit."""
    if not legs or contracts <= 0:
        return None
    net_paid = 0.0
    seen = False
    for p in legs:
        qty = float(getattr(p, "qty", 0.0) or 0.0)
        avg = float(getattr(p, "avg_price", 0.0) or 0.0)
        if qty == 0.0 or avg <= 0.0:
            continue
        seen = True
        per_share = avg / 100.0                 # strip the option multiplier
        # qty > 0 is a long (we paid); qty < 0 is a short (we received).
        net_paid += per_share if qty > 0 else -per_share
    if not seen:
        return None
    return round(-net_paid, 4)                  # paid -> negative credit


def combo_fill_from_executions(execs: list, client_order_id: str) -> Optional[tuple[float, float]]:
    """(filled_qty, avg_combo_price) for ONE order from its BAG executions, in
    IB's sign convention (negative = net credit received). None when no combo
    execution carries that orderRef, or a price is unusable.

    Only BAG rows count. The BAG row IS the net per-share price of the spread;
    re-deriving it from the OPT leg rows would repeat the partial-structure
    error the leg matcher once had (a condor priced off half its legs). Any
    order with combo fills has BAG rows; single-leg orders are not traded here.

    Quantity comes from the same rows, so a partial fill reports exactly how
    much filled — the number the order feed shows as 0.0 cross-process."""
    qty = 0.0
    notional = 0.0
    for e in execs or []:
        if str(getattr(e, "client_order_id", "") or "") != str(client_order_id or ""):
            continue
        if str(getattr(e, "sec_type", "") or "").upper() != "BAG":
            continue
        q = _usable(getattr(e, "qty", None))
        if q is None or q <= 0:
            continue
        # A combo price of 0.0 is IB's "unpopulated", never a real net-zero
        # spread. ONE such row would drag the average and be booked as
        # measured, so the whole answer is refused rather than diluted.
        px = _usable(getattr(e, "price", None))
        if px is None:
            return None
        qty += q
        notional += q * px
    if qty <= 0:
        return None
    return round(qty, 4), round(notional / qty, 4)
