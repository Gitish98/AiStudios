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

import math
from datetime import date, datetime, timedelta
from typing import Any, Optional

from .base import (
    Account, BrokerAdapter, OptionContract, OrderLeg, OrderRequest, OrderResult,
    Position, Quote,
)
from ..options_math import fill_greeks_from_mid

# Expiry selection for chain fetches. We return SEVERAL expiries around
# TARGET_DTE rather than just the nearest, because the strategies' own windows
# (premium_harvest 30-45 DTE) sit INSIDE this band — handing them only the
# soonest expiry made signalling structurally impossible most of the month.
EXPIRY_DTE_MIN, EXPIRY_DTE_MAX, TARGET_DTE, MAX_EXPIRIES = 20, 60, 35, 3
# IBKR allows only ~100 SIMULTANEOUS market-data lines. reqTickers on a whole
# multi-expiry chain at once silently exceeds that: the call returns, but the
# throttled contracts arrive with modelGreeks=None, so every delta filter matches
# nothing and the engine reports "0 signals" while looking healthy. Request in
# chunks below the limit instead. (Measured: 732 contracts in one call -> 0 of 98
# in-window puts had a delta; chunked -> greeks populate.)
TICKER_CHUNK = 60
# Seconds to let a delayed-data batch stream in before reading it. reqTickers
# does not wait long enough for delayed ticks (and never asks for open interest).
CHAIN_SETTLE_SECS = 4.0

LIVE_PORTS = {4001, 7496}
PAPER_PORTS = {4002, 7497}


# ── Pure, unit-testable order/contract planning (no IB library needed) ────────

def ib_expiry(date_str: str) -> str:
    """'2026-02-19' -> '20260219' (IB's lastTradeDateOrContractMonth)."""
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y%m%d")


def option_right(option_type: str) -> str:
    return "C" if option_type == "call" else "P"


def pick_spot(*candidates: Any) -> Optional[float]:
    """First strictly-POSITIVE, finite price from the candidates, else None.

    IB's marketPrice() returns -1.0 as a "no live data" sentinel after hours, and
    -1.0 is truthy — so `marketPrice() or close` short-circuits to -1.0 and never
    reaches the close fallback. A negative spot then makes the strike band
    (spot*0.92 .. spot*1.08) negative, yielding ZERO strikes and an empty chain:
    the whole chain silently disappears near the close, exactly when the 15:30
    cron runs. Order candidates best-first (marketPrice, then last, then close)."""
    for x in candidates:
        v = _to_float_opt(x)
        if v is not None and v > 0:
            return v
    return None


def _to_float(x: Any, default: float = 0.0) -> float:
    """float(x), treating None and NaN as the default. IB market-data fields come
    back as NaN (not None) when absent — and `NaN or 0` is NaN (NaN is truthy), so
    naive `float(x or 0)` / `int(x or 0)` propagate NaN or crash."""
    try:
        v = float(x)
        return default if math.isnan(v) else v
    except (TypeError, ValueError):
        return default


def _to_int(x: Any, default: int = 0) -> int:
    try:
        return int(_to_float(x, float(default)))
    except (TypeError, ValueError):
        return default


def _to_float_opt(x: Any) -> Optional[float]:
    """Like _to_float but None (not 0.0) when absent/NaN — for optional greeks/IV."""
    try:
        v = float(x)
        return None if math.isnan(v) else v
    except (TypeError, ValueError):
        return None


