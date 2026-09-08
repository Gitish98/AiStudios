"""ib_noise.py — count IB's delayed-data entitlement notices instead of printing
each one.

WHAT THESE ARE
--------------
Under `reqMarketDataType(3)` (delayed — the documented paper-mode choice, so we
paper-trade against the same delay we would trade live on), IB answers every
option request with:

    Error 10091 ... requires additional subscription ... Delayed market data is
    available. SPY ARCA/TOP/ALL

and each ARCA-listed ETF's stock request with its cousin, 10089. They are not
data failures. Measured live (2026-09-08, read-only probe): under 10091 both
legs of our open SPY spread delivered bid, ask, last, close, sizes AND model
greeks, and `mark_option` returned a bid/ask mid — the same values delayed-
frozen mode returned. The notice names the ONE component we do not subscribe
to (the underlying's home-exchange real-time top-of-book: ARCA for SPY/DIA/IWM/
XLF/XLE, NASDAQ.NMS for QQQ/XLK); the delayed ticks (types 66-75) still land
in `ticker.bid/ask/last/close` because ib_async maps them there.

WHY A FILTER
------------
ib_async classifies these codes as errors (they are outside its warning set),
so each one is a `logger.error` line. One cycle emits ~2,330 of them: 1.1 MB
of log per cycle, 83 MB of cron.log in two months with no rotation, and — the
part that actually bit — a heartbeat body that was 99% this one line, with the
cycle's verdict and the self-audit truncated off the end.

WHAT THIS IS NOT
----------------
Not silence. The FIRST occurrence of each code per cycle passes through
untouched, so the fact "we are on delayed data" is printed every cycle. Every
other IB code passes through untouched — a code we have not classified is
exactly the kind of thing that must stay loud. And the counts are journaled
with `cycle_end`, so a cycle in which the notices VANISH (an entitlement
appeared, or requests stopped happening) is visible in the record.

Standing rule #1 in this repo: silence is a question, not a status. This
module removes repetition, not information.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

# IB codes that mean "delayed data is being served in place of a real-time
# component you have not subscribed to". 10167 is already logged at INFO by
# ib_async and never reached the log; listed for completeness.
DELAYED_DATA_NOTICE_CODES = frozenset({10089, 10090, 10091, 10167})

# ib_async / ib_insync Wrapper.error() formats every non-warning as
#   "Error {code}, reqId {n}: {text}[, contract: ...]"
# The code is at the front and the format has been stable across both
# libraries, so anchoring here cannot match anything else.
_RX = re.compile(r"^Error (\d+), reqId ")

_LOGGER_NAMES = ("ib_async.wrapper", "ib_insync.wrapper")


class DelayedDataNoticeFilter(logging.Filter):
    """Pass the first notice of each code per cycle; count and drop the rest.

    Attached to the IB library's own logger, so it sees the record before any
    handler and before propagation. Everything that is not one of the known
    delayed-data codes is passed through unchanged."""

    def __init__(self) -> None:
        super().__init__(name="ib_delayed_data_notices")
        self.counts: dict[int, int] = {}
        self._passed: set[int] = set()

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:                      # never let a filter break logging
            return True
        m = _RX.match(msg)
        if not m:
            return True
        code = int(m.group(1))
        if code not in DELAYED_DATA_NOTICE_CODES:
            return True                        # unclassified -> stays loud
        self.counts[code] = self.counts.get(code, 0) + 1
        if code in self._passed:
            return False                       # repeat: counted, not printed
        self._passed.add(code)
        return True                            # first of the cycle: printed

    def reset(self) -> None:
        """Start a new cycle's census."""
        self.counts = {}
        self._passed = set()

    def census(self) -> dict[int, int]:
        """{code: occurrences} for the current cycle, sorted by code."""
        return dict(sorted(self.counts.items()))


_FILTER: Optional[DelayedDataNoticeFilter] = None


def install_notice_filter() -> DelayedDataNoticeFilter:
    """Attach the (single, shared) filter to the IB library loggers. Idempotent:
    calling it every cycle attaches nothing new and returns the same object."""
    global _FILTER
    if _FILTER is None:
        _FILTER = DelayedDataNoticeFilter()
    for name in _LOGGER_NAMES:
        lg = logging.getLogger(name)
        if _FILTER not in lg.filters:
            lg.addFilter(_FILTER)
    return _FILTER


def uninstall_notice_filter() -> None:
    """Detach the filter (tests, or an operator who wants the raw firehose)."""
    global _FILTER
    if _FILTER is None:
        return
    for name in _LOGGER_NAMES:
        logging.getLogger(name).removeFilter(_FILTER)
    _FILTER = None


def summary_line(census: dict, market_data_type=None) -> str:
    """One human line for the cycle summary and the heartbeat body.

    Codes and counts only — and the MEANING, which depends on the market-data
    type in force. Under delayed (3/4) these notices are the expected signature
    of running without real-time entitlements. Under real-time (1) the SAME
    codes mean the opposite: the login lacks the entitlement and the component
    was NOT delivered — which on a live account is a quote-quality failure. A
    label that said "expected" there would be a confident wrong word. Three
    independent reviewers raised this; the sixth go-live gate makes it hard to
    reach, and the label still must not lie if it is reached."""
    if not census:
        return "none"
    parts = ", ".join(f"{int(c)} x{int(n)}" for c, n in sorted(census.items(), key=lambda kv: int(kv[0])))
    try:
        md = int(market_data_type) if market_data_type is not None else None
    except (TypeError, ValueError):
        md = None
    if md == 1:
        return (f"{parts} — UNEXPECTED under real-time data (type 1): the login lacks "
                f"entitlements for these components and they were NOT delivered")
    if md in (3, 4):
        return f"{parts} — delayed-data entitlement notices (expected under market_data_type={md})"
    return f"{parts} — delayed-data entitlement notices (market_data_type unknown)"
