"""
US market-calendar guard. Prevents unattended cycles from firing on weekends and
holidays. Uses pandas_market_calendars (NYSE) when installed; otherwise a
RULE-BASED stdlib fallback valid for ANY year.

Why rule-based and not a hardcoded list: the previous fallback enumerated
2026-2027 and silently failed OPEN beyond it — every 2028 weekday, New Year's
Day included, would have counted as a trading day on an unattended VM. But US
market holidays are deterministic (fixed dates with weekend-observation shifts,
nth-weekday rules, and Good Friday via the Easter computus), so the right fix is
to compute them. The old hardcoded sets are kept below purely as a cross-check:
a test asserts the rules reproduce them exactly for both years.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta


def easter(year: int) -> date:
    """Easter Sunday (Gregorian) — the Anonymous Gregorian computus."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    first = d + timedelta(days=(weekday - d.weekday()) % 7)
    return first + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        d = date(year, 12, 31)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    """NYSE weekend observation: Sunday -> Monday after; Saturday -> Friday
    before. (A Saturday New Year's Day is the one exception — simply not
    observed — and is handled at its call site.)"""
    if d.weekday() == 6:
        return d + timedelta(days=1)
    if d.weekday() == 5:
        return d - timedelta(days=1)
    return d


def us_market_holidays(year: int) -> set[date]:
    """Full US equity-market holiday set for any year, from the NYSE rules."""
    out: set[date] = set()
    ny = date(year, 1, 1)
    if ny.weekday() == 6:                       # Sunday -> observed Monday
        out.add(ny + timedelta(days=1))
    elif ny.weekday() != 5:                     # Saturday -> NOT observed (NYSE rule)
        out.add(ny)
    out.add(_nth_weekday(year, 1, 0, 3))        # MLK: 3rd Monday of January
    out.add(_nth_weekday(year, 2, 0, 3))        # Washington: 3rd Monday of February
    out.add(easter(year) - timedelta(days=2))   # Good Friday
    out.add(_last_weekday(year, 5, 0))          # Memorial Day: last Monday of May
    out.add(_observed(date(year, 6, 19)))       # Juneteenth
    out.add(_observed(date(year, 7, 4)))        # Independence Day
    out.add(_nth_weekday(year, 9, 0, 1))        # Labor Day: 1st Monday of September
    out.add(_nth_weekday(year, 11, 3, 4))       # Thanksgiving: 4th Thursday of November
    out.add(_observed(date(year, 12, 25)))      # Christmas
    return out


# The previous hardcoded fallback, kept ONLY as a cross-check fixture: a test
# asserts us_market_holidays() reproduces these exactly. Not consulted at runtime.
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
    return d not in us_market_holidays(d.year)


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
    if d.month == 11 and d == _nth_weekday(d.year, 11, 3, 4) + timedelta(days=1):
        return 13                                # day after Thanksgiving
    if d.month == 12 and d.day == 24:
        return 13
    if d.month == 7 and d.day == 3:
        return 13
    return None


def within_rth(hour: int, minute: int, close_hour: int = 16) -> bool:
    """Is (hour, minute) ET inside regular trading hours [09:30, close)?

    Minutes matter: an hour-only check admits 9:00-9:29 premarket — exactly
    where a stale-quote cycle lands when a UTC-written crontab drifts an hour
    after a DST transition."""
    return (hour, minute) >= (9, 30) and hour < close_hour


def trading_day_reason(d: date | None = None) -> str:
    d = d or datetime.now().date()
    if d.weekday() >= 5:
        return f"{d.isoformat()} is a weekend"
    if d in us_market_holidays(d.year):
        return f"{d.isoformat()} is a US market holiday"
    # Reachable when the authoritative NYSE calendar (pandas_market_calendars)
    # declared the day closed but the rule-based fallback has no rule for it —
    # an ad-hoc/special closure (e.g. a national day of mourning). Say THAT,
    # not the self-contradictory "is a trading day" in a skip message that
    # would invite --force on a genuinely closed market.
    return (f"{d.isoformat()} is closed per the NYSE calendar "
            "(special closure not covered by the fallback rules)")
