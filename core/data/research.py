"""
Research brief — READ-ONLY synthesis over a DataHub.

build_research_brief() gathers price-trend, realized volatility, recent news +
average sentiment, fundamentals, and the next earnings event into a single dict.
It NEVER raises: every hub call may return [] / {} / None, and any malformed
value degrades to a null field rather than an exception.

Read-only. Does NOT import or touch the risk core.
"""

from __future__ import annotations

from typing import Optional

from core.indicators import sma
from core.options_math import realized_vol


def _num(x) -> Optional[float]:
    """Coerce to float, or None if it isn't a finite number."""
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        f = float(x)
        if f != f or f in (float("inf"), float("-inf")):
            return None
        return f
    return None


def _closes(bars) -> list:
    out = []
    for b in bars or []:
        if not isinstance(b, dict):
            continue
        c = _num(b.get("c"))
        if c is not None:
            out.append(c)
    return out


def _price_trend(closes: list) -> dict:
    last = closes[-1] if closes else None
    s20 = sma(closes, 20)
    s50 = sma(closes, 50)
    pct_20d = None
    if len(closes) >= 21:
        prior = closes[-21]
        if prior:
            pct_20d = (closes[-1] - prior) / prior * 100.0
    return {"last": last, "sma20": s20, "sma50": s50, "pct_20d": pct_20d}


def _news(items) -> tuple:
    cleaned = []
    sentiments = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        sent = _num(item.get("sentiment"))
        cleaned.append({
            "headline": item.get("headline"),
            "sentiment": sent,
            "ts": item.get("ts"),
        })
        if sent is not None:
            sentiments.append(sent)
        if len(cleaned) >= 5:
            break
    avg = sum(sentiments) / len(sentiments) if sentiments else None
    return cleaned, avg


def _fundamentals(fund) -> dict:
    f = fund if isinstance(fund, dict) else {}
    sector = f.get("sector")
    return {
        "market_cap": _num(f.get("market_cap")),
        "pe": _num(f.get("pe")),
        "sector": sector if isinstance(sector, str) else None,
        "beta": _num(f.get("beta")),
    }


def _earnings(earn) -> Optional[dict]:
    if not isinstance(earn, dict):
        return None
    date = earn.get("date")
    time = earn.get("time")
    if date is None and time is None:
        return None
    return {"date": date, "time": time}


def build_research_brief(symbol: str, data_hub) -> dict:
    """Synthesize a read-only research brief for `symbol` over `data_hub`.

    Degrades gracefully on any missing/empty/malformed data; never raises.
    """
    def _safe(call, default):
        try:
            return call()
        except Exception:
            return default

    bars = _safe(lambda: data_hub.get_bars(symbol), [])
    news_raw = _safe(lambda: data_hub.get_news(symbol), [])
    fund_raw = _safe(lambda: data_hub.get_fundamentals(symbol), {})
    earn_raw = _safe(lambda: data_hub.get_earnings(symbol), None)

    closes = _closes(bars)
    news_items, avg_sent = _news(news_raw)

    return {
        "symbol": symbol,
        "price_trend": _price_trend(closes),
        "realized_vol_annualized": realized_vol(closes),
        "news": news_items,
        "avg_news_sentiment": avg_sent,
        "fundamentals": _fundamentals(fund_raw),
        "earnings": _earnings(earn_raw),
    }
