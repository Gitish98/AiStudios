"""
Tests for the Cboe volatility-index history feed. Fully OFFLINE — the http
transport is injected, so no test touches the network.

These lock the specific traps the adversarial review raised, each of which fails
silently rather than loudly in production:
  - a stale-but-HTTP-200 feed (a discontinued index serves years-old numbers)
  - MM/DD/YYYY parsed explicitly (inference transposes days 1-12)
  - both shipped schemas (5-column OHLC and 2-column VVIX/SKEW)
  - the 252-day window (full history is dominated by outliers)
  - never mixing our ATM IV with VIX history
"""

from __future__ import annotations

import datetime as dt
import tempfile
from pathlib import Path

from core.data import cboe


def _csv(rows, header="DATE,OPEN,HIGH,LOW,CLOSE"):
    out = [header]
    for d, v in rows:
        out.append(f"{d},{v},{v},{v},{v}" if header.count(",") == 4 else f"{d},{v}")
    return "\n".join(out) + "\n"


def _series(n, start=dt.date(2026, 1, 2), val=lambda i: 15.0 + i * 0.01):
    """n business-ish daily rows ending today-ish, MM/DD/YYYY formatted."""
    rows, d = [], start
    for i in range(n):
        rows.append((d.strftime("%m/%d/%Y"), round(val(i), 2)))
        d += dt.timedelta(days=1)
    return rows


def test_parses_ohlc_schema_and_us_dates():
    text = _csv([("07/23/2026", 18.70), ("07/22/2026", 17.05)])
    rows = cboe.parse_history_csv(text)
    assert [r[0] for r in rows] == [dt.date(2026, 7, 22), dt.date(2026, 7, 23)]  # sorted
    assert rows[-1][1] == 18.70
    # 03/04/2026 is 4 March, not 3 April — explicit format, no inference.
    assert cboe.parse_history_csv(_csv([("03/04/2026", 20.0)]))[0][0] == dt.date(2026, 3, 4)


def test_parses_two_column_vvix_skew_schema():
    rows = cboe.parse_history_csv(_csv([("07/23/2026", 95.5)], header="DATE,VVIX"))
    assert rows == [(dt.date(2026, 7, 23), 95.5)]


def test_malformed_rows_are_skipped_not_guessed():
    text = "DATE,OPEN,HIGH,LOW,CLOSE\n07/23/2026,1,1,1,18.7\ngarbage\n,,,,\n07/24/2026,1,1,1,x\n"
    assert cboe.parse_history_csv(text) == [(dt.date(2026, 7, 23), 18.7)]


def test_stale_feed_is_rejected():
    # THE operational trap: a dead index still returns HTTP 200 with old numbers.
    today = dt.date(2026, 7, 23)
    fresh = cboe.parse_history_csv(_csv([("07/22/2026", 18.0)]))
    old = cboe.parse_history_csv(_csv([("01/05/2022", 18.0)]))
    assert cboe.is_stale(fresh, today) is False
    assert cboe.is_stale(old, today) is True       # 4 years stale
    assert cboe.is_stale([], today) is True        # empty == unusable
    # A long weekend must NOT false-alarm.
    weekend = cboe.parse_history_csv(_csv([("07/17/2026", 18.0)]))
    assert cboe.is_stale(weekend, dt.date(2026, 7, 21)) is False


def test_stale_feed_yields_no_rank_rather_than_a_wrong_one():
    today = dt.date(2026, 7, 23)
    stale = _csv([(d, v) for d, v in _series(300, start=dt.date(2021, 1, 1))])
    with tempfile.TemporaryDirectory() as td:
        got = cboe.proxy_iv_rank("SPY", today=today, http=lambda url: stale,
                                 cache_dir=Path(td))
    assert got is None, "a stale feed must stand aside, not rank on 2021 vol"


def test_proxy_rank_uses_252_window_and_tags_provenance():
    today = dt.date(2026, 7, 23)
    # 400 rising sessions ending today: within the last 252 the final value is the
    # max, so rank == 1.0; the extra 148 older rows must be excluded by the window.
    n = 400
    start = today - dt.timedelta(days=n - 1)
    text = _csv(_series(n, start=start))
    with tempfile.TemporaryDirectory() as td:
        got = cboe.proxy_iv_rank("SPY", today=today, http=lambda url: text,
                                 cache_dir=Path(td))
    assert got is not None
    assert got["observations"] == cboe.RANK_WINDOW == 252
    assert got["source"] == "cboe:VIX"
    assert got["rank"] == 1.0


