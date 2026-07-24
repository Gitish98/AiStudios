"""Correlation-cluster caps: correlated names share ONE defined-risk budget.

This was configured and documented as enforced in three places for months while
never being evaluated — with a watchlist where SPY/QQQ/DIA/XLK are effectively the
same bet, that meant "6 diversified positions" could be one leveraged S&P
exposure. docs/05 §0 names exactly this as design target #2.
"""

from __future__ import annotations

from core.brokers.base import Account, OrderLeg, OrderRequest, Position
from core.clusters import (
    cluster_breakdown, cluster_exposure, cluster_of, unmapped_symbols,
)
from core.risk import RiskContext, RiskGate, RiskLimits


def test_the_real_watchlist_collapses_into_few_clusters():
    watch = ["SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE"]
    clusters = {cluster_of(s) for s in watch}
    # 7 symbols, but only 4 distinct bets — the whole point of the cap.
    assert clusters == {"us_large_cap", "us_small_cap", "financials", "energy"}
    assert cluster_of("SPY") == cluster_of("QQQ") == cluster_of("DIA") == cluster_of("XLK")
    assert cluster_of("IWM") != cluster_of("SPY")
    assert unmapped_symbols(watch) == []          # every watchlist symbol is classified


def test_unmapped_symbol_gets_its_own_cluster_never_merged():
    # An unknown symbol must NOT be silently folded into another cluster's budget.
    assert cluster_of("ZZZZ") == "unmapped:ZZZZ"
    assert cluster_of("ZZZZ") != cluster_of("SPY")
    assert unmapped_symbols(["SPY", "ZZZZ"]) == ["ZZZZ"]


def _pos(sym, max_loss):
    return Position(symbol=sym, qty=1, avg_price=0.0, market_value=100.0,
                    asset_class="option", underlying=sym, max_loss=max_loss)


def test_cluster_exposure_sums_correlated_risk():
    book = [_pos("SPY", 300.0), _pos("QQQ", 200.0), _pos("XLE", 400.0)]
    # SPY + QQQ are one cluster: risk ADDS, never diversifies away.
    assert cluster_exposure(book, "DIA") == 500.0     # DIA shares their cluster
    assert cluster_exposure(book, "XLE") == 400.0
    assert cluster_exposure(book, "IWM") == 0.0       # different cluster
    assert cluster_breakdown(book) == {"us_large_cap": 500.0, "energy": 400.0}


def _spread(underlying="QQQ", max_loss=400.0):
    return OrderRequest(
        client_order_id="c1",
        legs=[OrderLeg(f"{underlying}_SP", "sell", 1, "option", "put", strike=40),
              OrderLeg(f"{underlying}_LP", "buy", 1, "option", "put", strike=39)],
        limit_price=0.30, strategy="put_credit_spread", max_loss=max_loss,
        est_credit=30.0, underlying=underlying, required_approval_level=3)


def _ctx(positions):
    acct = Account(equity=30000.0, cash=30000.0, buying_power=60000.0,
                   account_type="margin", is_paper=True)
    return RiskContext(account=acct, positions=positions, is_paper=True,
                       kill_switch=False, options_approval_level=3, region="CA")


def test_correlated_book_is_blocked_before_uncorrelated_one():
    """THE regression. On $30k equity the cluster cap is 3% = $900, while total
    portfolio heat allows 6% = $1800. A book already holding $800 of us_large_cap
    risk must REJECT another large-cap spread while ACCEPTING the identical trade
    in an uncorrelated cluster — heat alone would wave both through."""
    gate = RiskGate(RiskLimits())
    book = [_pos("SPY", 450.0), _pos("QQQ", 350.0)]   # $800 in ONE cluster
    order = _spread(max_loss=140.0)                   # within per-trade (0.5% = $150)

    # A third large-cap spread: cluster $940 > $900 -> rejected on cluster heat...
    d = gate.evaluate(_spread("XLK", 140.0), _ctx(book))
    assert not d.approved
    assert any("cluster heat" in r.lower() for r in d.reasons)
    # ...while total heat ($940) is comfortably under the 6% portfolio cap ($1800),
    # i.e. ONLY the cluster cap catches this.
    assert 940 < 0.06 * 30000

    # The identical-size trade in an uncorrelated cluster is allowed.
    d2 = gate.evaluate(_spread("XLE", 140.0), _ctx(book))
    assert d2.approved, d2.reasons


def test_cluster_cap_is_configurable_and_can_be_relaxed():
    gate = RiskGate(RiskLimits(max_cluster_risk_pct=0.10))
    book = [_pos("SPY", 450.0), _pos("QQQ", 350.0)]
    assert gate.evaluate(_spread("XLK", 140.0), _ctx(book)).approved
