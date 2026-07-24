"""
Broker adapter interface — every method is broker-agnostic.

Implementations live in sim.py (no keys, deterministic) and alpaca.py (real
paper API). The rest of the system talks ONLY to this interface, so swapping
brokers never touches strategy or risk code.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Literal, Optional

Side = Literal["buy", "sell"]
OptionType = Literal["call", "put"]


@dataclass
class Account:
    equity: float
    cash: float
    buying_power: float
    account_type: str = "margin"  # cash | margin
    is_paper: bool = True
    pdt_day_trade_count: int = 0   # day trades in the trailing 5 sessions (broker-reported)


@dataclass
class Position:
    symbol: str
    qty: float
    avg_price: float
    market_value: float
    asset_class: str = "us_equity"  # us_equity | option
    underlying: Optional[str] = None
    max_loss: float = 0.0  # defined risk of this position (for portfolio-heat math)
    # Option-leg detail (populated by the broker for option positions; used by
    # reconcile to match our tracked legs against broker truth).
    option_expiration: Optional[str] = None  # YYYY-MM-DD
    option_strike: Optional[float] = None
    option_right: Optional[str] = None        # "P" | "C"


@dataclass
class Quote:
    symbol: str
    bid: float
    ask: float
    last: float

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return round((self.bid + self.ask) / 2, 4)
        return self.last

    @property
    def spread(self) -> float:
        return max(0.0, self.ask - self.bid)


@dataclass
class OptionContract:
    symbol: str            # OCC-style option symbol
    underlying: str
    expiration: str        # YYYY-MM-DD
    strike: float
    option_type: OptionType
    bid: float
    ask: float
    last: float = 0.0
    implied_vol: Optional[float] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None
    open_interest: int = 0
    volume: int = 0
    dte: int = 0
    greeks_modeled: bool = False  # True => greeks are OUR Black-Scholes, not the broker's

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return round((self.bid + self.ask) / 2, 4)
        return self.last

    @property
    def spread_pct(self) -> float:
        m = self.mid
        if m <= 0:
            return 1.0
        return (self.ask - self.bid) / m


@dataclass
class OrderLeg:
    symbol: str
    side: Side
    qty: float
    asset_class: str = "us_equity"  # us_equity | option
    option_type: Optional[OptionType] = None
    strike: Optional[float] = None
    expiration: Optional[str] = None
    intent: str = "open"            # open | close — drives broker position_intent / openClose


@dataclass
class OrderRequest:
    client_order_id: str          # idempotency key — same id never double-places
    legs: list[OrderLeg]
    order_type: str = "limit"     # limit | market
    limit_price: Optional[float] = None
    time_in_force: str = "day"
    strategy: str = ""
    rationale: str = ""
    # Risk metadata the gate needs:
    max_loss: float = 0.0         # defined maximum loss in dollars (>0 means defined-risk)
    est_credit: float = 0.0       # net credit (premium collected), per contract * 100 included
    underlying: str = ""
    required_approval_level: int = 1
    is_day_trade: bool = False    # would this create a same-day round trip?


@dataclass
class OrderResult:
    accepted: bool
    client_order_id: str
    broker_order_id: Optional[str] = None
    status: str = "new"           # new | filled | rejected | canceled
    filled_qty: float = 0.0
    filled_avg_price: float = 0.0
    reason: str = ""
    raw: dict = field(default_factory=dict)


class BrokerAdapter(abc.ABC):
    """Contract every broker must satisfy. Implementations must be honest about
    is_paper — the factory refuses to build a live adapter in Phase 0."""

    name: str = "base"

    @property
    @abc.abstractmethod
    def is_paper(self) -> bool: ...

    @abc.abstractmethod
    def get_account(self) -> Account: ...

    @abc.abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abc.abstractmethod
    def get_quote(self, symbol: str) -> Quote: ...

    @abc.abstractmethod
    def get_option_chain(
        self, underlying: str, expiration: Optional[str] = None
    ) -> list[OptionContract]: ...

    @abc.abstractmethod
    def place_order(self, order: OrderRequest) -> OrderResult: ...

    @abc.abstractmethod
    def list_orders(self) -> list[OrderResult]: ...

    @abc.abstractmethod
    def cancel_order(self, broker_order_id: str) -> bool: ...
