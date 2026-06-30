# 02 — Data Sources

> **Scope:** US equities, ETFs, and **options**. Not crypto.
> **Architecture:** Claude + MCP/REST is the *reasoning layer*; a deterministic Python core (pandas-ta, risk, execution) does the math and the trading. Data sources feed both.
> **Default mode:** PAPER. Live trading is a separate, explicit, guarded human action (see `04-execution.md`).
> **Honesty clause:** This is a research/edge tool, not a money printer. Most retail algos underperform after costs and slippage. Nothing here is financial advice. Free tiers are *delayed, throttled, and incomplete* — treat them as research-grade, not production-grade.

---

## 0. How to read this doc

Every data source falls into one of two integration shapes. Be precise about which, because it changes how Claude touches it:

| Shape | What it means | When we use it |
|---|---|---|
| **MCP server** | A real Model Context Protocol server exists (official or community). Claude calls typed tools directly; we register it in the MCP client config. | Preferred when an official, maintained server exists. |
| **Our tool over REST** | No good MCP server. We write a thin Python wrapper (a single tool/function) around the provider's REST API and expose *that* to Claude. The deterministic core also calls the same REST client. | Default for anything without a trustworthy MCP server, and for anything latency/cost-sensitive. |

Rule of thumb: **don't adopt a random community MCP server for anything that touches money or keys.** A 40-line REST wrapper we control is safer than an unaudited third-party server holding our broker credentials. Official servers (Alpaca, Polygon, Unusual Whales, Perplexity, Tavily, Exa) are fine. Community servers are fine for read-only public data (e.g. SEC EDGAR).

---

## 1. The crypto-MCP playbook, translated to equities/options

The source tweet is a "10 crypto MCPs" stack. Below, each crypto idea is mapped to its **best equities/ETF/options analog**, with named real providers, whether an MCP server exists *today* vs. a REST wrapper we build, free-tier reality, and what it's actually good for.

> Note on the tweet: the canonical crypto-MCP playbook covers (1) price/market data, (2) DEX/CEX execution, (3) news, (4) social sentiment, (5) technical analysis/screening, (6) on-chain analytics (Dune), (7) web research, (8) charts/alerts, (9) wallet/portfolio, (10) whale/flow tracking. The translation table follows that 10-slot structure. Where a crypto slot has **no clean equities analog** (slot 6, on-chain), that is the most important section of this whole project — see §2.

### 1.1 Translation table

