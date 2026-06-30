"""
Strategy interface. A strategy is a PURE function of market data -> proposed
Signals. It never places orders, never touches the broker, never sees secrets.
Execution + risk decide what (if anything) happens to a Signal.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Optional

from core.brokers.base import OrderLeg


@dataclass
class Signal:
    strategy: str
    underlying: str
    legs: list[OrderLeg]
    rationale: str
    limit_price: float                 # net price (credit as a positive number for credits)
    est_credit: float = 0.0            # dollars of premium collected (per unit * 100)
    max_loss: float = 0.0              # defined max loss in dollars (>0 => defined risk)
    required_approval_level: int = 1
    is_day_trade: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class StrategyContext:
    """Everything a strategy is allowed to see for one symbol."""
    underlying: str
    spot: float
    option_chain: list  # list[OptionContract]
    iv_history: list[float] = field(default_factory=list)
    closes: list[float] = field(default_factory=list)
    highs: list[float] = field(default_factory=list)
    lows: list[float] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)


class Strategy(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    def generate(self, ctx: StrategyContext) -> list[Signal]:
        ...
