# 11 — Data Providers

> Goal: explain the **data layer** — where market data comes from, the contract
> every source implements, the four providers and their free-tier reality, how
> the DataHub aggregates and falls back, and how to add your own. Still paper.
> Not financial/legal/tax advice.

---

## What the data layer is

The data layer is the **read-only** boundary between AiStudios and the outside
world. It answers questions like "give me 252 daily bars for AAPL," "any recent
news?", "when's the next earnings date?", "what's the market cap?" — and nothing
else. It never places an order, never sizes a position, never touches the risk
gate. (See the honest note at the bottom.)

Everything lives under `core/data/`:

```
core/data/
  base.py            # the DataProvider contract + shared HTTP transport
  providers/
    finnhub.py
    fmp.py
    alphavantage.py
    tiingo.py
```

---

## The DataProvider contract

`core/data/base.py` defines `DataProvider`, an abstract base class. Four design
rules keep providers safe, testable, and swappable:

1. **Pure stdlib HTTP.** Each provider takes an injectable `http` transport
   (default: `urllib_get`). Tests pass a fake transport, so the whole suite runs
   **fully offline** — no network, no keys.
2. **Lazy keys from the environment.** A provider with no key raises
   `DataUnavailable`, never guesses.
3. **Normalized shapes or `NotSupported`.** Every method returns one canonical
   shape, or raises `NotSupported` if that source doesn't offer the data class.
4. **Read-only.** A provider can never place an order or touch risk.

### The four data classes

| Method | Returns |
|---|---|
| `get_bars(symbol, days=252)` | list of `{date, o, h, l, c, v}` |
| `get_news(symbol, limit=20)` | list of `{ts, headline, summary, url, source, sentiment}` |
| `get_earnings(symbol)` | next event `{date, time, eps_estimate}` or `None` |
| `get_fundamentals(symbol)` | `{market_cap, pe, sector, beta, shares_out}` |

Normalized shapes (exact field types) are documented at the top of `base.py`.
Sentiment, when present, is in `[-1, 1]`.

### Errors and capability introspection

- `DataUnavailable` — missing key, network failure, or empty/invalid response.
- `NotSupported` — this source doesn't offer that data class.

A provider declares support implicitly: `supports("news")` returns `True` only
if the subclass **overrode** `get_news`. The aggregator uses this to pick a
source without trial-and-error.

---

## The four providers and their free-tier reality

Each provider reads exactly one env var for its key. With **no key**, the
provider still imports fine and simply raises `DataUnavailable` on use — so you
can run with one provider, all four, or none.

| Provider | Env key | Supports | Free-tier reality |
|---|---|---|---|
| **Finnhub** | `FINNHUB_API_KEY` | bars, news, earnings, fundamentals | Widest free coverage of the four. Generous call rate, but some fundamentals/earnings fields can be thin or US-only on the free tier. |
| **FMP** (Financial Modeling Prep) | `FMP_API_KEY` | bars, fundamentals, earnings | Strong fundamentals + earnings. Free tier is capped at a low daily call count and often limited to US symbols. |
| **Alpha Vantage** | `ALPHAVANTAGE_API_KEY` | bars, news | Free key is **heavily rate-limited** (a few calls/min, low daily cap). It returns HTTP 200 with a `Note`/`Information` body when throttling — the provider maps that to `DataUnavailable`. |
| **Tiingo** | `TIINGO_API_KEY` | bars, news | Solid EOD bars + a news feed. Free tier has modest daily/hourly limits; news access can require a higher tier on some accounts. Uses a token **header** for auth. |

> Reality check: "free tier" means rate-limited, sometimes US-only, and subject
> to change by each vendor. Treat any single free key as best-effort, not
> guaranteed. The aggregator below is what makes this survivable.

---

## How the DataHub aggregates and falls back

The DataHub is the layer that sits **above** the individual providers. Given a
data class and a symbol, it:

1. Walks its ordered list of providers.
2. Skips any that don't `supports()` the requested data class.
3. Skips any without a key.
4. Calls the first eligible provider. On `DataUnavailable` (throttled, down,
   empty), it **falls through** to the next eligible provider.
5. Returns the first good result, or raises `DataUnavailable` if every provider
   is exhausted.

Because providers normalize to identical shapes, the caller never knows or cares
which source answered. Order your provider list by preference (e.g. Finnhub
first for breadth, FMP next for fundamentals) and let cheaper/faster sources win
when they're available.

---

## How to add a new provider

1. Create `core/data/providers/yoursource.py`.
2. Subclass `DataProvider`, set a unique `name`, and pass your `env_key` up:

   ```python
   class YourProvider(DataProvider):
       name = "yoursource"

       def __init__(self, api_key=None, http=None):
           super().__init__(api_key=api_key, http=http, env_key="YOURSOURCE_API_KEY")

       def get_bars(self, symbol, days=252):
           self._require_key()
           data = self._http(self.BASE_URL, {...}, headers={...})
           return [ {"date": ..., "o": ..., "h": ..., "l": ..., "c": ..., "v": ...} for r in data ]
   ```

3. **Only override the data classes you actually support.** Leave the rest
   raising `NotSupported` (the default) — `supports()` handles the rest.
4. Return the **normalized shapes** exactly. Map vendor errors / throttle
   responses to `DataUnavailable`.
5. Route all I/O through `self._http` so your tests run offline with a fake
   transport (see `tests/test_provider_*.py`).
6. Register it in the DataHub's provider list, in your preferred fallback order.

That's it — no caller changes needed.

---

## Honest note: this is read-only and never touches the risk gate

None of the data layer can move money or override safety. Providers cannot place
orders, cannot resize positions, and **cannot touch the risk gate** — it's not a
permission they're trusted with, it's an absence: there is simply no code path
from a `DataProvider` to execution or risk. Bad, stale, or missing data can make
a *strategy* propose a worse trade, but that proposal still has to pass the risk
gate like any other. The data layer informs decisions; it never makes or
enforces them.