| # | Crypto MCP slot | Data category | Best equities/ETF/options equivalent(s) — real providers | MCP today? | Free tier? | Good for |
|---|---|---|---|---|---|---|
| 1 | Price/market data (CoinGecko, CMC) | OHLCV bars, quotes, trades, snapshots | **Alpaca Market Data**, **Polygon.io**, **Finnhub**, **Twelve Data**, **Tiingo**, **Alpha Vantage**, **FMP** | **Yes** — Alpaca (official), Polygon (official). Others: our REST wrapper | Alpaca: free real-time IEX + 15-min SIP delayed, no daily cap. Polygon: free = EOD/15-min delayed, 5 req/min. Finnhub: 60 req/min free. Twelve Data: 800 req/day, 8 req/min. Tiingo: free EOD + limited intraday. Alpha Vantage: **25 req/day** (effectively unusable). FMP: 250 req/day | Bars for indicators, snapshots for current price, the spine of the whole system |
| 2 | DEX/CEX execution (Jupiter, Uniswap, exchange MCPs) | Order routing, brokerage, account, positions | **Alpaca Trading** (paper + live), **Tradier Brokerage** (paper sandbox + live) | **Yes** — Alpaca official MCP (defaults to paper). Tradier: our REST wrapper | Alpaca paper: free, unlimited. Tradier sandbox: free with account, delayed data | Paper-first order placement, positions, fills. **This is the slot that can lose money — guard it.** |
| 3 | News (crypto news MCPs) | Headlines, full text, tagged tickers | **Benzinga** (news API/newswire), **Finnhub news**, **Tiingo news**, **Alpha Vantage News & Sentiment**, **NewsAPI**, **Polygon news**, **FMP news** | Partial — Finnhub/Polygon/FMP news rides their MCP/REST. Benzinga: our REST wrapper | Benzinga: free "Basic" tier = headline+teaser+link only (full body is paid). Finnhub: company news free (60/min). Tiingo: news is a paid add-on. AV News+Sentiment: free but 25/day. NewsAPI: free 100 req/day, **dev-only, no commercial use**, 24h delay | Catalyst detection, "why did it move", pre-earnings headline scan |
| 4 | Social sentiment (crypto Twitter/Telegram MCPs) | Retail social mention volume + bull/bear | **StockTwits** (frozen API), **ApeWisdom** (Reddit/WSB, keyless), **Swaggy Stocks**, **Tradestie** (WSB top-50 JSON), Reddit API | No official MCP — all our REST wrapper | ApeWisdom: free, keyless, **mention counts only (no sentiment score)**. Tradestie: free, keyless, top-50 WSB w/ sentiment. Swaggy Stocks: free dashboard + limited API. StockTwits: API frozen to new devs | Crowding/squeeze-risk signal, meme-stock awareness. **Treat as a contrarian/risk flag, not alpha.** |
| 5 | Technicals + screeners (TA MCPs, scanners) | Indicators, screens, scans | **pandas-ta / TA-Lib computed locally** (preferred), **Finviz** (screener, scrape/elite API), **TradingView** (screener) | TA: no MCP needed — **compute in our Python core**. Finviz/TradingView: our REST/scrape wrapper | pandas-ta: free, local, deterministic. Finviz: free web screener; Elite (~$40/mo) for export/API. TradingView: free tier, paid for more | **Compute indicators ourselves** for determinism + reproducibility. Use Finviz/TV for discovery/screening only |
| 6 | **On-chain analytics (Dune Analytics)** | — | **NO EQUITIES ANALOG** — the real-equities replacement is **options flow + SEC filings + short interest** | see §2 | see §2 | This is the highest-signal section for equities. **See §2.** |
| 7 | Web research (Perplexity/Exa crypto MCPs) | Open-web research, citations | **Perplexity** (Sonar), **Tavily**, **Exa** | **Yes** — all three have official MCP servers | Perplexity: pay-per-request (cheap models exist, but **bills add up if called every turn**). Tavily: 1,000 free credits/mo. Exa: free trial credits then paid | "What's the bull/bear thesis on XYZ", management changes, lawsuit/recall context, earnings-call gist |
| 8 | Charts/alerts (TradingView crypto) | Charting, visual TA, alerts | **TradingView** (still the answer for equities) | No clean MCP for charts | Free tier with limits; paid for multi-chart/alerts | Human review on iPhone, alert webhooks → our system. Charts are for *you*, not Claude |
| 9 | Wallet/portfolio (crypto wallet MCPs) | Account value, P&L, positions, buying power | **Alpaca account/positions**, **Tradier account** (same providers as slot 2) | **Yes** — Alpaca official MCP | Free (paper) | Portfolio state for risk checks, position sizing, exposure caps |
| 10 | Whale/flow tracking (crypto whale MCPs) | "Smart money" / unusual activity | **Unusual Whales** (options + dark pool + congress), **CBOE** flow, **WhaleWisdom** (13F), **SEC EDGAR** (Form 4 insiders) | **Yes** — Unusual Whales official MCP. EDGAR: community MCP (free). WhaleWisdom/CBOE: our REST wrapper | Unusual Whales: **paid only** ($48–$75+/mo for API/MCP). EDGAR: free, keyless. WhaleWisdom: free tier + paid API. CBOE DataShop: paid per dataset | Unusual options flow, dark-pool prints, insider buys, institutional positioning. Overlaps heavily with §2 |

---

## 2. Options & equities-only data with NO crypto analog (the core edge)

Crypto's "on-chain analytics" (Dune) lets you see *the actual ledger* — every wallet, every transfer. Equities has no public ledger. **The closest equivalent — and the actual edge for this project — is the combination of options flow, regulatory filings, and short interest.** These have no crypto analog and are central. Prioritize this section over everything in §1 except price data.

### 2.1 Option chains + Greeks + IV

You cannot trade options without this. Free options data is scarce and usually delayed.

