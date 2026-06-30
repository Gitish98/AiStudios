"""
US market-calendar guard. Prevents unattended cycles from firing on weekends and
holidays. Uses pandas_market_calendars (NYSE) when installed; otherwise a stdlib
fallback (weekends + a hardcoded US market-holiday set).

The fallback is deliberately conservative: if it isn't sure, it treats the day as
a NON-trading day (fail closed — better to skip a cycle than run on a closed
market). Override with --force on the CLI.
"""

from __future__ import annotations

from datetime import date, datetime


# Fixed/observed US equity-market holidays (extend yearly). Conservative fallback
# only; pandas_market_calendars is authoritative when available.
_FALLBACK_HOLIDAYS = {
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    # 2027
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}


def _fallback_is_trading_day(d: date) -> bool:
    if d.weekday() >= 5:  # Sat/Sun
        return False
    return d.isoformat() not in _FALLBACK_HOLIDAYS


def is_trading_day(d: date | None = None) -> bool:
    d = d or datetime.now().date()
    try:
        import pandas_market_calendars as mcal  # type: ignore
        nyse = mcal.get_calendar("NYSE")
        sched = nyse.schedule(start_date=d.isoformat(), end_date=d.isoformat())
        return len(sched) > 0
    except ImportError:
        return _fallback_is_trading_day(d)
    except Exception:
        # Any calendar error -> fail closed.
        return _fallback_is_trading_day(d)


def trading_day_reason(d: date | None = None) -> str:
    d = d or datetime.now().date()
    if d.weekday() >= 5:
        return f"{d.isoformat()} is a weekend"
    if d.isoformat() in _FALLBACK_HOLIDAYS:
        return f"{d.isoformat()} is a US market holiday"
    return f"{d.isoformat()} is a trading day"
