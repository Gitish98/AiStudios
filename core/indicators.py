"""
Technical indicators — pure stdlib, deterministic. Phase 0 keeps a small set;
heavier indicator work can later move to pandas-ta (see requirements.txt).
"""

from __future__ import annotations

import math
from typing import Sequence


def sma(values: Sequence[float], n: int) -> float | None:
    vals = list(values)
    if len(vals) < n or n <= 0:
        return None
    return sum(vals[-n:]) / n


def ema(values: Sequence[float], n: int) -> float | None:
    vals = list(values)
    if len(vals) < n or n <= 0:
        return None
    k = 2.0 / (n + 1.0)
    e = sum(vals[:n]) / n
    for v in vals[n:]:
        e = v * k + e * (1.0 - k)
    return e


def true_ranges(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> list[float]:
    trs: list[float] = []
    for i in range(1, len(closes)):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return trs


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], n: int = 14) -> float | None:
    trs = true_ranges(highs, lows, closes)
    if len(trs) < n:
        return None
    return sum(trs[-n:]) / n


def rsi(closes: Sequence[float], n: int = 14) -> float | None:
    cs = list(closes)
    if len(cs) < n + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(len(cs) - n, len(cs)):
        diff = cs[i] - cs[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain, avg_loss = gains / n, losses / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def bollinger_bandwidth(closes: Sequence[float], n: int = 20, k: float = 2.0) -> float | None:
    """(upper - lower) / middle — a squeeze metric. Lower = tighter compression."""
    cs = list(closes)
    if len(cs) < n:
        return None
    window = cs[-n:]
    mid = sum(window) / n
    if mid == 0:
        return None
    var = sum((c - mid) ** 2 for c in window) / n
    sd = math.sqrt(var)
    return (2 * k * sd) / mid


def percentile_rank(value: float, history: Sequence[float]) -> float | None:
    """Fraction of history strictly below `value`, in [0, 1]."""
    vals = [v for v in history if v is not None]
    if not vals:
        return None
    return sum(1 for v in vals if v < value) / len(vals)


# ── volatility-compression / squeeze detection ───────────────────────────────

def nr7(highs: Sequence[float], lows: Sequence[float]) -> bool:
    """True if the latest bar has the narrowest range of the last 7 (NR7)."""
    if len(highs) < 7 or len(lows) < 7:
        return False
    ranges = [highs[i] - lows[i] for i in range(-7, 0)]
    return ranges[-1] <= min(ranges)


def inside_day(highs: Sequence[float], lows: Sequence[float]) -> bool:
    """Latest bar's range fully inside the prior bar's (an inside day)."""
    if len(highs) < 2 or len(lows) < 2:
        return False
    return highs[-1] <= highs[-2] and lows[-1] >= lows[-2]


def bandwidth_percentile(closes: Sequence[float], n: int = 20,
                         lookback: int = 120, k: float = 2.0) -> float | None:
    """Percentile (0..1) of the CURRENT Bollinger bandwidth versus its own recent
    history. Low = compressed (a squeeze)."""
    cs = list(closes)
    if len(cs) < n + 5:
        return None
    series = []
    start = max(n, len(cs) - lookback)
    for end in range(start, len(cs) + 1):
        bw = bollinger_bandwidth(cs[:end], n, k)
        if bw is not None:
            series.append(bw)
    if len(series) < 5:
        return None
    current = series[-1]
    return percentile_rank(current, series[:-1])


def atr_contraction_ratio(highs: Sequence[float], lows: Sequence[float],
                          closes: Sequence[float], fast: int = 7, slow: int = 50) -> float | None:
    """ATR(fast) / ATR(slow). < 1 means recent ranges are contracting vs longer-term."""
    a_fast = atr(highs, lows, closes, fast)
    a_slow = atr(highs, lows, closes, slow)
    if not a_fast or not a_slow or a_slow == 0:
        return None
    return a_fast / a_slow


def range_position(closes: Sequence[float], lookback: int = 20) -> float | None:
    """Where the latest close sits in its recent high-low range, 0 (low) .. 1 (high).
    A directional bias hint for which way a squeeze is likely to resolve."""
    cs = list(closes)
    if len(cs) < lookback:
        return None
    window = cs[-lookback:]
    lo, hi = min(window), max(window)
    if hi - lo < 1e-9:
        return 0.5
    return (cs[-1] - lo) / (hi - lo)
