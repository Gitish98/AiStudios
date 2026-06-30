"""
Simulation broker — deterministic, no keys, no network.

Lets the whole system run end-to-end on a phone with zero setup. Prices and
option chains are generated deterministically from the symbol + an `asof` date
(so tests are reproducible). Orders "fill" instantly at the limit/mid. This is
PAPER in the strongest sense: nothing leaves the machine.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from typing import Optional

from .. import options_math as om
from .base import (
    Account, BrokerAdapter, OptionContract, OrderRequest, OrderResult, Position, Quote,
)

# Plausible per-symbol anchors so output looks sane (purely cosmetic / deterministic).
_ANCHORS = {
    "SPY": (560.0, 0.14), "QQQ": (490.0, 0.18), "IWM": (220.0, 0.20),
    "DIA": (430.0, 0.13), "XLF": (44.0, 0.17), "XLE": (92.0, 0.24),
    "XLK": (230.0, 0.20), "GLD": (245.0, 0.13), "TLT": (95.0, 0.15),
}


def _seed(*parts: str) -> float:
    h = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF  # in [0, 1)


class SimAdapter(BrokerAdapter):
    name = "sim"

    def __init__(self, asof: Optional[str] = None, equity: float = 30000.0,
                 account_type: str = "margin"):
        self.asof = asof or date.today().isoformat()
        self._equity = equity
        self._account_type = account_type
        self._orders: list[OrderResult] = []
        self._order_seq = 0

    @property
    def is_paper(self) -> bool:
        return True

    # ── account ──────────────────────────────────────────────────────────────
    def get_account(self) -> Account:
        bp = self._equity * (2.0 if self._account_type == "margin" else 1.0)
        return Account(
            equity=self._equity, cash=self._equity, buying_power=bp,
            account_type=self._account_type, is_paper=True, pdt_day_trade_count=0,
        )

    def get_positions(self) -> list[Position]:
        return []  # Phase 0 sim starts flat each run; journal tracks placed orders.

    # ── market data ──────────────────────────────────────────────────────────
    def _spot(self, symbol: str) -> float:
        base, _ = _ANCHORS.get(symbol.upper(), (100.0, 0.22))
        wobble = (_seed(symbol, self.asof) - 0.5) * 0.04  # +/-2% deterministic drift
        return round(base * (1.0 + wobble), 2)

    def _base_iv(self, symbol: str) -> float:
        _, iv = _ANCHORS.get(symbol.upper(), (100.0, 0.22))
        # Deterministic daily IV between ~0.6x and ~1.4x the anchor.
        factor = 0.6 + 0.8 * _seed("iv", symbol, self.asof)
        return round(iv * factor, 4)

    def iv_history(self, symbol: str, days: int = 252) -> list[float]:
        """Deterministic 1y IV history so IV-rank is computable in sim."""
        _, anchor = _ANCHORS.get(symbol.upper(), (100.0, 0.22))
        hist = []
        d0 = date.fromisoformat(self.asof)
        for i in range(days):
            d = (d0 - timedelta(days=i)).isoformat()
            factor = 0.6 + 0.8 * _seed("iv", symbol, d)
            hist.append(round(anchor * factor, 4))
        return hist

    def get_earnings_date(self, symbol: str, asof: Optional[str] = None) -> Optional[str]:
        """Deterministic synthetic next-earnings date: 1–9 days after asof for
        symbols whose seed lands in the 'has upcoming earnings' bucket."""
        u = symbol.upper()
        d0 = date.fromisoformat(asof or self.asof)
        s = _seed("earn", u, self.asof)
        if s > 0.6:  # ~40% of names have earnings in the near window on a given day
            return None
        days_out = 1 + int(s * 14)  # 1..9 days
        return (d0 + timedelta(days=days_out)).isoformat()

    def get_history(self, symbol: str, days: int = 120,
                    asof: Optional[str] = None) -> list[dict]:
        """Deterministic synthetic daily OHLC ending at `asof`, last close ≈ spot."""
        u = symbol.upper()
        base, vol = _ANCHORS.get(u, (100.0, 0.22))
        d0 = date.fromisoformat(asof or self.asof)
        bars = []
        price = base
        for i in range(days, 0, -1):
            d = (d0 - timedelta(days=i)).isoformat()
            r = (_seed("hist", u, d) - 0.5) * vol * 0.18  # deterministic daily return
            price = max(1.0, price * (1.0 + r))
            rng = price * vol * 0.02 * (0.5 + _seed("rng", u, d))
            o = price * (1.0 + (_seed("open", u, d) - 0.5) * 0.005)
            h = max(o, price) + rng / 2
            l = min(o, price) - rng / 2
            bars.append({"date": d, "o": round(o, 2), "h": round(h, 2),
                         "l": round(max(0.5, l), 2), "c": round(price, 2)})
        return bars

    def get_quote(self, symbol: str) -> Quote:
        s = self._spot(symbol)
        half = max(0.01, round(s * 0.0002, 2))  # tight ETF spread
        return Quote(symbol=symbol.upper(), bid=round(s - half, 2),
                     ask=round(s + half, 2), last=s)

    def get_option_chain(self, underlying: str, expiration: Optional[str] = None) -> list[OptionContract]:
        u = underlying.upper()
        spot = self._spot(u)
        iv = self._base_iv(u)
        r = 0.045
        d0 = date.fromisoformat(self.asof)

        # Target ~35 DTE expiry (third-Friday-ish is not needed for sim).
        exp = expiration or (d0 + timedelta(days=35)).isoformat()
        dte = (date.fromisoformat(exp) - d0).days
        t = max(dte, 1) / 365.0

        # Realistic strike ladder: $1 increments for >= $100 underlyings, $0.50
        # below. Wide enough to reach ~0.15-delta puts, so credit spreads are
        # tradeable on a small account.
        step = 1.0 if spot >= 100 else 0.5
        n = 30
        center = round(spot / step) * step
        strikes = [round(center + (k * step), 2) for k in range(-n, n + 1)]

        chain: list[OptionContract] = []
        for K in strikes:
            for ot in ("call", "put"):
                price = om.bs_price(spot, K, t, r, iv, ot)
                g = om.bs_greeks(spot, K, t, r, iv, ot)
                mid = max(0.05, round(price, 2))
                half = max(0.01, round(mid * 0.04, 2))  # 8% wide-ish option spread
                oi = int(500 + 4000 * _seed("oi", u, str(K), ot, self.asof))
                vol = int(50 + 800 * _seed("vol", u, str(K), ot, self.asof))
                occ = f"{u}{exp.replace('-', '')[2:]}{ot[0].upper()}{int(K * 1000):08d}"
                chain.append(OptionContract(
                    symbol=occ, underlying=u, expiration=exp, strike=K, option_type=ot,
                    bid=round(mid - half, 2), ask=round(mid + half, 2), last=mid,
                    implied_vol=iv, delta=round(g["delta"], 4), gamma=round(g["gamma"], 6),
                    theta=round(g["theta"], 4), vega=round(g["vega"], 4),
                    open_interest=oi, volume=vol, dte=dte,
                ))
        return chain

    def mark_option(self, underlying: str, expiration: str, strike: float,
                    right: str, asof: Optional[str] = None) -> float:
        """Deterministic per-share option mark via Black-Scholes from the sim's
        spot + IV. Lets the management pass value open spreads without depending
        on the (spot-centered) strike ladder."""
        u = underlying.upper()
        spot = self._spot(u)
        iv = self._base_iv(u)
        d0 = date.fromisoformat(asof or self.asof)
        dte = max(0, (date.fromisoformat(expiration) - d0).days)
        t = max(dte, 0) / 365.0
        ot = "put" if right.lower().startswith("p") else "call"
        return round(om.bs_price(spot, strike, t, 0.045, iv, ot), 4)

    # ── execution ────────────────────────────────────────────────────────────
    def place_order(self, order: OrderRequest) -> OrderResult:
        self._order_seq += 1
        bid = f"SIM-{self._order_seq:06d}"
        fill = order.limit_price if order.limit_price else 0.0
        res = OrderResult(
            accepted=True, client_order_id=order.client_order_id, broker_order_id=bid,
            status="filled", filled_qty=sum(l.qty for l in order.legs),
            filled_avg_price=fill, reason="sim fill",
            raw={"sim": True, "asof": self.asof},
        )
        self._orders.append(res)
        return res

    def list_orders(self) -> list[OrderResult]:
        return list(self._orders)

    def cancel_order(self, broker_order_id: str) -> bool:
        return True