def parse_account_values(rows: list[tuple[str, str, Any]]) -> dict:
    """Pure, unit-testable: turn IB (tag, currency, value) account rows into USD
    equity / cash / buying_power.

    Why this is not trivial: IBKR reports the account-SUMMARY tags (NetLiquidation,
    TotalCashValue, BuyingPower) ONLY in the account's BASE currency. A CA margin
    account is CAD-base, so filtering to USD (as the old code did) found no rows and
    read $0 — which fails the risk gate closed. Worse, per-currency rows like
    CashBalance exist in USD too (a stray FX sub-balance), so a naive read grabbed
    the wrong number.

    We therefore: (1) find the base currency (the one whose ExchangeRate == 1.0,
    else NetLiquidation's currency); (2) read the base-currency summary figures;
    (3) convert to USD via IB's ExchangeRate (units of BASE per 1 USD, e.g. 1.42
    CAD/USD) so the USD-denominated risk gate and notionals compare apples-to-apples.
    If the base is USD, no conversion. If the base is non-USD but no USD rate is
    available, we return base-currency values and flag converted=False so the caller
    can warn rather than silently mis-scale risk."""
    cell: dict[tuple[str, str], Any] = {(t, c): v for (t, c, v) in rows}

    base_ccy: Optional[str] = None
    for (tag, ccy), val in cell.items():
        if tag == "ExchangeRate" and ccy not in ("BASE", "") and _to_float(val) == 1.0:
            base_ccy = ccy
            break
    if base_ccy is None:
        base_ccy = next((c for (t, c) in cell if t == "NetLiquidation"), "USD")

    def base_val(tag: str) -> float:
        for ccy in (base_ccy, "BASE"):
            if (tag, ccy) in cell:
                return _to_float(cell[(tag, ccy)])
        return 0.0

    equity = base_val("NetLiquidation")
    cash = base_val("TotalCashValue") or base_val("CashBalance")
    bp = base_val("BuyingPower")

    usd_rate = _to_float(cell.get(("ExchangeRate", "USD")), 0.0)
    converted = False
    if base_ccy != "USD" and usd_rate > 0:
        equity, cash, bp = equity / usd_rate, cash / usd_rate, bp / usd_rate
        converted = True
    return {"equity": equity, "cash": cash, "buying_power": bp,
            "base_currency": base_ccy,
            "usd_rate": (1.0 if base_ccy == "USD" else usd_rate),
            "converted": converted}


