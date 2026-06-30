"""Tests for THE RISK GATE — the most important code in the system."""

from core.brokers.base import Account, OrderLeg, OrderRequest, Position
from core.risk import RiskContext, RiskGate, RiskLimits, _notional, _has_naked_short_option


def _account(equity=30000.0, acct_type="margin", dt=0):
    return Account(equity=equity, cash=equity, buying_power=equity * 2,
                   account_type=acct_type, is_paper=True, pdt_day_trade_count=dt)


def _ctx(account=None, positions=None, kill=False, opened=0, daily_pnl=0.0, level=3):
    return RiskContext(
        account=account or _account(), positions=positions or [],
        is_paper=True, kill_switch=kill, positions_opened_today=opened,
        day_trades_trailing_5=(account.pdt_day_trade_count if account else 0),
        daily_pnl=daily_pnl, options_approval_level=level,
    )


def _csp_order(strike=50.0, credit=0.50, max_loss=None):
    """A small cash-secured put: defined risk by collateral."""
    ml = (strike - credit) * 100 if max_loss is None else max_loss
    return OrderRequest(
        client_order_id="t1",
        legs=[OrderLeg(symbol="XLF_P", side="sell", qty=1, asset_class="option",
                       option_type="put", strike=strike)],
        limit_price=credit, strategy="cash_secured_put", max_loss=ml,
        est_credit=credit * 100, underlying="XLF", required_approval_level=2,
    )


def _spread_order(width=1.0, credit=0.30, underlying="XLF", short_strike=40.0):
    """A defined-risk put credit spread: sell short_strike put, buy the wing below."""
    max_loss = round((width - credit) * 100, 2)
    return OrderRequest(
        client_order_id="sp1",
        legs=[
            OrderLeg(symbol=f"{underlying}_Pshort", side="sell", qty=1,
                     asset_class="option", option_type="put", strike=short_strike),
            OrderLeg(symbol=f"{underlying}_Plong", side="buy", qty=1,
                     asset_class="option", option_type="put", strike=short_strike - width),
        ],
        limit_price=credit, strategy="put_credit_spread", max_loss=max_loss,
        est_credit=credit * 100, underlying=underlying, required_approval_level=3,
    )


def _gate(**overrides):
    lim = RiskLimits()
    for k, v in overrides.items():
        setattr(lim, k, v)
    return RiskGate(lim)


def test_kill_switch_blocks_everything():
    d = _gate().evaluate(_csp_order(), _ctx(kill=True))
    assert not d.approved
    assert any("Kill switch" in r for r in d.reasons)


def test_live_blocked_in_phase0():
    ctx = _ctx()
    ctx.is_paper = False
    d = _gate().evaluate(_csp_order(), ctx)
    assert not d.approved


def test_small_spread_approved():
    # $1-wide put credit spread: max loss ~$70, collateral $100 — fits all caps
    # on a $30k account. This is the Phase 0 starter vehicle.
    d = _gate().evaluate(_spread_order(width=1.0, credit=0.30), _ctx())
    assert d.approved, d.reasons


def test_naked_short_option_blocked():
    o = OrderRequest(
        client_order_id="naked",
        legs=[OrderLeg(symbol="X_P", side="sell", qty=1, asset_class="option",
                       option_type="put", strike=50)],
        limit_price=1.0, strategy="naked_put", max_loss=0.0,  # undefined!
        underlying="X", required_approval_level=2,
    )
    d = _gate().evaluate(o, _ctx())
    assert not d.approved
    assert any("defined-risk" in r.lower() or "naked" in r.lower() for r in d.reasons)


def test_options_approval_ceiling():
    o = _csp_order(strike=12.0, credit=0.30)
    o.required_approval_level = 4
    d = _gate().evaluate(o, _ctx(level=3))
    assert not d.approved
    assert any("level" in r.lower() for r in d.reasons)


def test_per_position_notional_cap():
    # strike 200 -> $20k collateral on $30k equity >> 5% notional cap.
    d = _gate().evaluate(_csp_order(strike=200.0, credit=1.0), _ctx())
    assert not d.approved
    assert any("notional" in r.lower() for r in d.reasons)


