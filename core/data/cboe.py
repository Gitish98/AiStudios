"""
Cboe volatility-index history — free, 15+ years, no key.

WHY: our own ATM-IV bootstrap needs 60+ sessions before an IV rank means anything
(core.options_math.MIN_IV_OBSERVATIONS). Cboe publishes daily closes for the
volatility indices of the underlyings behind four of our seven ETFs, going back
decades — so those four can have a real 252-day IV rank today instead of in
months.

THE ONE RULE THIS MODULE EXISTS TO ENFORCE:
    Rank VIX against VIX. NEVER rank our ATM IV against VIX history.
VIX is a variance-swap strip across the whole OTM chain at constant 30-day
maturity; our snapshot is a single nearest-strike IV on one expiry. VIX therefore
runs structurally ~1.5-3 vol points ABOVE ATM IV because of the skew contribution
(measured live 2026-07-23: VIX 18.70 vs our SPY ATM 16.2). Feeding an ATM current
into a VIX min/max produces a permanently depressed rank that keeps the 0.40 gate
shut except in genuine panics — and it looks exactly like "the strategy is being
selective" rather than like a bug. So `proxy_iv_rank` takes the current value from
the SAME series as the history, and callers never supply a current reading.

Honest limitations, surfaced rather than hidden:
  - These are INDEX vols (SPX/NDX/RUT/DJX), not the ETFs' own. A close proxy for
    the same risk, but a proxy; the rank is tagged with its source so nothing
    downstream can mistake it for a measurement of the traded instrument.
  - Only SPY/QQQ/IWM/DIA have a proxy. XLK/XLF/XLE keep the self-bootstrapped
    series, so ranks across the watchlist have MIXED provenance and are not
    strictly comparable. The source tag makes that visible.
  - Cboe offers no SLA and disclaims accuracy. Every failure path here falls back
    to the local bootstrap rather than guessing.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any, Callable, Optional

from core.config import REPO_ROOT

# ETF -> the volatility index of its underlying benchmark.
PROXY_INDEX: dict[str, str] = {"SPY": "VIX", "QQQ": "VXN", "IWM": "RVX", "DIA": "VXD"}

_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{idx}_History.csv"
CACHE_DIR = REPO_ROOT / "data" / "cboe"     # gitignored: do not redistribute Cboe data
RANK_WINDOW = 252                            # 1 trading year, hard
MAX_STALE_DAYS = 7                           # calendar days; see is_stale()


def parse_history_csv(text: str) -> list[tuple[_dt.date, float]]:
    """Parse a Cboe daily-price CSV into [(date, close)] oldest->newest.

    Tolerates the two shipped schemas — `DATE,OPEN,HIGH,LOW,CLOSE` (VIX/VXN/RVX/
    VXD) and the two-column `DATE,VVIX` / `DATE,SKEW` — by preferring a CLOSE
    column and otherwise taking the last field. Dates are US MM/DD/YYYY and are
    parsed with an explicit format: inferring would silently transpose day/month
    for the first twelve of each month. Malformed rows are skipped, never guessed.
    """
    rows: list[tuple[_dt.date, float]] = []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return rows
    header = [h.strip().upper() for h in lines[0].split(",")]
    try:
        vcol = header.index("CLOSE")
    except ValueError:
        vcol = len(header) - 1              # two-column VVIX/SKEW form
    for ln in lines[1:]:
        parts = ln.split(",")
        if len(parts) <= vcol:
            continue
        try:
            d = _dt.datetime.strptime(parts[0].strip(), "%m/%d/%Y").date()
            v = float(parts[vcol])
        except (ValueError, IndexError):
            continue
        if v > 0:
            rows.append((d, v))
    rows.sort(key=lambda r: r[0])
    return rows


def is_stale(rows: list[tuple[_dt.date, float]], today: Optional[_dt.date] = None,
             max_age_days: int = MAX_STALE_DAYS) -> bool:
    """True when the series is too old to act on.

    This is the single biggest operational risk of a free CDN feed: a discontinued
    index still returns HTTP 200 with numbers that are years old, and on an
    unattended VM that is a trade sized off 2022 volatility. HTTP 200 is not a
    freshness signal. The window is generous in calendar days (a long weekend plus
    a holiday is ~4) so normal gaps never false-alarm, while a genuinely dead feed
    is caught by orders of magnitude."""
    if not rows:
        return True
    today = today or _dt.date.today()
    return (today - rows[-1][0]).days > max_age_days


def _cached_path(index: str, cache_dir: Optional[Path] = None) -> Path:
    return (Path(cache_dir) if cache_dir else CACHE_DIR) / f"{index}_History.csv"


def _default_http(url: str) -> str:
    import httpx
    r = httpx.get(url, timeout=30.0, follow_redirects=True)
    r.raise_for_status()
    return r.text


def load_index_closes(index: str, today: Optional[_dt.date] = None,
                      http: Optional[Callable[[str], str]] = None,
                      cache_dir: Optional[Path] = None) -> Optional[list[float]]:
    """Daily closes for a Cboe volatility index, oldest->newest, or None.

    Fetches at most once per calendar day and caches to disk; on any network or
    parse failure it falls back to the cache, and if that is stale too it returns
    None so the caller degrades to the local bootstrap. Fails closed, never guesses.
    """
    today = today or _dt.date.today()
    path = _cached_path(index, cache_dir)
    text: Optional[str] = None

    fresh_cache = False
    if path.exists():
        try:
            mtime = _dt.date.fromtimestamp(path.stat().st_mtime)
            fresh_cache = mtime >= today
        except OSError:
            fresh_cache = False

    if fresh_cache:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = None

    if text is None:
        try:
            text = (http or _default_http)(_URL.format(idx=index))
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            except OSError:
                pass                          # cache is an optimisation, not a requirement
        except Exception:
            if path.exists():                 # network down -> last good copy
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    return None
            else:
                return None

    rows = parse_history_csv(text or "")
    if is_stale(rows, today):
        return None                           # stale feed -> stand aside
    return [v for _, v in rows]


def proxy_iv_rank(underlying: str, today: Optional[_dt.date] = None,
                  http: Optional[Callable[[str], str]] = None,
                  cache_dir: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """A 252-day IV rank for `underlying` from its benchmark volatility index.

    Returns {rank, source, observations, current} or None when unavailable. The
    current value is taken from the SAME series as the history — the caller cannot
    supply one, which is what structurally prevents the ATM-vs-VIX mixing this
    module exists to prevent."""
    from core.options_math import iv_rank

    index = PROXY_INDEX.get((underlying or "").upper())
    if not index:
        return None
    closes = load_index_closes(index, today=today, http=http, cache_dir=cache_dir)
    if not closes:
        return None
    window = closes[-RANK_WINDOW:]            # hard 252: full history is dominated
    current = window[-1]                      # by outliers (e.g. 2020) and flips signals
    rank = iv_rank(current, window)
    if rank is None:
        return None
    return {"rank": rank, "source": f"cboe:{index}",
            "observations": len(window), "current": round(current, 4)}
