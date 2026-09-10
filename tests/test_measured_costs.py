"""
Slippage as EVIDENCE, not assumption.

The system captures realized fill prices (core/fills.py) precisely so its P&L
stops being a restatement of its own price assumptions. But `realized_pnl` is
computed from INTENDED prices and the cost model subtracted a MODELED slippage
of $0.01/contract/leg — while the first two real fills measured $1.60 and $1.39
per contract on the ENTRY alone, ~70x more. That number sits directly under the
graduation gate's "positive net expectancy" test, so understating it makes a
losing book read as eligible to go live. These lock the fix.
"""

from __future__ import annotations

from core.costs import CostModel
from core.performance import compute_metrics, graduation_status


def test_measured_slippage_replaces_the_model():
    c = CostModel()
    legs, contracts = 2, 5
    # Modeled: 0.75/leg/contract -> 7.50 on top of 6.50 commission.
    modeled = c.order_cost(legs, contracts)
    assert modeled == round(6.50 + 0.75 * 2 * 5, 2) == 14.0
    # MEASURED 0.0139/share on a 5-contract spread = $6.95 of real slippage.
    measured = c.order_cost(legs, contracts, measured_slip_ps=0.0139)
    assert measured == round(6.50 + 6.95, 2) == 13.45
    # order_cost NO LONGER clamps: a preflight review showed the clamp turned an
    # implausible (or sign-flipped) slip into a confident $0.00 cost. Validation
    # now happens upstream in position_cost via fills.plausible_slip_ps, so this
    # primitive faithfully reports whatever it is handed — including a genuine
    # price improvement, which really does reduce cost.
    assert c.order_cost(legs, contracts, measured_slip_ps=-0.002) == round(6.50 - 1.0, 2)


def test_default_slippage_is_no_longer_two_orders_of_magnitude_light():
    """REGRESSION: the old $0.01/contract/leg default was ~70x below both real
    measurements. A model that cheap makes the go-live gate structurally
    optimistic on any book with no measured fills yet."""
    c = CostModel()
    assert c.slippage_per_contract >= 0.5, "modeled half-spread must be plausible"
    # One 5-contract spread's modeled round trip is now dollars, not cents.
    rt = c.position_cost("put_credit_spread", 5, "profit_target")
    assert rt > 20.0


def test_position_cost_uses_each_side_independently():
    c = CostModel()
    # Entry measured, exit not: entry uses evidence, exit falls back to model.
    mixed = c.position_cost("put_credit_spread", 5, "profit_target",
                            entry_slip_ps=0.0139, exit_slip_ps=None)
    assert mixed == round(c.order_cost(2, 5, 0.0139) + c.order_cost(2, 5), 2)
    # An EXPIRY trades nothing to close -> one side only.
    exp = c.position_cost("put_credit_spread", 5, "expired", entry_slip_ps=0.0139)
    assert exp == c.order_cost(2, 5, 0.0139)


def test_coverage_is_reported_in_DOLLARS_not_side_counts():
    """A measured entry beside a modeled exit is not '50% evidence' when the two
    sides cost different amounts — and the number exists to tell a reader how
    much of the expectancy rests on evidence. Review finding: count sides, and a
    cheap measured side beside an expensive modeled one overstates coverage."""
    c = CostModel()
    base = {"structure": "put_credit_spread", "contracts": 5, "width": 1.0}
    both = dict(base, entry_slip_ps=0.01, exit_slip_ps=0.02)
    half = dict(base, entry_slip_ps=0.01, exit_slip_ps=None)
    none = dict(base, entry_slip_ps=None, exit_slip_ps=None)

    m, t = c.measured_dollars(both, "profit_target")
    assert m == t and t > 0, "fully measured"
    m, t = c.measured_dollars(none, "profit_target")
    assert m == 0.0 and t > 0, "fully modeled"
    m, t = c.measured_dollars(half, "profit_target")
    # Measured entry = 6.50 + 5.00 = 11.50; modeled exit = 6.50 + 7.50 = 14.00.
    # Dollar coverage is 45%, NOT the 50% a side count would claim.
    assert (m, t) == (11.5, 25.5)
    assert round(m / t, 3) == 0.451
    # An EXPIRY trades nothing to close -> the entry is the whole cost.
    m, t = c.measured_dollars(half, "expired")
    assert m == t == 11.5