| Provider | MCP / REST | Free tier reality | Notes |
|---|---|---|---|
| **Tradier** | REST wrapper (we build) | **Sandbox is free** with delayed chains + **Greeks & IV (powered by ORATS)** | Best free starting point. Paper account = free chains with greeks. Live needs funded account |
| **Polygon.io Options** | Official MCP + REST | Free = 15-min delayed, 5 req/min; options chains/aggregates on paid Options plans | Clean, well-documented; real-time options needs a paid Options subscription |
| **Alpaca Options** | Official MCP | Free option chains **with Greeks** on snapshots; OPRA real-time needs paid data plan | Good because it's the same provider as execution |
| **Theta Data** | REST wrapper | Free trial = 30 days EOD only; paid $40–$160/mo | Tick-level history, 1st/2nd/3rd-order greeks, per-contract (not aggregated) |
| **ORATS** | REST wrapper | Paid; trial available | Cleaned, smoothed IV + theoretical values, IV forecasts/slope; gold standard for IV analytics |
| **CBOE DataShop** | REST/file wrapper | Paid per dataset | Authoritative EOD options w/ calculated IV + greeks |

**Recommendation:** Tradier sandbox for free greeks/IV in dev → Polygon or Alpaca paid Options when going to real-time.

### 2.2 IV rank / IV percentile

There is **no free API that just hands you IV rank**. Compute it yourself:

- Pull historical IV (ATM IV / IV30) from Tradier, ORATS, or Theta Data.
- In the Python core, compute **IV Rank = (current IV − 52w low IV) / (52w high IV − 52w low IV)** and **IV Percentile = % of days in trailing year with IV below today's IV**.
- This is deterministic and belongs in our code, not in Claude. (Same philosophy as indicators in §1.5.)

### 2.3 Options flow / unusual activity

| Provider | MCP / REST | Free? | Notes |
|---|---|---|---|
| **Unusual Whales** | **Official MCP** + REST/WS | **Paid** (~$48–$75+/mo for API access) | 100+ endpoints: options flow, dark pool, gamma/greek exposure, congress trades. The most complete single source |
| **CBOE** | REST/file | Paid | Authoritative volume/flow, but not packaged for "unusual" detection |
| **Roll your own** | our tool over Polygon/Tradier | Free-ish | Pull options trades + open interest, flag large-premium sweeps yourself. More work, fewer features, no dark pool |

### 2.4 Earnings calendar + implied move

| Provider | MCP / REST | Free? | Notes |
|---|---|---|---|
| **Finnhub** | REST/MCP | Free (60/min) | Earnings calendar, estimates |
| **FMP** | REST/MCP | Free (250/day) | Earnings calendar, surprises |
| **Nasdaq** | REST wrapper | Free (unofficial endpoints) | Earnings dates, expected move |
| **Market Chameleon** | scrape/paid | Mostly paid | Best for **implied move** + post-earnings stats, IV crush context |

**Implied move** (straddle-implied expected % move into earnings) we compute ourselves from the ATM straddle price ÷ stock price, using chains from §2.1. Deterministic → Python core.

### 2.5 Short interest (FINRA)

- **FINRA** publishes consolidated short interest **twice a month** (settlement on the 15th and last business day), via the **FINRA Developer Center / data files — free, no key**.
- It is **biweekly and lagged** — useful for squeeze-risk context, not a timing signal.
- Daily/borrow-rate/utilization data is **not** free from FINRA — that lives behind paid vendors (Ortex, S3, Fintel). Be honest with yourself about the lag.

### 2.6 13F / institutional positioning

| Provider | MCP / REST | Free? | Notes |
|---|---|---|---|
| **SEC EDGAR** | **Community MCP** (e.g. EdgarTools, secedgar-mcp) + free REST | **Free, no key** | 13F-HR holdings, Form 3/4/5 insider trades, 8-K/10-K/10-Q. Lagged 45 days for 13F |
| **WhaleWisdom** | REST wrapper | Free tier + paid API | Pre-parsed 13F aggregation, "what funds bought/sold", easier than parsing EDGAR XML |

EDGAR is the canonical source and free; WhaleWisdom is the convenience layer. **13F is lagged up to 45 days — it's positioning context, not a trade trigger.**

