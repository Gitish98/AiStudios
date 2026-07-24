"""
Correlation clusters — turn "6 positions" into "how many BETS?"

Position-level caps are blind to the fact that SPY, QQQ, DIA and XLK are largely
the same trade. docs/05 §0 names this as design target #2: "silent drift...
correlation stacks up, and one ordinary market gap wipes a quarter of the
account." A book of six put credit spreads across our watchlist is not six
diversified positions; it is close to one leveraged bet on US large-cap beta.

WHY A STATIC MAP RATHER THAN COMPUTED CORRELATION:
computing rolling pairwise correlation needs a price history we do not reliably
have for every symbol, and — more importantly — measured correlation COLLAPSES
toward 1.0 exactly when it matters (a market-wide gap), so a backward-looking
estimate is most wrong precisely when the cap should bind hardest. For a fixed,
well-understood universe of index and sector ETFs, a hand-drawn map encodes the
structural truth ("these track the S&P") without pretending to a precision we do
not have. It is deliberately conservative: when in doubt, group together.

The map is data, not judgement about the future — extend it when the watchlist
changes. An unknown symbol becomes its own cluster (never silently merged into
someone else's budget) but is flagged so the operator can classify it.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

# cluster id -> members. Broad US large-cap beta is ONE cluster: SPY/QQQ/DIA all
# track heavily-overlapping mega-cap indices, and XLK is ~30% of the S&P by weight
# and moves with QQQ. IWM (small caps) is correlated but has a genuinely distinct
# risk profile, so it is its own cluster. Sector funds with their own drivers
# (energy, financials) are separate.
CLUSTERS: dict[str, set[str]] = {
    "us_large_cap": {"SPY", "QQQ", "DIA", "XLK", "VOO", "IVV", "VTI", "XLC", "XLY"},
    "us_small_cap": {"IWM", "IJR", "VB"},
    "energy":       {"XLE", "XOP", "USO", "VDE"},
    "financials":   {"XLF", "KRE", "VFH"},
    "healthcare":   {"XLV", "IBB", "VHT"},
    "utilities":    {"XLU"},
    "staples":      {"XLP"},
    "industrials":  {"XLI"},
    "materials":    {"XLB"},
    "real_estate":  {"XLRE", "VNQ"},
    "gold":         {"GLD", "IAU", "GDX"},
    "bonds":        {"TLT", "IEF", "AGG", "LQD", "HYG"},
}

_SYMBOL_TO_CLUSTER: dict[str, str] = {
    sym: cid for cid, members in CLUSTERS.items() for sym in members
}


def cluster_of(symbol: str) -> str:
    """Cluster id for a symbol. An unmapped symbol gets its OWN cluster keyed by
    the symbol itself — never folded into another cluster's budget, which would
    silently understate that cluster's risk."""
    s = (symbol or "").upper().strip()
    return _SYMBOL_TO_CLUSTER.get(s, f"unmapped:{s}" if s else "unmapped:?")


def is_mapped(symbol: str) -> bool:
    return (symbol or "").upper().strip() in _SYMBOL_TO_CLUSTER


def cluster_exposure(positions: Iterable[Any], symbol: str) -> float:
    """Summed DEFINED RISK (max_loss) of open positions in `symbol`'s cluster.

    Risk within a cluster is ADDED, never diversified away — that is the entire
    point. Positions carrying no known max_loss contribute 0 (a real broker
    position imported without one), which is a known understatement documented in
    core/risk.py rather than a silent assumption."""
    cid = cluster_of(symbol)
    total = 0.0
    for p in positions or []:
        sym = getattr(p, "underlying", None) or getattr(p, "symbol", "") or ""
        if cluster_of(sym) == cid:
            total += max(0.0, float(getattr(p, "max_loss", 0.0) or 0.0))
    return round(total, 2)


def cluster_breakdown(positions: Iterable[Any]) -> dict[str, float]:
    """cluster id -> summed defined risk. For reporting/dashboards."""
    out: dict[str, float] = {}
    for p in positions or []:
        sym = getattr(p, "underlying", None) or getattr(p, "symbol", "") or ""
        cid = cluster_of(sym)
        out[cid] = round(out.get(cid, 0.0)
                         + max(0.0, float(getattr(p, "max_loss", 0.0) or 0.0)), 2)
    return out


def unmapped_symbols(symbols: Iterable[str]) -> list[str]:
    """Watchlist symbols with no cluster — each trades as its own cluster, so the
    cap still binds, but the operator should classify them deliberately."""
    return sorted({(s or "").upper() for s in symbols if s and not is_mapped(s)})