def test_implausible_slippage_is_REJECTED_not_believed():
    """A slip larger than the spread's width is arbitrage-impossible — it means a
    bad price or a sign-convention mismatch, not a real cost. Such a number must
    fall back to the MODEL and stop counting as evidence, never be clamped into a
    plausible-looking figure. (This is the same rule that saved the mark path.)"""
    from core.fills import plausible_slip_ps
    c = CostModel()
    assert plausible_slip_ps(0.0139, 1.0) is True
    assert plausible_slip_ps(-0.05, 1.0) is True        # real price improvement
    assert plausible_slip_ps(0.52, 1.0) is True         # bad but not impossible
    assert plausible_slip_ps(1.60, 1.0) is False        # exceeds the width
    assert plausible_slip_ps(None, 1.0) is False
    # 0.0 IS a measurement here. "0.0 means no data" is the rule for a BROKER
    # PRICE (fills._usable enforces it where prices enter); a STORED slip can
    # only be 0.0 if a real fill matched the intent exactly — record_*_fill
    # writes NULL, never 0.0, when the price was unusable. Rejecting it charged
    # every perfect fill with modeled slippage (preflight review, 2026-09-09).
    assert plausible_slip_ps(0.0, 1.0) is True

    bad = {"structure": "put_credit_spread", "contracts": 5, "width": 1.0,
           "entry_slip_ps": 2.5, "exit_slip_ps": None}
    # The impossible entry slip must NOT be spent as evidence...
    assert c.position_cost("put_credit_spread", 5, "profit_target", 2.5, None, 1.0)         == c.position_cost("put_credit_spread", 5, "profit_target", None, None, 1.0)
    # ...and must not be counted as coverage either.
    m, t = c.measured_dollars(bad, "profit_target")
    assert m == 0.0


def test_metrics_carry_measured_costs_and_coverage():
    """The real trade: QQQ 695/694 x5, intended credit 0.21, MEASURED entry fill
    0.1961 (slippage 0.0139/sh). Closing at 0.10 grosses $55 from intended
    prices — but the honest net must subtract the real $6.95 of entry slippage,
    not a $0.10 model."""
    closed = [{
        "realized_pnl": 55.0, "closed_asof": "2026-08-17",
        "structure": "put_credit_spread", "contracts": 5,
        "exit_reason": "profit_target",
        "entry_slip_ps": 0.0139, "exit_slip_ps": None,
    }]
    m = compute_metrics(closed, ["2026-08-17"], CostModel())
    # Entry: 6.50 commission + 6.95 measured. Exit: 6.50 + 7.50 modeled.
    assert m["total_costs"] == 27.45
    assert m["net_pnl"] == round(55.0 - 27.45, 2)
    # Coverage is dollar-weighted: measured entry 13.45 of 27.45 total = 49%.
    assert m["cost_measured_pct"] == round(13.45 / 27.45, 3)
    assert "MEASURED" in m["note"]


def test_understated_slippage_no_longer_flatters_the_graduation_gate():
    """The stakes: 40 small winners that a cheap slippage model would call
    profitable must read as NEGATIVE expectancy once real fills are priced in."""
    closed = [{"realized_pnl": 12.0, "closed_asof": f"2026-{m:02d}-{d:02d}",
               "structure": "put_credit_spread", "contracts": 5,
               "exit_reason": "profit_target",
               "entry_slip_ps": 0.0139, "exit_slip_ps": 0.0139}
              for m in (3, 4, 5, 6) for d in range(1, 11)]
    days = sorted({p["closed_asof"] for p in closed})
    m = compute_metrics(closed, days, CostModel())
    assert m["trades"] == 40 and len(days) >= 40
    assert m["gross_pnl"] > 0, "every trade was a gross winner"
    assert m["expectancy_net"] < 0, "but real costs make the book a loser"
    assert m["cost_measured_pct"] == 1.0, "fully evidence-based here"
    assert graduation_status(m)["graduated"] is False


