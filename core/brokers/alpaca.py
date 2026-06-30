"""
Alpaca adapter (PAPER) — thin REST client over stdlib urllib so it imports and
runs without the alpaca-py package or httpx. Real network calls only happen when
a method is invoked AND keys are present.

Paper base URL is hard-defaulted. This class refuses to target the live endpoint
unless explicitly constructed with is_paper=False (the factory forbids that in
Phase 0).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any, Optional

from .base import (
    Account, BrokerAdapter, OptionContract, OrderLeg, OrderRequest, OrderResult,
    Position, Quote,
)

PAPER_TRADING = "https://paper-api.alpaca.markets"
DATA_BASE = "https://data.alpaca.markets"


class AlpacaError(RuntimeError):
    pass


class AlpacaAdapter(BrokerAdapter):
    name = "alpaca_paper"

    def __init__(self, key: Optional[str] = None, secret: Optional[str] = None,
                 paper: bool = True):
        self._key = key or os.environ.get("ALPACA_PAPER_KEY", "")
        self._secret = secret or os.environ.get("ALPACA_PAPER_SECRET", "")
        self._paper = paper
        if not paper:
            raise AlpacaError("Phase 0 forbids live Alpaca. Only paper is permitted.")
        self.trading_base = os.environ.get("ALPACA_PAPER_BASE_URL", PAPER_TRADING)

    @property
    def is_paper(self) -> bool:
        return self._paper

    @property
    def has_keys(self) -> bool:
        return bool(self._key and self._secret)

    # ── HTTP ─────────────────────────────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        if not self.has_keys:
            raise AlpacaError("ALPACA_PAPER_KEY / ALPACA_PAPER_SECRET not set in .env")
        return {
            "APCA-API-KEY-ID": self._key,
            "APCA-API-SECRET-KEY": self._secret,
            "Content-Type": "application/json",
        }

    def _get(self, base: str, path: str, params: Optional[dict] = None) -> Any:
        url = base + path
        if params:
            from urllib.parse import urlencode
            url += "?" + urlencode(params)
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        return self._send(req)

    def _post(self, base: str, path: str, body: dict) -> Any:
        req = urllib.request.Request(
            base + path, data=json.dumps(body).encode(),
            headers=self._headers(), method="POST",
        )
        return self._send(req)

    def _send(self, req: urllib.request.Request) -> Any:
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            raise AlpacaError(f"Alpaca HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise AlpacaError(f"Alpaca network error: {e.reason}") from e

    # ── account / positions ──────────────────────────────────────────────────
    def get_account(self) -> Account:
        a = self._get(self.trading_base, "/v2/account")
        return Account(
            equity=float(a.get("equity", 0)),
            cash=float(a.get("cash", 0)),
            buying_power=float(a.get("buying_power", 0)),
            account_type="margin" if a.get("shorting_enabled") else "cash",
            is_paper=True,
            pdt_day_trade_count=int(a.get("daytrade_count", 0)),
        )

    def get_positions(self) -> list[Position]:
        rows = self._get(self.trading_base, "/v2/positions")
        return [position_from_alpaca_row(p) for p in rows]

    # ── market data ──────────────────────────────────────────────────────────
    def get_quote(self, symbol: str) -> Quote:
        d = self._get(DATA_BASE, f"/v2/stocks/{symbol}/quotes/latest")
        q = d.get("quote", {})
        bid, ask = float(q.get("bp", 0)), float(q.get("ap", 0))
        last = (bid + ask) / 2 if bid and ask else 0.0
        return Quote(symbol=symbol.upper(), bid=bid, ask=ask, last=last)

    def get_option_chain(self, underlying: str, expiration: Optional[str] = None) -> list[OptionContract]:
        params: dict[str, Any] = {"underlying_symbols": underlying.upper(), "limit": 1000}
        if expiration:
            params["expiration_date"] = expiration
        d = self._get(DATA_BASE, "/v1beta1/options/snapshots", params)
        snaps = d.get("snapshots", {}) or {}
        out: list[OptionContract] = []
        today = date.today()
        for occ, s in snaps.items():
            greeks = s.get("greeks", {}) or {}
            q = s.get("latestQuote", {}) or {}
            meta = _parse_occ(occ)
            if not meta:
                continue
            exp, ot, strike = meta
            dte = max(0, (date.fromisoformat(exp) - today).days)
            out.append(OptionContract(
                symbol=occ, underlying=underlying.upper(), expiration=exp,
                strike=strike, option_type=ot,
                bid=float(q.get("bp", 0)), ask=float(q.get("ap", 0)),
                implied_vol=_f(s.get("impliedVolatility")),
                delta=_f(greeks.get("delta")), gamma=_f(greeks.get("gamma")),
                theta=_f(greeks.get("theta")), vega=_f(greeks.get("vega")),
                dte=dte,
            ))
        return out

    # ── execution ────────────────────────────────────────────────────────────
    def place_order(self, order: OrderRequest) -> OrderResult:
        body = build_order_body(order)
        try:
            r = self._post(self.trading_base, "/v2/orders", body)
        except AlpacaError as e:
            return OrderResult(accepted=False, client_order_id=order.client_order_id,
                               status="rejected", reason=str(e))
        return OrderResult(
            accepted=True, client_order_id=order.client_order_id,
            broker_order_id=r.get("id"), status=r.get("status", "new"),
            filled_qty=float(r.get("filled_qty", 0) or 0),
            filled_avg_price=float(r.get("filled_avg_price") or 0), raw=r,
        )

    def list_orders(self) -> list[OrderResult]:
        rows = self._get(self.trading_base, "/v2/orders", {"status": "all", "limit": 50})
        return [
            OrderResult(
                accepted=True, client_order_id=r.get("client_order_id", ""),
                broker_order_id=r.get("id"), status=r.get("status", ""),
                filled_qty=float(r.get("filled_qty", 0) or 0),
                filled_avg_price=float(r.get("filled_avg_price") or 0), raw=r,
            ) for r in rows
        ]

    def cancel_order(self, broker_order_id: str) -> bool:
        req = urllib.request.Request(
            f"{self.trading_base}/v2/orders/{broker_order_id}",
            headers=self._headers(), method="DELETE",
        )
        try:
            self._send(req)
            return True
        except AlpacaError:
            return False


def build_order_body(order: OrderRequest) -> dict:
    """Construct the Alpaca order payload. Pure function (no network) so the
    credit-sign and GCD-ratio conventions are unit-testable."""
    if len(order.legs) == 1:
        leg = order.legs[0]
        body: dict[str, Any] = {
            "symbol": leg.symbol, "qty": str(leg.qty), "side": leg.side,
            "type": order.order_type, "time_in_force": order.time_in_force,
            "client_order_id": order.client_order_id,
        }
        if order.limit_price is not None:   # 0.0 is a valid limit, don't drop it
            body["limit_price"] = str(order.limit_price)
        return body

    # Multi-leg: Alpaca requires GCD-reduced leg ratios — total contracts =
    # parent qty * ratio_qty. So parent qty = gcd(leg qtys), ratio = qty/gcd.
    from functools import reduce
    from math import gcd
    leg_qtys = [max(1, int(round(l.qty))) for l in order.legs]
    g = reduce(gcd, leg_qtys) or 1
    # Sign convention: our codebase carries a credit as a POSITIVE limit_price,
    # but Alpaca mleg expects a NEGATIVE price for a net credit (positive = net
    # debit). Negate when this is a credit order.
    price = None
    if order.limit_price is not None:   # 0.0 is a valid combo limit (worthless close)
        is_credit = order.est_credit > 0
        # guard the 0.0 case so we don't emit "-0.0"
        price = str(-order.limit_price if (is_credit and order.limit_price) else order.limit_price)

    def _intent(l):
        closing = getattr(l, "intent", "open") == "close"
        if l.side == "sell":
            return "sell_to_close" if closing else "sell_to_open"
        return "buy_to_close" if closing else "buy_to_open"

    return {
        "order_class": "mleg", "qty": str(g), "type": order.order_type,
        "time_in_force": order.time_in_force,
        "client_order_id": order.client_order_id,
        "limit_price": price,
        "legs": [
            {"symbol": l.symbol, "ratio_qty": str(int(round(l.qty)) // g),
             "side": l.side, "position_intent": _intent(l)}
            for l in order.legs
        ],
    }


def position_from_alpaca_row(p: dict) -> Position:
    """Map an Alpaca position row to our Position. Critically, OPTION rows must be
    normalized to asset_class='option' with strike/right/expiration populated, or
    reconcile drops them and reports false drift. (Pure function — unit-tested.)"""
    import re
    ac = p.get("asset_class", "us_equity")
    is_opt = ac == "us_option"
    qty = float(p["qty"])
    common = dict(symbol=p["symbol"], qty=qty,
                  avg_price=float(p.get("avg_entry_price", 0) or 0),
                  market_value=float(p.get("market_value", 0) or 0))
    if is_opt:
        meta = _parse_occ(p["symbol"])
        root = re.match(r"^([A-Z]+)\d{6}[CP]\d{8}$", p["symbol"])
        if meta:
            exp, otype, strike = meta
            return Position(
                **common, asset_class="option",
                underlying=(root.group(1) if root else p.get("underlying_symbol")),
                option_expiration=exp, option_strike=strike,
                option_right="C" if otype == "call" else "P")
    return Position(**common, asset_class=ac, underlying=p.get("symbol"))


def _f(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _parse_occ(occ: str) -> Optional[tuple[str, str, float]]:
    """Parse an OCC option symbol like 'SPY240920P00450000' -> (exp, type, strike)."""
    import re
    m = re.match(r"^[A-Z]+(\d{6})([CP])(\d{8})$", occ)
    if not m:
        return None
    yymmdd, cp, strike_milli = m.groups()
    exp = f"20{yymmdd[:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}"
    ot = "call" if cp == "C" else "put"
    return exp, ot, int(strike_milli) / 1000.0