def test_per_trade_risk_cap():
    # Force a large max_loss to trip the per-trade risk limit specifically.
    o = _csp_order(strike=12.0, credit=0.30, max_loss=5000.0)
    d = _gate().evaluate(o, _ctx())
    assert not d.approved
    assert any("per-trade risk" in r.lower() for r in d.reasons)


def test_max_concurrent_positions():
    positions = [Position(symbol=f"S{i}", qty=1, avg_price=1, market_value=10)
                 for i in range(6)]
    d = _gate(max_concurrent_positions=6).evaluate(_csp_order(strike=12.0), _ctx(positions=positions))
    assert not d.approved
    assert any("concurrent" in r.lower() for r in d.reasons)


def test_daily_loss_trips_kill_switch():
    d = _gate(daily_loss_limit_pct=0.03).evaluate(
        _csp_order(strike=12.0), _ctx(daily_pnl=-1000.0))  # -1000 < -3% of 30k=-900
    assert not d.approved
    assert d.trip_kill_switch


def test_pdt_guard_for_small_margin_account():
    acct = _account(equity=10000.0, acct_type="margin", dt=3)
    o = _csp_order(strike=12.0, credit=0.30)
    o.is_day_trade = True
    d = _gate().evaluate(o, _ctx(account=acct))
    assert not d.approved
    assert any("pdt" in r.lower() for r in d.reasons)


def test_pdt_guard_not_triggered_for_cash_account():
    acct = _account(equity=10000.0, acct_type="cash", dt=9)
    # Small defined-risk spread that clears every other cap on a $10k account,
    # so the PDT guard is the only variable under test.
    o = _spread_order(width=0.5, credit=0.20)
    o.is_day_trade = True
    d = _gate().evaluate(o, _ctx(account=acct))
    assert d.approved, d.reasons


def test_fail_closed_on_zero_equity():
    d = _gate().evaluate(_csp_order(strike=12.0), _ctx(account=_account(equity=0.0)))
    assert not d.approved


def test_naked_short_with_fabricated_maxloss_still_blocked():
    # A genuinely naked short put mislabeled with a positive max_loss must NOT
    # slip past the defined-risk guard (structural detection, not metadata trust).
    o = OrderRequest(
        client_order_id="fake",
        legs=[OrderLeg(symbol="X_P", side="sell", qty=1, asset_class="option",
                       option_type="put", strike=50)],
        limit_price=1.0, strategy="totally_legit", max_loss=500.0,  # fabricated
        underlying="X", required_approval_level=2,
    )
    assert _has_naked_short_option(o) is True
    d = _gate().evaluate(o, _ctx())
    assert not d.approved
    assert any("naked" in r.lower() or "defined-risk" in r.lower() for r in d.reasons)


def test_iron_condor_notional_uses_wing_not_span():
    # Wings 1-wide each; collateral should be ~$100, not the $4200 outer span.
    legs = [
        OrderLeg("SP", "sell", 1, "option", "put", strike=440),
        OrderLeg("LP", "buy", 1, "option", "put", strike=439),
        OrderLeg("SC", "sell", 1, "option", "call", strike=480),
        OrderLeg("LC", "buy", 1, "option", "call", strike=481),
    ]
    ic = OrderRequest("ic", legs, limit_price=0.40, strategy="iron_condor",
                      max_loss=60.0, est_credit=40.0, underlying="SPY",
                      required_approval_level=3)
    assert _notional(ic) == 100.0


def test_portfolio_heat_cap_enforced():
    # Existing open defined risk near the heat cap; a new order tips it over.
    big = [Position(symbol="A", qty=1, avg_price=0, market_value=50, max_loss=1700.0)]
    # heat cap default 6% of 30k = 1800; existing 1700 + new 70 = 1770 < 1800 OK,
    # but bump existing to 1750 so 1750 + 70 = 1820 > 1800 -> reject.
    big[0].max_loss = 1750.0
    d = _gate().evaluate(_spread_order(width=1.0, credit=0.30), _ctx(positions=big))
    assert not d.approved
    assert any("heat" in r.lower() for r in d.reasons)