# ─────────── the sign convention: one owner, four call sites ───────────

def test_signed_intent_covers_all_four_direction_cases():
    """The store keeps entry_credit_ps / exit_value_ps as unsigned MAGNITUDES;
    the sign lives in is_credit and in whether we are opening or closing. A
    preflight review found three of four fill call sites passing the raw
    magnitude — which read a debit spread's exit as hugely FAVOURABLE."""
    from core.fills import signed_intent_ps
    # OPEN a credit spread: money in.       CLOSE it: money out.
    assert signed_intent_ps(0.21, is_credit=True,  closing=False) == 0.21
    assert signed_intent_ps(0.10, is_credit=True,  closing=True) == -0.10
    # OPEN a debit spread: money out.       CLOSE it: money in.
    assert signed_intent_ps(0.31, is_credit=False, closing=False) == -0.31
    assert signed_intent_ps(0.42, is_credit=False, closing=True) == 0.42
    assert signed_intent_ps(None, True, False) is None
    assert signed_intent_ps(0.0, True, False) is None          # 0.0 = NO DATA


def test_adverse_is_positive_for_every_structure_and_direction():
    """POSITIVE = worse than intended, in all four cases. Broker prices are in
    IB's combo convention (negative = credit received)."""
    from core.fills import entry_slippage_ps, exit_slippage_ps

    # CREDIT entry: expect +0.21, receive only 0.1961 -> adverse 0.0139.
    assert entry_slippage_ps(0.21, -0.1961, is_credit=True) == 0.0139
    # ...and receiving MORE is favourable (negative).
    assert entry_slippage_ps(0.21, -0.2200, is_credit=True) == -0.01

    # DEBIT entry (the real trade #1): expect to pay 0.31, actually pay 0.326.
    assert entry_slippage_ps(0.31, 0.326, is_credit=False) == 0.016

    # CREDIT close: expect to pay 0.10, actually pay 0.13 -> adverse 0.03.
    assert exit_slippage_ps(0.10, 0.13, is_credit=True) == 0.03
    # DEBIT close: expect to RECEIVE 0.42, actually receive 0.40 -> adverse 0.02.
    assert exit_slippage_ps(0.42, -0.40, is_credit=False) == 0.02
    # THE BUG THIS LOCKS: with the old magnitude-only call, that debit close
    # produced -0.82 — a huge fake "favourable" fill that deleted the exit cost.
    assert exit_slippage_ps(0.42, -0.40, is_credit=False) > 0


def test_sim_reports_the_broker_price_convention():
    """The sim adapter used to report our own positive-credit price, so
    signed_credit_ps negated an already-correct number and EVERY backtested fill
    booked ~2x the premium as fictitious 'measured' slippage. The broker boundary
    must speak one language: IB's (negative = credit)."""
    from core.brokers.base import OrderLeg, OrderRequest
    from core.brokers.sim import SimAdapter
    from core.fills import entry_slippage_ps, signed_credit_ps

    def _order(limit, est_credit):
        return OrderRequest(
            client_order_id="x",
            legs=[OrderLeg("A", "sell", 1, "option", "put", strike=100, expiration="2026-09-18"),
                  OrderLeg("B", "buy", 1, "option", "put", strike=99, expiration="2026-09-18")],
            limit_price=limit, strategy="put_credit_spread",
            est_credit=est_credit, underlying="SPY")

    a = SimAdapter(asof="2026-01-15")
    credit = a.place_order(_order(0.26, 26.0))
    assert credit.filled_avg_price == -0.26, "a CREDIT is negative at the broker"
    assert signed_credit_ps(credit.filled_avg_price) == 0.26
    # A perfect sim fill must therefore measure ZERO slippage, not 2x the credit.
    assert entry_slippage_ps(0.26, credit.filled_avg_price, is_credit=True) == 0.0

    debit = a.place_order(_order(0.31, 0.0))
    assert debit.filled_avg_price == 0.31, "a DEBIT is positive at the broker"
    assert entry_slippage_ps(0.31, debit.filled_avg_price, is_credit=False) == 0.0


