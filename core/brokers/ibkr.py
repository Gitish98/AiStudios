"""
Interactive Brokers adapter (PAPER) — for an Ontario-resident, non-registered
MARGIN account trading US equities/ETFs/options.

IBKR's API is not a cloud REST endpoint: your code connects over a socket to a
running **IB Gateway** (or TWS) that is authenticated to your account. So this
adapter needs a Gateway reachable at host:port. With no Gateway running (e.g.
operating from a phone), the factory falls back to the sim broker — see
core/brokers/factory.py and docs/08-ibkr-gateway-runbook.md.

We target `ib_async` (the maintained fork) and fall back to `ib_insync` (same
API). Neither is imported until we actually connect, so this module imports fine
on a machine that has neither installed — the pure order-planning functions
below are unit-tested without any IB library.

Port convention (IB defaults):
    IB Gateway  paper = 4002   live = 4001
    TWS         paper = 7497   live = 7496
This adapter REFUSES to construct against a live port unless paper=False is
explicitly passed (the factory never does that in the current phase).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Optional

from .base import (
    Account, BrokerAdapter, OptionContract, OrderLeg, OrderRequest, OrderResult,
    Position, Quote,
)

LIVE_PORTS = {4001, 7496}
PAPER_PORTS = {4002, 7497}


# ── Pure, unit-testable order/contract planning (no IB library needed) ────────

def ib_expiry(date_str: str) -> str:
    """'2026-02-19' -> '20260219' (IB's lastTradeDateOrContractMonth)."""
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y%m%d")


def option_right(option_type: str) -> str:
    return "C" if option_type == "call" else "P"


def build_order_plan(order: OrderRequest) -> dict:
    """Translate an OrderRequest into a broker-neutral IB order PLAN (a dict).

    The live path resolves each leg to an IB conId and submits a BAG combo; this
    function captures every convention that must be correct: leg actions, GCD-
    reduced ratios, and the limit-price SIGN.

    IB combo sign convention: submit the BAG with action BUY; a net DEBIT is a
    POSITIVE limit price and a net CREDIT is NEGATIVE. Our codebase carries a
    credit as a positive number (est_credit / limit_price), so we negate it.
    """
    from functools import reduce
    from math import gcd

    if len(order.legs) == 1:
        leg = order.legs[0]
        sec_type = "OPT" if leg.asset_class == "option" else "STK"
        plan: dict[str, Any] = {
            "kind": "single",
            "sec_type": sec_type,
            "symbol": leg.symbol if sec_type == "STK" else (order.underlying or leg.symbol),
            "action": leg.side.upper(),  # BUY | SELL
            "total_quantity": int(round(abs(leg.qty))),
            "order_type": "LMT" if order.limit_price else "MKT",
            "limit_price": order.limit_price,
            "tif": order.time_in_force.upper(),
            "order_ref": order.client_order_id,
        }
        if sec_type == "OPT":
            plan["option"] = {
                "underlying": order.underlying or leg.symbol,
                "expiry": ib_expiry(leg.expiration) if leg.expiration else None,
                "strike": leg.strike,
                "right": option_right(leg.option_type) if leg.option_type else None,
            }
        return plan

    # Multi-leg combo (BAG).
    leg_qtys = [max(1, int(round(abs(l.qty)))) for l in order.legs]
    g = reduce(gcd, leg_qtys) or 1

    price = None
    if order.limit_price:
        is_credit = order.est_credit > 0
        price = -abs(order.limit_price) if is_credit else abs(order.limit_price)

    legs = []
    for l in order.legs:
        legs.append({
            "underlying": order.underlying or l.symbol,
            "expiry": ib_expiry(l.expiration) if l.expiration else None,
            "strike": l.strike,
            "right": option_right(l.option_type) if l.option_type else None,
            "action": l.side.upper(),                 # BUY | SELL
            "ratio": int(round(abs(l.qty))) // g,     # GCD-reduced
        })
    return {
        "kind": "combo",
        "symbol": order.underlying,
        "currency": "USD",
        "action": "BUY",            # BUY the BAG; credit reflected by negative price
        "total_quantity": g,
        "order_type": "LMT" if order.limit_price is not None else "MKT",
        "limit_price": price,
        "tif": order.time_in_force.upper(),
        "order_ref": order.client_order_id,
        "legs": legs,
    }


def _load_ib():
    """Import the IB library lazily. Prefer the maintained ib_async fork; fall
    back to ib_insync. Raises a clear error if neither is installed."""
    try:
        import ib_async as ib  # type: ignore
        return ib
    except ImportError:
        try:
            import ib_insync as ib  # type: ignore
            return ib
        except ImportError as e:
            raise RuntimeError(
                "Neither ib_async nor ib_insync is installed. "
                "Run: pip install ib_async   (only needed to connect to IB Gateway)."
            ) from e


class IBKRAdapter(BrokerAdapter):
    name = "ibkr_paper"

    def __init__(self, host: str = "127.0.0.1", port: int = 4002,
                 client_id: int = 7, paper: bool = True, region: str = "CA",
                 timeout: float = 8.0):
        if not paper and port in PAPER_PORTS:
            raise ValueError("paper=False but port is a paper port — refusing ambiguous config.")
        if paper and port in LIVE_PORTS:
            raise ValueError(
                f"port {port} is a LIVE IB port but paper=True. Use 4002 (Gateway) "
                "or 7497 (TWS) for paper."
            )
        self.host, self.port, self.client_id = host, port, client_id
        self._paper = paper
        self.region = region
        self.timeout = timeout
        self._ib = None  # set on connect()

    @property
    def is_paper(self) -> bool:
        return self._paper

    # ── connection ───────────────────────────────────────────────────────────
    def try_connect(self) -> bool:
        """Best-effort connect for the factory's graceful fallback. Returns
        False (never raises) if the IB library is missing or no Gateway answers."""
        try:
            self.connect()
            return True
        except Exception:
            return False

    def connect(self):
        ib = _load_ib()
        self._ib = ib.IB()
        self._ib.connect(self.host, self.port, clientId=self.client_id,
                         timeout=self.timeout, readonly=False)
        return self._ib

    def _require(self):
        if self._ib is None or not getattr(self._ib, "isConnected", lambda: False)():
            self.connect()
        return self._ib

    def disconnect(self) -> None:
        if self._ib is not None:
            try:
                self._ib.disconnect()
            except Exception:
                pass
            self._ib = None

    # ── account / positions ──────────────────────────────────────────────────
    def get_account(self) -> Account:
        ib = self._require()
        vals = {v.tag: v.value for v in ib.accountValues()
                if v.currency in ("USD", "BASE", "")}
        equity = float(vals.get("NetLiquidation", 0) or 0)
        cash = float(vals.get("TotalCashValue", vals.get("CashBalance", 0)) or 0)
        bp = float(vals.get("BuyingPower", 0) or 0)
        return Account(
            equity=equity, cash=cash, buying_power=bp,
            account_type="margin", is_paper=self._paper,
            pdt_day_trade_count=0,  # US-only concept; not applicable to a CA account
        )

    def get_positions(self) -> list[Position]:
        ib = self._require()
        out = []
        for p in ib.positions():
            c = p.contract
            out.append(Position(
                symbol=c.localSymbol or c.symbol, qty=float(p.position),
                avg_price=float(p.avgCost or 0),
                market_value=float(p.position) * float(p.avgCost or 0),
                asset_class="option" if c.secType == "OPT" else "us_equity",
                underlying=getattr(c, "symbol", None),
            ))
        return out

    # ── market data ──────────────────────────────────────────────────────────
    def get_quote(self, symbol: str) -> Quote:
        ib = _load_ib()
        live = self._require()
        contract = ib.Stock(symbol, "SMART", "USD")
        live.qualifyContracts(contract)
        t = live.reqMktData(contract, "", False, False)
        live.sleep(1.0)
        bid = float(t.bid or 0); ask = float(t.ask or 0)
        last = float(t.last or t.close or 0)
        return Quote(symbol=symbol.upper(), bid=bid, ask=ask, last=last)

    def get_option_chain(self, underlying: str, expiration: Optional[str] = None) -> list[OptionContract]:
        ib = _load_ib()
        live = self._require()
        stock = ib.Stock(underlying, "SMART", "USD")
        live.qualifyContracts(stock)
        [ticker] = live.reqTickers(stock)
        spot = float(ticker.marketPrice() or ticker.close or 0)

        params = live.reqSecDefOptParams(stock.symbol, "", stock.secType, stock.conId)
        smart = next((p for p in params if p.exchange == "SMART"), params[0] if params else None)
        if not smart:
            return []

        today = date.today()
        # Choose the expiry nearest the 30-45 DTE window if not specified.
        exps = sorted(smart.expirations)
        if expiration:
            target = ib_expiry(expiration)
            exps = [e for e in exps if e == target]
        else:
            def dte(e):
                return (datetime.strptime(e, "%Y%m%d").date() - today).days
            exps = [e for e in exps if 20 <= dte(e) <= 55][:1] or exps[:1]
        if not exps:
            return []
        exp = exps[0]

        # Strikes within ~12% of spot to bound request size.
        strikes = sorted(s for s in smart.strikes
                         if spot * 0.88 <= s <= spot * 1.12) if spot else sorted(smart.strikes)
        contracts = []
        for K in strikes:
            for right in ("P", "C"):
                contracts.append(ib.Option(underlying, exp, K, right, "SMART", currency="USD"))
        live.qualifyContracts(*contracts)
        tickers = live.reqTickers(*contracts)

        out: list[OptionContract] = []
        dte_val = (datetime.strptime(exp, "%Y%m%d").date() - today).days
        for tk in tickers:
            c = tk.contract
            g = tk.modelGreeks
            out.append(OptionContract(
                symbol=c.localSymbol or f"{underlying}{exp}{c.right}{int(c.strike*1000):08d}",
                underlying=underlying.upper(),
                expiration=datetime.strptime(exp, "%Y%m%d").strftime("%Y-%m-%d"),
                strike=float(c.strike), option_type="call" if c.right == "C" else "put",
                bid=float(tk.bid or 0), ask=float(tk.ask or 0), last=float(tk.last or 0),
                implied_vol=float(g.impliedVol) if g and g.impliedVol else None,
                delta=float(g.delta) if g and g.delta is not None else None,
                gamma=float(g.gamma) if g and g.gamma is not None else None,
                theta=float(g.theta) if g and g.theta is not None else None,
                vega=float(g.vega) if g and g.vega is not None else None,
                open_interest=int(tk.callOpenInterest or tk.putOpenInterest or 0),
                volume=int(tk.volume or 0), dte=dte_val,
            ))
        return out

    # ── execution ────────────────────────────────────────────────────────────
    def place_order(self, order: OrderRequest) -> OrderResult:
        ib = _load_ib()
        live = self._require()
        plan = build_order_plan(order)
        try:
            contract = self._plan_to_contract(ib, live, plan)
            ib_order = ib.Order(
                action=plan["action"],
                totalQuantity=plan["total_quantity"],
                orderType=plan["order_type"],
                lmtPrice=plan["limit_price"] if plan["order_type"] == "LMT" else 0.0,
                tif=plan["tif"], orderRef=plan["order_ref"],
                transmit=True,
            )
            trade = live.placeOrder(contract, ib_order)
            live.sleep(0.5)
            st = trade.orderStatus
            return OrderResult(
                accepted=st.status not in ("Inactive", "ApiCancelled", "Cancelled"),
                client_order_id=order.client_order_id,
                broker_order_id=str(trade.order.orderId or trade.order.permId or ""),
                status=st.status or "Submitted",
                filled_qty=float(st.filled or 0),
                filled_avg_price=float(st.avgFillPrice or 0),
                raw={"status": st.status},
            )
        except Exception as e:
            return OrderResult(accepted=False, client_order_id=order.client_order_id,
                               status="rejected", reason=str(e))

    def _plan_to_contract(self, ib, live, plan: dict):
        if plan["kind"] == "single":
            if plan["sec_type"] == "STK":
                c = ib.Stock(plan["symbol"], "SMART", "USD")
            else:
                o = plan["option"]
                c = ib.Option(o["underlying"], o["expiry"], o["strike"], o["right"],
                              "SMART", currency="USD")
            live.qualifyContracts(c)
            return c
        # Combo: resolve each leg to a conId, then build the BAG.
        combo_legs = []
        for leg in plan["legs"]:
            opt = ib.Option(leg["underlying"], leg["expiry"], leg["strike"], leg["right"],
                            "SMART", currency="USD")
            live.qualifyContracts(opt)
            combo_legs.append(ib.ComboLeg(
                conId=opt.conId, ratio=leg["ratio"], action=leg["action"], exchange="SMART"))
        bag = ib.Contract(symbol=plan["symbol"], secType="BAG",
                          currency=plan["currency"], exchange="SMART", comboLegs=combo_legs)
        return bag

    def list_orders(self) -> list[OrderResult]:
        ib = self._require()
        out = []
        for t in ib.trades():
            st = t.orderStatus
            out.append(OrderResult(
                accepted=True, client_order_id=getattr(t.order, "orderRef", "") or "",
                broker_order_id=str(t.order.orderId or ""),
                status=st.status or "", filled_qty=float(st.filled or 0),
                filled_avg_price=float(st.avgFillPrice or 0)))
        return out

    def cancel_order(self, broker_order_id: str) -> bool:
        ib = self._require()
        for t in ib.openTrades():
            if str(t.order.orderId) == str(broker_order_id):
                ib.cancelOrder(t.order)
                return True
        return False