### 2.7 Dark pool prints

- No free clean source. **Unusual Whales** is the practical option (paid). CBOE/FINRA ATS data is aggregated and lagged. Treat dark-pool "signals" with heavy skepticism — much of it is noise and marketing.

> **Reality check for §2:** The crypto on-chain edge ("I can see the whale's wallet") does **not** exist cleanly in equities. Filings are lagged (13F: 45d, short interest: ~2 weeks), and the genuinely timely flow data (options sweeps, dark pool) is **paid**. The honest version of "on-chain for equities" is: *free, lagged filings* + *paid, timely flow*. Budget accordingly.

---

## 3. Recommended stacks

### 3.1 STARTER stack — free / cheap, paper-friendly, iPhone-operable

Everything here is free or near-free and works in paper mode. This is what we build first.

| Need | Pick | Integration | Cost |
|---|---|---|---|
| Price/bars/snapshots | **Alpaca Market Data** | Official MCP | Free |
| Execution (paper) | **Alpaca Trading (paper)** | Official MCP (defaults to paper) | Free |
| Option chains + greeks + IV | **Tradier sandbox** (ORATS-powered greeks) | Our REST wrapper | Free w/ account |
| News + fundamentals + earnings | **Finnhub** | Our REST wrapper / MCP | Free, 60/min |
| Backup price/EOD | **Twelve Data** or **Tiingo** | Our REST wrapper | Free |
| Indicators / IV rank / implied move | **pandas-ta / TA-Lib + our math** | Python core (no MCP) | Free |
| Social/squeeze risk | **ApeWisdom + Tradestie** | Our REST wrapper | Free, keyless |
| Filings / insiders / 13F | **SEC EDGAR** | Community MCP or REST | Free |
| Short interest | **FINRA data files** | Our REST wrapper | Free |
| Web research | **Perplexity** (sparingly) or **Tavily** (1k free/mo) | Official MCP | Free-ish |
| Charts (human, iPhone) | **TradingView** free | App, not Claude | Free |

**Starter total: ~$0/month**, fully paper, all reviewable from an iPhone via Claude Code. Limits to respect: Alpha Vantage is out (25/day); Finnhub 60/min is the main throttle; options data is delayed in sandbox.

### 3.2 PRO stack — when an edge is validated and you're paying for timeliness

Add these *only after* paper results justify spend. Money spent on data is a cost that your strategy must overcome.

| Need | Upgrade to | Why | Rough cost |
|---|---|---|---|
| Real-time stocks + options | **Polygon.io** (Stocks + Options plans) | Real-time, full chains, clean history | ~$30–$200+/mo per asset class |
| Options flow + dark pool + congress | **Unusual Whales** (Official MCP) | The §2.3/§2.7 edge, packaged | ~$48–$75+/mo |
| IV analytics / theoretical values | **ORATS** or **Theta Data** | Smoothed IV, forecasts, tick greeks | ~$40–$160+/mo |
| Earnings implied move + IV crush | **Market Chameleon** | Best earnings/options stats | ~$50–$100/mo |
| Pro news/newswire | **Benzinga** (full body + speed) | Faster catalysts, full text | Paid (quote-based) |
| Live brokerage | **Alpaca live** and/or **Tradier live** | Real execution (guarded, explicit) | Free API, real capital at risk |
| Deeper screening | **Finviz Elite** | Export/API, intraday screens | ~$40/mo |

---

## 4. What is a real MCP server vs. our own REST wrapper (be explicit)

This is the part people get wrong. As of June 2026:

**Genuinely exist as official, maintained MCP servers (safe to register directly):**
- **Alpaca** — official MCP (trading + market data + options; defaults to paper). ✅
- **Polygon.io** — official MCP (`polygon-io/mcp_polygon`). ✅
- **Unusual Whales** — official MCP (remote + local; options flow, dark pool, congress). ✅
- **Perplexity** — official MCP (`perplexityai/modelcontextprotocol`). ✅
- **Tavily** — official MCP. ✅
- **Exa** — official MCP (most-used search MCP). ✅
- **Financial Modeling Prep (FMP)** — official MCP. ✅