def test_trade_rows_and_metrics_agree_on_cost():
    """REGRESSION: trade_rows (the tax/trade-log CSV) computed cost WITHOUT the
    measured slips, so exports/trades.csv and `cli.py performance` disagreed
    about the same trades — two different truths from one book."""
    from core.performance import compute_metrics, trade_rows
    closed = [{
        "realized_pnl": 55.0, "closed_asof": "2026-08-17", "opened_asof": "2026-08-05",
        "structure": "put_credit_spread", "contracts": 5, "width": 1.0,
        "exit_reason": "profit_target", "entry_slip_ps": 0.0139, "exit_slip_ps": 0.02,
    }]
    m = compute_metrics(closed, ["2026-08-17"], CostModel())
    rows = trade_rows(closed, CostModel())
    assert round(sum(r["cost"] for r in rows), 2) == m["total_costs"]
    assert round(sum(r["net_pnl"] for r in rows), 2) == m["net_pnl"]
    assert rows[0]["entry_slip_ps"] == 0.0139        # the CSV states its evidence
    assert rows[0]["cost_measured_pct"] == 1.0


def test_graduation_days_measure_elapsed_sessions_not_closing_dates():
    """REGRESSION (operator caught it): the gate received 'distinct dates a trade
    CLOSED', so 2 closes read as 2/60 'trading days' while the system had been
    running 36 sessions. Worse, that count is bounded above by trade count, so
    requiring 60 of them implied 60+ closes — making the separate '40 closed
    trades' hurdle DEAD CODE that could never bind. The two conditions are meant
    to be independent: enough elapsed time AND enough sample."""
    import tempfile
    from pathlib import Path
    from core.performance import compute_metrics, graduation_status, paper_record_sessions
    from core.store import Store

    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "p.db")
        # 36 sessions of IV, 5 of them BEFORE the strategy ever traded.
        for i in range(41):
            st.record_iv_snapshot("SPY", f"2026-07-{i+1:02d}" if i < 31
                                  else f"2026-08-{i-30:02d}", 0.15)
        st.open_position({
            "client_order_id": "c", "strategy": "s", "structure": "put_credit_spread",
            "family": "put", "is_credit": 1, "underlying": "SPY", "status": "closed",
            "opened_asof": "2026-07-06", "opened_ts": "t", "expiration": "2026-09-04",
            "contracts": 1, "short_strike": 500.0, "long_strike": 499.0, "width": 1.0,
            "legs_json": "[]", "entry_credit_ps": 0.2, "max_loss": 80.0})
        # The record starts at the FIRST TRADE, not at VM provisioning.
        n = paper_record_sessions(st)
        assert n == 36, f"expected 36 elapsed sessions since the first trade, got {n}"
        st.close()

    # Two closes on two dates, but a 36-session record: days reflects ELAPSED
    # time and the TRADES hurdle now binds independently.
    closed = [{"realized_pnl": 10.0, "closed_asof": "2026-08-05",
               "structure": "put_credit_spread", "contracts": 1, "width": 1.0,
               "exit_reason": "profit_target"},
              {"realized_pnl": -5.0, "closed_asof": "2026-08-18",
               "structure": "put_credit_spread", "contracts": 1, "width": 1.0,
               "exit_reason": "stop_loss"}]
    m = compute_metrics(closed, ["2026-08-05", "2026-08-18"], CostModel(), sessions=36)
    assert m["days"] == 36 and m["closing_days"] == 2
    reasons = graduation_status(m)["reasons"]
    assert any("36/60" in r for r in reasons), "days must report elapsed sessions"
    assert any("2/40 closed trades" in r for r in reasons), "the trade hurdle must BIND"

    # And the hurdles are genuinely independent now: a long record with too few
    # trades is refused on trades alone.
    m2 = compute_metrics(closed, ["2026-08-05", "2026-08-18"], CostModel(), sessions=99)
    assert not any("trading sessions" in r for r in graduation_status(m2)["reasons"])
    assert any("closed trades" in r for r in graduation_status(m2)["reasons"])
