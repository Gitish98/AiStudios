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


def early_close_et(d: date | None = None) -> int | None:
    """Hour (ET, 24h) of an EARLY market close on `d`, or None for a full day.

    NYSE half days close at 13:00 ET: the day after Thanksgiving, Christmas Eve
    (when it's a trading weekday), and July 3rd (when it's a trading weekday —
    when July 4th falls on a Saturday, July 3rd is instead a FULL holiday and
    the is_trading_day guard already skips it). Without this, the 15:30 ET cron
    runs a full cycle 2.5 hours AFTER the close, on end-of-day-stale quotes —
    the exact conditions that produced the fabricated-mark incident."""
    d = d or datetime.now().date()
    if not is_trading_day(d):
        return None
    # Day after Thanksgiving: Thanksgiving = 4th Thursday of November.
    if d.month == 11:
        first_thu = 1 + (3 - date(d.year, 11, 1).weekday()) % 7
        if d.day == first_thu + 22:              # Thursday + 21 days, +1 for Friday
            return 13
    if d.month == 12 and d.day == 24:
        return 13
    if d.month == 7 and d.day == 3:
        return 13
    return None


def trading_day_reason(d: date | None = None) -> str:
    d = d or datetime.now().date()
    if d.weekday() >= 5:
        return f"{d.isoformat()} is a weekend"
    if d.isoformat() in _FALLBACK_HOLIDAYS:
        return f"{d.isoformat()} is a US market holiday"
    return f"{d.isoformat()} is a trading day"