**Exist only as community MCP servers (OK for read-only public data; vet before trusting):**
- **SEC EDGAR** — multiple community servers (EdgarTools, secedgar-mcp). Free, no key, read-only → acceptable. ⚠️
- **Finnhub** — several community MCP servers exist; quality varies. We may prefer our own REST wrapper for control. ⚠️

**No good MCP — we wrap the REST API ourselves as a single Claude tool (and reuse the same client in the Python core):**
- **Tradier** (chains/greeks/IV, sandbox execution)
- **Tiingo**, **Twelve Data**, **Alpha Vantage** (price/news backups)
- **Benzinga** (news)
- **ApeWisdom**, **Tradestie**, **Swaggy Stocks**, **StockTwits** (social)
- **FINRA** (short interest), **WhaleWisdom** (13F), **CBOE** (flow/IV), **ORATS**, **Theta Data**, **Market Chameleon** (options analytics)
- **Finviz**, **TradingView** screeners

**Principle:** anything holding broker credentials or touching orders is either (a) the **official Alpaca MCP** or (b) **our own audited code** — never an unaudited community server.

---

## 5. Free-tier & rate-limit cheat sheet (honest version)

| Provider | Free limit | Catch |
|---|---|---|
| Alpaca Market Data | No daily cap; real-time IEX | Full SIP/OPRA real-time is a paid data plan; free SIP is 15-min delayed |
| Polygon.io | 5 req/min | EOD / 15-min delayed on free; real-time = paid |
| Finnhub | 60 req/min | Some endpoints (incl. premium fundamentals) are paid-only |
| Twelve Data | 800 req/day, 8 req/min | Symbol coverage limited on free |
| Tiingo | Free EOD + limited intraday | News is a paid add-on |
| Alpha Vantage | **25 req/day** | Effectively unusable for anything live — backup only |
| FMP | 250 req/day | Many endpoints gated to paid |
| Benzinga | Headline+teaser+link | Full article body = paid |
| NewsAPI | 100 req/day | **No commercial use on free; 24h delay** |
| ApeWisdom | Keyless, free | Mention counts only — **no sentiment score** |
| Tradestie | Keyless, free | Top-50 WSB only |
| Tradier sandbox | Free w/ account | Delayed data; greeks via ORATS included |
| SEC EDGAR | Free, no key | Fair-use rate limit (~10 req/sec); 13F lagged 45d |
| FINRA short interest | Free files | Biweekly, lagged ~2 weeks |
| Unusual Whales | None (paid) | $48–$75+/mo |
| Perplexity / Tavily / Exa | Pay-per-call / 1k credits / trial | Perplexity bills compound if called every turn |

**Three honest warnings:**
1. **Delayed ≠ free real-time.** Almost every "free" tier is 15-min delayed or EOD. Do not paper-trade strategies that depend on real-time and then expect them to work — backtest with the *same* delay you'll trade on.
2. **Rate limits will bite the agent loop.** Claude calling tools in a loop hits 5 req/min (Polygon) or 25 req/day (AV) fast. Cache aggressively in the Python core; don't let the agent re-fetch.
3. **The signal you actually want (timely options flow, dark pool, daily short borrow) is paid.** Free filings are lagged. Plan around the lag or pay for timeliness — don't pretend the lag isn't there.

---

## 6. Open decisions (for `00-overview.md` / project owner)

- [ ] Confirm starter broker: **Alpaca paper** (recommended — official MCP, free) vs Tradier sandbox vs both.
- [ ] Free options greeks source for dev: **Tradier sandbox** (recommended) until a paid Options plan is justified.
- [ ] Which research MCP to default to: **Tavily** (1k free/mo, cheap) vs Perplexity (better answers, costs add up). Recommend Tavily for routine, Perplexity for deep dives.
- [ ] Decide the §2 budget: stay all-free (lagged filings only) vs add **Unusual Whales** (~$50/mo) for timely flow. Only after paper validates an edge.
- [ ] Secrets handling: all keys via env vars / secrets manager, never committed. (See `05-security.md`.)

---

*Last reviewed: 2026-06-30. Provider tiers and MCP availability change frequently — re-verify free limits and MCP server status before relying on any number here.*