def fail_closed_if_unconverted(a: dict) -> dict:
    """Zero out account figures when a non-USD base could not be converted.

    Every cap, the sizing budget, and the daily-loss threshold are USD-denominated.
    Feeding them raw CAD numbers overstates equity ~42% — every limit silently
    loosens by the FX rate. The gate already fails closed on equity<=0, so zeroing
    is the correct closed behavior; the fx_note explains WHY nothing is trading."""
    if a.get("base_currency") != "USD" and not a.get("converted"):
        a = dict(a)
        a["equity"] = a["cash"] = a["buying_power"] = 0.0
        a["fx_failed_closed"] = True
    return a


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
            "order_type": "LMT" if order.limit_price is not None else "MKT",
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
    if order.limit_price is not None:   # 0.0 is a valid combo limit (worthless close)
        is_credit = order.est_credit > 0
        price = -abs(order.limit_price) if (is_credit and order.limit_price) else abs(order.limit_price)

    legs = []
    for l in order.legs:
        legs.append({
            "underlying": order.underlying or l.symbol,
            "expiry": ib_expiry(l.expiration) if l.expiration else None,
            "strike": l.strike,
            "right": option_right(l.option_type) if l.option_type else None,
            "action": l.side.upper(),                 # BUY | SELL
            "ratio": int(round(abs(l.qty))) // g,     # GCD-reduced
            "intent": getattr(l, "intent", "open"),   # open | close
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


def pick_secdef_params(params: list, underlying: str):
    """Choose the SMART secdef entry for the STANDARD option class.

    Some ETFs (SPY, IWM) carry a SECOND options class at IB (tradingClass '2SPY',
    '2IWM') whose strikes overlap the standard one. Building contracts without a
    tradingClass then makes qualifyContracts AMBIGUOUS for nearly every strike, the
    unqualified contracts get dropped, and the chain collapses to a couple of odd
    survivors (observed live: SPY/IWM returned 2 contracts, 0 with IV, while QQQ
    returned 214/112). Prefer exchange==SMART with tradingClass==underlying; fall
    back to any SMART entry, then the first."""
    smart = [p for p in params if getattr(p, "exchange", "") == "SMART"]
    for p in smart:
        if getattr(p, "tradingClass", "").upper() == underlying.upper():
            return p
    return smart[0] if smart else (params[0] if params else None)


def _qualified_option(ib, live, underlying: str, expiry, strike, right):
    """Qualify ONE option for the order path, pinning the standard tradingClass
    first (disambiguates dual-class underlyings); if that class doesn't exist for
    this symbol, retry unpinned. Returns the contract (conId set iff qualified)."""
    opt = ib.Option(underlying, expiry, strike, right, "SMART", currency="USD",
                    tradingClass=underlying)
    live.qualifyContracts(opt)
    if not getattr(opt, "conId", 0):
        opt = ib.Option(underlying, expiry, strike, right, "SMART", currency="USD")
        live.qualifyContracts(opt)
    return opt


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
                 timeout: float = 8.0, market_data_type: int = 3):
        if not paper and port in PAPER_PORTS:
            raise ValueError("paper=False but port is a paper port — refusing ambiguous config.")
        if paper and port in LIVE_PORTS:
            raise ValueError(
                f"port {port} is a LIVE IB port but paper=True. Use 4002 (Gateway) "
                "or 7497 (TWS) for paper."
            )
        self.host, self.port, self.client_id = host, port, client_id
        self._paper = paper
        # Be honest in the banner/journal about which endpoint this is. A live
        # adapter MUST NOT report "ibkr_paper".
        self.name = "ibkr_paper" if paper else "ibkr_live"
        self.region = region
        self.timeout = timeout
        # IB market-data type: 1=live, 2=frozen, 3=delayed, 4=delayed-frozen.
        # Default 3 (delayed) — free, needs no real-time subscription, and does NOT
        # contend with a live session (avoids "Error 10197: No market data during
        # competing live session"). Matches docs/05: paper-trade the delay you'll
        # trade live on. A live operator with real-time entitlements can set 1.
        self.market_data_type = int(market_data_type)
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
        # Select the market-data type up front so every reqMktData/reqTickers below
        # uses it. Best-effort: a failure here must not break the connection.
        try:
            self._ib.reqMarketDataType(self.market_data_type)
        except Exception:
            pass
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
        rows = [(v.tag, v.currency, v.value) for v in ib.accountValues()]
        a = parse_account_values(rows)
        # Remember the currency situation so the CLI can surface it transparently.
        self.base_currency = a["base_currency"]
        if a["converted"]:
            self.fx_note = (f"account base is {a['base_currency']}; equity/cash/BP "
                            f"converted to USD at {a['usd_rate']:.4f} {a['base_currency']}/USD.")
        elif a["base_currency"] != "USD":
            # FAIL CLOSED, not approximate: unconverted CAD figures would loosen
            # every USD cap by the FX rate. Zeroed equity makes the gate reject
            # everything until a rate is available.
            a = fail_closed_if_unconverted(a)
            self.fx_note = (f"account base is {a['base_currency']} but no USD FX rate is "
                            "available — FAILING CLOSED (equity reported as 0 so the "
                            "risk gate rejects all orders until FX returns).")
        else:
            self.fx_note = None
        return Account(
            equity=a["equity"], cash=a["cash"], buying_power=a["buying_power"],
            account_type="margin", is_paper=self._paper,
            pdt_day_trade_count=0,  # US-only concept; not applicable to a CA account
        )

    def get_positions(self) -> list[Position]:
        ib = self._require()
        out = []
        for p in ib.positions():
            c = p.contract
            is_opt = c.secType == "OPT"
            exp = None
            if is_opt and getattr(c, "lastTradeDateOrContractMonth", None):
                raw = c.lastTradeDateOrContractMonth  # YYYYMMDD
                exp = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}" if len(raw) >= 8 else None
            out.append(Position(
                symbol=c.localSymbol or c.symbol, qty=float(p.position),
                avg_price=float(p.avgCost or 0),
                market_value=float(p.position) * float(p.avgCost or 0),
                asset_class="option" if is_opt else "us_equity",
                underlying=getattr(c, "symbol", None),
                option_expiration=exp,
                option_strike=float(c.strike) if is_opt and c.strike else None,
                option_right=(c.right or None) if is_opt else None,
            ))
        return out

    # ── market data ──────────────────────────────────────────────────────────
    def get_quote(self, symbol: str) -> Quote:
        ib = _load_ib()
        live = self._require()
        contract = ib.Stock(symbol, "SMART", "USD")
        live.qualifyContracts(contract)
        # Blocking snapshot (respects the session market-data type). More robust than
        # reqMktData+sleep(1s), which often hasn't populated delayed ticks in time.
        [t] = live.reqTickers(contract)
        bid, ask = _to_float(t.bid), _to_float(t.ask)
        # pick_spot filters out IB's -1.0 "no data" sentinel (marketPrice can be
        # -1 after hours); a plain `or` chain would leak it as a negative last.
        last = pick_spot(t.last, t.close, t.marketPrice()) or 0.0
        return Quote(symbol=symbol.upper(), bid=bid, ask=ask, last=last)

    def get_option_chain(self, underlying: str, expiration: Optional[str] = None) -> list[OptionContract]:
        ib = _load_ib()
        live = self._require()
        stock = ib.Stock(underlying, "SMART", "USD")
        live.qualifyContracts(stock)
        [ticker] = live.reqTickers(stock)
        spot = pick_spot(ticker.marketPrice(), ticker.last, ticker.close)
        if not spot:
            # No usable underlying price -> we cannot bound the strike band, so we
            # cannot build a meaningful chain. Return empty (the strategy stands
            # aside) rather than silently ranging over garbage strikes.
            return []

        params = live.reqSecDefOptParams(stock.symbol, "", stock.secType, stock.conId)
        smart = pick_secdef_params(params, underlying)
        if not smart:
            return []
        trading_class = getattr(smart, "tradingClass", "") or underlying

        today = date.today()
        # Choose the expiry nearest the 30-45 DTE window if not specified.
        exps = sorted(smart.expirations)
        if expiration:
            target = ib_expiry(expiration)
            exps = [e for e in exps if e == target]
        else:
            def dte(e):
                return (datetime.strptime(e, "%Y%m%d").date() - today).days
            # Return SEVERAL expiries spanning the strategies' windows, ordered by
            # closeness to TARGET_DTE. Taking only the nearest expiry in a 20-55
            # band was a silent structural bug: as expiries roll, the nearest one
            # is usually ~20-30 DTE, while premium_harvest requires 30-45 — so the
            # chain handed to the strategy frequently could not contain a single
            # qualifying contract, and the engine could never fire AT ANY IV RANK.
            # The system looked healthy while being incapable of signalling.
            in_window = [e for e in exps if EXPIRY_DTE_MIN <= dte(e) <= EXPIRY_DTE_MAX]
            in_window.sort(key=lambda e: abs(dte(e) - TARGET_DTE))
            exps = in_window[:MAX_EXPIRIES] or exps[:1]
        if not exps:
            return []

        # Strikes within ~8% of spot: the deltas the strategies want (0.12-0.45)
        # sit well inside that, and a tighter band keeps the request count low.
        strikes = sorted(s for s in smart.strikes
                         if spot * 0.92 <= s <= spot * 1.08) if spot else sorted(smart.strikes)
        contracts = []
        for exp in exps:
            for K in strikes:
                for right in ("P", "C"):
                    contracts.append(ib.Option(underlying, exp, K, right, "SMART",
                                               currency="USD", tradingClass=trading_class))
        # qualifyContracts populates conId only for combos that actually exist as
        # listed contracts; a non-existent strike/right is left WITHOUT a conId.
        # Passing such a contract to reqTickers raises "can't be hashed because no
        # 'conId'" and kills the ENTIRE chain — so drop the unqualified ones first.
        live.qualifyContracts(*contracts)
        contracts = [c for c in contracts if getattr(c, "conId", 0)]
        if not contracts:
            return []
        # Stream with generic tick 101 (OPEN INTEREST) and wait, rather than
        # reqTickers. Two reasons proven live: (1) OI is delivered ONLY when tick
        # 101 is requested — reqTickers never asks, so putOpenInterest was always
        # NaN and every min_open_interest filter failed on liquid strikes; (2)
        # delayed ticks arrive slower than reqTickers waits, so it returned NaN for
        # bid/ask/greeks too. A brief settle populates both.
        tks = []
        for i in range(0, len(contracts), TICKER_CHUNK):
            batch = contracts[i:i + TICKER_CHUNK]
            try:
                tks.extend((c, live.reqMktData(c, "101", False, False)) for c in batch)
                live.sleep(CHAIN_SETTLE_SECS)
            except Exception:
                continue   # one bad batch must not lose the whole chain
        live.sleep(CHAIN_SETTLE_SECS)   # final settle for the last batch

        out: list[OptionContract] = []
        for c, tk in tks:
            # Each contract carries its OWN expiry now that the chain spans
            # several; deriving it from a single shared `exp` would mislabel them.
            raw_exp = getattr(c, "lastTradeDateOrContractMonth", "") or ""
            if len(raw_exp) < 8:
                continue
            c_exp = datetime.strptime(raw_exp[:8], "%Y%m%d").date()
            dte_val = (c_exp - today).days
            otype = "call" if c.right == "C" else "put"
            g = tk.modelGreeks
            bid, ask, last = _to_float(tk.bid), _to_float(tk.ask), _to_float(tk.last)
            iv = _to_float_opt(g.impliedVol) if g else None
            delta = _to_float_opt(g.delta) if g else None
            gamma = _to_float_opt(g.gamma) if g else None
            theta = _to_float_opt(g.theta) if g else None
            vega = _to_float_opt(g.vega) if g else None
            greeks_modeled = False

            # BS fallback: if the broker gave us no delta (delayed data, esp.
            # after hours), recover it from the mid. Tagged modeled, never silently
            # blended with real greeks — see options_math.fill_greeks_from_mid.
            if delta is None:
                mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else last
                filled = fill_greeks_from_mid(spot, float(c.strike), dte_val, otype, mid)
                if filled:
                    iv = iv if iv is not None else filled["implied_vol"]
                    delta, gamma = filled["delta"], filled["gamma"]
                    theta, vega = filled["theta"], filled["vega"]
                    greeks_modeled = True

            try:
                live.cancelMktData(c)   # release the streaming line
            except Exception:
                pass

            out.append(OptionContract(
                symbol=c.localSymbol or f"{underlying}{raw_exp[:8]}{c.right}{int(c.strike*1000):08d}",
                underlying=underlying.upper(),
                expiration=c_exp.strftime("%Y-%m-%d"),
                strike=float(c.strike), option_type=otype,
                bid=bid, ask=ask, last=last,
                implied_vol=iv, delta=delta, gamma=gamma, theta=theta, vega=vega,
                greeks_modeled=greeks_modeled,
                open_interest=_to_int(tk.putOpenInterest if c.right == "P"
                                      else tk.callOpenInterest),
                volume=_to_int(tk.volume), dte=dte_val,
            ))
        return out

    def mark_option(self, underlying: str, expiration: str, strike: float,
                    right: str, asof=None):
        """Per-share mid for ONE option contract, or None when no usable quote.

        WHY: the management pass used to mark positions by fetching the WHOLE
        multi-expiry chain (~30-120s each with streaming settles). At 4-6 open
        positions that blows the cron's timeout and later positions silently go
        unmanaged. Marking needs exactly the position's own strikes — four
        contracts at most — so fetch exactly those. `_marker` in core/manage.py
        prefers this method automatically when an adapter provides it.

        Same honesty rules as the chain path: 0/NaN quotes are ABSENT (None),
        never a fabricated price; tradingClass is pinned to the underlying so
        SPY/IWM dual-class ambiguity cannot pick the wrong contract."""
        ib = _load_ib()
        live = self._require()
        try:
            c = ib.Option(underlying, ib_expiry(expiration), float(strike),
                          option_right(right), "SMART", currency="USD",
                          tradingClass=underlying)
            live.qualifyContracts(c)
            if not getattr(c, "conId", 0):
                return None
            tk = live.reqMktData(c, "", False, False)
            live.sleep(CHAIN_SETTLE_SECS)
            bid, ask, last = _to_float(tk.bid), _to_float(tk.ask), _to_float(tk.last)
            try:
                live.cancelMktData(c)
            except Exception:
                pass
            if bid > 0 and ask > 0:
                return round((bid + ask) / 2, 4)
            if last > 0:
                return last
            return None
        except Exception:
            return None                      # no mark -> the caller HOLDS, never guesses

    def get_history(self, symbol: str, days: int = 120,
                    asof: Optional[str] = None) -> list[dict]:
        ib = _load_ib()
        live = self._require()
        stock = ib.Stock(symbol, "SMART", "USD")
        live.qualifyContracts(stock)
        # IB rejects very large "D" durations for daily bars; switch to years past
        # ~1y so a multi-year VRP study can pull one clean request.
        dur = f"{max(days, 30)} D" if days <= 365 else f"{min(15, days // 365 + 1)} Y"
        bars = live.reqHistoricalData(
            stock, endDateTime="", durationStr=dur,
            barSizeSetting="1 day", whatToShow="TRADES", useRTH=True, formatDate=1)
        return [{"date": str(b.date), "o": float(b.open), "h": float(b.high),
                 "l": float(b.low), "c": float(b.close)} for b in bars]

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
                live.qualifyContracts(c)
            else:
                o = plan["option"]
                c = _qualified_option(ib, live, o["underlying"], o["expiry"],
                                      o["strike"], o["right"])
            return c
        # Combo: resolve each leg to a conId, then build the BAG. tradingClass is
        # pinned per leg so dual-class underlyings (SPY/2SPY) can't come back
        # ambiguous and sink the whole combo.
        combo_legs = []
        for leg in plan["legs"]:
            opt = _qualified_option(ib, live, leg["underlying"], leg["expiry"],
                                    leg["strike"], leg["right"])
            # IB ComboLeg.openClose: 1 = OPEN, 2 = CLOSE. Setting this explicitly
            # stops a managed close from being treated as opening a new position.
            open_close = 2 if leg.get("intent") == "close" else 1
            combo_legs.append(ib.ComboLeg(
                conId=opt.conId, ratio=leg["ratio"], action=leg["action"],
                exchange="SMART", openClose=open_close))
        bag = ib.Contract(symbol=plan["symbol"], secType="BAG",
                          currency=plan["currency"], exchange="SMART", comboLegs=combo_legs)
        return bag

    def list_orders(self) -> list[OrderResult]:
        """Open AND completed orders.

        `ib.trades()` is SESSION-scoped: it holds what this connection has seen.
        Our cron is one process per run, so an order that filled between cycles is
        simply absent — which the fill finalizers cannot distinguish from "still
        working", stranding the position as immortally 'pending'. Explicitly
        requesting completed orders repopulates that history on a fresh connect.
        Best-effort: if the request fails we still return the live trades."""
        ib = self._require()
        out = []
        try:
            ib.reqCompletedOrders(apiOnly=False)
            ib.sleep(1.0)          # let the completed-order feed arrive
        except Exception:
            pass
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
