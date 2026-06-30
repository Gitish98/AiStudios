"""Isolated Claude reasoning layer.

This package is READ-ONLY and VETO-ONLY. It must NEVER import core/execution,
core/risk, core/store, core/brokers, or cli. It can only annotate candidates.
"""

from agent.advisor import Advisor

__all__ = ["Advisor"]