def test_only_mapped_underlyings_get_a_proxy():
    # XLK/XLF/XLE have no benchmark vol index — they must keep the local bootstrap.
    assert cboe.proxy_iv_rank("XLF", today=dt.date(2026, 7, 23),
                              http=lambda url: "") is None
    assert set(cboe.PROXY_INDEX) == {"SPY", "QQQ", "IWM", "DIA"}


def test_network_failure_falls_back_to_cache_then_gives_up():
    today = dt.date(2026, 7, 23)
    start = today - dt.timedelta(days=299)
    good = _csv(_series(300, start=start))

    def boom(url):
        raise RuntimeError("network down")

    with tempfile.TemporaryDirectory() as td:
        cache = Path(td)
        # No cache + no network -> None (never a fabricated rank).
        assert cboe.proxy_iv_rank("SPY", today=today, http=boom, cache_dir=cache) is None
        # Seed the cache with a good fetch, then break the network: still works.
        assert cboe.proxy_iv_rank("SPY", today=today, http=lambda u: good,
                                  cache_dir=cache) is not None
        assert cboe.proxy_iv_rank("SPY", today=today, http=boom, cache_dir=cache) is not None


def test_current_value_comes_from_the_index_never_from_our_atm_iv():
    """The core invariant: VIX is ranked against VIX. There is deliberately no
    parameter through which a caller could inject our ATM IV as the current
    reading — VIX runs ~1.5-3 vol points above ATM because of skew, so mixing
    them would depress every rank permanently."""
    import inspect
    params = set(inspect.signature(cboe.proxy_iv_rank).parameters)
    assert "current" not in params and "atm_iv" not in params and "iv" not in params

    today = dt.date(2026, 7, 23)
    start = today - dt.timedelta(days=299)
    text = _csv(_series(300, start=start, val=lambda i: 15.0 + i * 0.01))
    with tempfile.TemporaryDirectory() as td:
        got = cboe.proxy_iv_rank("SPY", today=today, http=lambda u: text,
                                 cache_dir=Path(td))
    # The reported current is the index's own last close, in index points (~18),
    # not a decimal ATM vol (~0.16).
    assert got["current"] > 1.0


def test_strategy_prefers_external_rank_and_tags_provenance():
    """A supplied benchmark rank must override the (untrustworthy) short local
    history, and the signal must record WHICH series produced it — a book mixing
    cboe-ranked and bootstrap-ranked symbols is not comparable across symbols."""
    from strategies.premium_harvest import PremiumHarvest, _resolve_iv_rank
    from strategies.base import StrategyContext

    short_hist = [0.12, 0.13, 0.14]          # far below the 60-observation floor

    ctx_local = StrategyContext(underlying="SPY", spot=100.0, option_chain=[],
                                iv_history=short_hist)
    rank, src, n = _resolve_iv_rank(ctx=ctx_local, atm_iv=0.14,
                                    min_obs=PremiumHarvest({}).min_iv_observations)
    assert rank is None and src == "bootstrap"   # correctly stands aside

    ctx_cboe = StrategyContext(underlying="SPY", spot=100.0, option_chain=[],
                               iv_history=short_hist, iv_rank_value=0.297,
                               iv_rank_source="cboe:VIX", iv_rank_observations=252)
    rank, src, n = _resolve_iv_rank(ctx=ctx_cboe, atm_iv=0.14, min_obs=60)
    assert rank == 0.297 and src == "cboe:VIX" and n == 252


def test_external_rank_below_threshold_still_blocks_the_trade():
    """Provenance does not bypass the gate: a real 29.7% rank is still < 0.40."""
    from strategies.premium_harvest import PremiumHarvest
    from strategies.base import StrategyContext
    ctx = StrategyContext(underlying="SPY", spot=100.0, option_chain=[],
                          iv_rank_value=0.297, iv_rank_source="cboe:VIX",
                          iv_rank_observations=252)
    assert PremiumHarvest({}).generate(ctx) == []
