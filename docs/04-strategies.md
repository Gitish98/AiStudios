# 04 — Strategy Engines

> **Status:** research / paper-trading spec. Nothing here is financial advice.
> **Scope:** US equities, liquid ETFs, and listed options. No crypto.
> **Architecture:** Claude + MCP/APIs do the *reasoning* (regime read, candidate curation, sanity checks, narrative). A deterministic Python core does the *math* (indicators, signals, sizing, risk). Strategies below are written so that every signal is computable in Python and only the judgment calls are left to the operator/Claude.
> **Default mode:** PAPER. Live trading is a separate, explicit, guarded human action (see `docs/06-execution` once it exists). All sizing below is expressed in fractions of account equity so it ports cleanly between paper and live.

This document specs three strategy engines:

1. **Volatility-Compression Breakout** — find coiled stocks and ride the expansion.
2. **Premium Harvesting** — sell defined, repeatable option premium through normal cycles.
3. **Earnings-Vol Plays** — defined-risk bets around scheduled earnings using implied-vs-realized move.

Each engine specifies: required data inputs (tied to `docs/02-data-sources`), exact signals with formulas / library calls, entry / exit / management rules, position sizing, a worked example, a labeled defaults block, and a hard **when-NOT-to-trade** rule set.

---

## 0. Conventions, shared inputs, and honesty

### 0.1 Sober framing

This is an **edge-research and execution-discipline tool, not a money printer.** Most retail systematic strategies underperform buy-and-hold after costs, slippage, taxes, and behavioral error. The value here is (a) forcing *pre-committed, mechanical* rules, (b) sizing risk consistently, and (c) keeping a paper audit trail. Treat positive paper results as *necessary but not sufficient*. Require an out-of-sample paper period before risking real capital.

### 0.2 Data inputs (from `docs/02-data-sources`)

All three engines draw from these canonical tables/feeds. Names below are the contract the strategy code expects; `docs/02` owns how they are fetched and cached.

| Logical input | Fields | Used by | Cadence |
|---|---|---|---|
| `bars_daily` | `symbol, date, open, high, low, close, adj_close, volume` | all | EOD |
| `bars_intraday` (optional) | `symbol, ts, open, high, low, close, volume` (1–15m) | breakout entry timing | intraday |
| `options_chain` | `symbol, expiry, strike, type, bid, ask, mid, last, volume, open_interest, iv, delta, gamma, theta, vega` | premium, earnings | EOD (snap intraday before entry) |
| `iv_summary` | `symbol, date, iv30, iv_rank_1y, iv_pct_1y, hv20, hv30, term_structure_slope` | premium, earnings | EOD |
| `earnings_calendar` | `symbol, report_date, time_of_day (BMO/AMC), confirmed` | earnings, *avoidance* in all | daily |
| `corp_actions` | `symbol, ex_date, type (div/split), amount` | all (adjust + assignment risk) | as published |
| `universe` | curated liquid symbols (see 0.4) | all | static + weekly review |
| `account_state` | `equity, cash, buying_power, positions[]` | sizing/risk | live |

> If `iv_rank_1y` is not provided by the vendor, compute it in the core (see §0.6). If greeks are not provided, compute Black–Scholes greeks in the core from `iv`, time to expiry, and risk-free rate.

### 0.3 Indicator library

Deterministic core uses **`pandas-ta`** as primary (pure-Python, no system deps — easiest to run from an iPhone-driven Claude Code session) with **TA-Lib** as an optional faster backend. Where both exist, the `pandas-ta` call is given. All custom formulas (BB width percentile, NR7, VCP, implied move, IV rank) are defined explicitly so they can be unit-tested independently of any library.

```python
import pandas as pd
import pandas_ta as ta   # primary
# import talib           # optional fast backend
```

### 0.4 Universe filters (apply before any scan)

A symbol is *tradable* only if, as of the prior close:

- Price `close >= MIN_PRICE` (default $10) — avoids microstructure noise.
- `dollar_volume = close * volume`, 20-day average `>= MIN_ADV_USD` (default $20M).
- For any options strategy: the target expiry has **bid > 0 on both legs**, `open_interest >= MIN_OI` (default 250 per contract), and **relative spread** `(ask - bid) / mid <= MAX_REL_SPREAD` (default 0.10).
- Optionable, US-listed, not a leveraged/inverse ETN unless explicitly whitelisted.

### 0.5 Risk budget (shared)

- `ACCOUNT_RISK_PER_TRADE` default **0.5%** of equity (the dollar amount you are willing to lose if the stop/defined-max-loss is hit).
- `MAX_PORTFOLIO_HEAT` default **5%** of equity across all open *defined-risk* at once.
- `MAX_POSITIONS` default **8** concurrent.
- `MAX_SINGLE_NAME_NOTIONAL` default **15%** of equity (shares) so one gap can't wreck the book.
- No new risk if `account drawdown from high-water mark > MAX_DD_PAUSE` (default 10%) — pause and review.

### 0.6 Shared formula helpers (core)

```python
# True Range / ATR (Wilder)
def atr(df, length=14):
    return ta.atr(df["high"], df["low"], df["close"], length=length)  # Wilder RMA

# Generic rolling percentile rank of the latest value vs trailing window
def pct_rank(series, lookback):
    # fraction of the lookback window the current value sits ABOVE (0..1)
    return series.rolling(lookback).apply(
        lambda w: (w < w[-1]).mean(), raw=True
    )

# IV Rank over 1y of daily IV30 (use if vendor doesn't supply it)
def iv_rank(iv30_series, lookback=252):
    lo = iv30_series.rolling(lookback).min()
    hi = iv30_series.rolling(lookback).max()
    return (iv30_series - lo) / (hi - lo)        # 0..1

# IV Percentile = fraction of days in lookback with IV below today's
def iv_percentile(iv30_series, lookback=252):
    return pct_rank(iv30_series, lookback)
```

---

## 1. Engine A — Volatility-Compression Breakout

**Thesis (crypto→equities translation):** the source playbook hunts "thin volatility that could break a coin out." Equities analog: stocks consolidate (range contracts, volume dries up) and then *expand*. We are buying the **transition from low to high volatility**, not predicting direction blindly — we trade the breakout in the direction it resolves, with a hard ATR stop. Edge is asymmetry: small, defined risk in the squeeze; let the expansion pay.

### 1.1 Required data inputs

- `bars_daily` (≥ 1y history per symbol) — required.
- `bars_intraday` (optional) — only to time the breakout candle; daily-close confirmation also supported.
- `options_chain` + `iv_summary` — *only if* expressing the trade as options (to check IV is cheap, see §1.6).
- `earnings_calendar` — to enforce earnings avoidance (do not be long a naked breakout into earnings unless using defined-risk options).

### 1.2 Compression signals (the SCAN — all computed on `bars_daily`)

Run nightly across the universe. A symbol is a **squeeze candidate** if it passes the gate (§1.2.7) built from these:

**1.2.1 Bollinger Band Width percentile (BBWP)**
```python
bb = ta.bbands(df["close"], length=20, std=2.0)
# columns: BBL_20_2.0, BBM_20_2.0, BBU_20_2.0, BBB_20_2.0, BBP_20_2.0
bbw = bb["BBB_20_2.0"]                 # band width as % of mid (pandas-ta provides this)
bbwp = pct_rank(bbw, lookback=126)     # 0..1 over ~6 months
squeeze_bbwp = bbwp <= 0.10            # width in tightest 10% of 6 months
```

**1.2.2 Keltner-inside-Bollinger (TTM-style squeeze)**
The classic "squeeze on" = Bollinger Bands sit *inside* Keltner Channels (volatility unusually low).
```python
kc = ta.kc(df["high"], df["low"], df["close"], length=20, scalar=1.5)  # KCLe/KCUe
squeeze_on = (bb["BBL_20_2.0"] > kc["KCLe_20_1.5"]) & (bb["BBU_20_2.0"] < kc["KCUe_20_1.5"])
# pandas-ta also exposes ta.squeeze(...) returning SQZ_ON / SQZ_OFF directly:
sqz = ta.squeeze(df["high"], df["low"], df["close"])  # SQZ_ON_20_2.0_20_1.5 == 1 when squeezed
```

**1.2.3 ATR contraction**
```python
atr14 = atr(df, 14)
atr_contraction = atr14 / atr14.rolling(50).mean()    # < 1 means tightening
sig_atr = atr_contraction.iloc[-1] <= 0.85            # ATR ≤ 85% of its 50-day avg
```

**1.2.4 NR7 and inside days**
```python
rng = df["high"] - df["low"]
nr7 = rng == rng.rolling(7).min()                      # narrowest range of last 7
inside = (df["high"] < df["high"].shift(1)) & (df["low"] > df["low"].shift(1))
# count inside days in last 5 sessions as a coil score
inside_count_5 = inside.tail(5).sum()
```

**1.2.5 Volume dry-up**
```python
vol_ratio = df["volume"] / df["volume"].rolling(50).mean()
dry_up = vol_ratio.tail(5).mean() <= 0.70              # recent volume ≤ 70% of 50-day avg
```

**1.2.6 Volatility Contraction Pattern (VCP) — Minervini-style**
Detect a sequence of progressively *shallower* pullbacks on *declining* volume inside an uptrend. Heuristic implementation:
```python
def vcp_score(df, n_contractions=3, max_first_depth=0.25, trend_len=50):
    # 1) must be in an uptrend: close above rising 50DMA, and 50DMA above 200DMA
    sma50, sma200 = ta.sma(df.close,50), ta.sma(df.close,200)
    uptrend = (df.close.iloc[-1] > sma50.iloc[-1] > sma200.iloc[-1])
    # 2) find swing highs/lows -> pullback depths; require each contraction
    #    shallower than the prior and volume lower than the prior
    depths, vols = detect_contractions(df)          # core util (swing detection)
    contracting = all(depths[i] < depths[i-1] for i in range(1, len(depths)))
    quieter     = all(vols[i]  < vols[i-1]  for i in range(1, len(vols)))
    tight       = depths and depths[-1] <= 0.10     # final base ≤ ~10% deep
    ok = uptrend and len(depths) >= n_contractions and contracting and quieter and tight \
         and depths[0] <= max_first_depth
    return ok, depths, vols
```
> VCP is the highest-quality but lowest-frequency signal; treat it as a *quality multiplier*, not a hard gate.

**1.2.7 Gate (default: squeeze + at least 2 confirmations)**
```python
is_squeeze = squeeze_bbwp & squeeze_on.iloc[-1]        # primary definition
confirmations = sum([sig_atr, bool(nr7.iloc[-1]), inside_count_5 >= 2, dry_up])
candidate = is_squeeze and confirmations >= 2
quality   = 1 + (0.5 if vcp_ok else 0)                # used only for ranking/sizing tilt
```
The nightly scan emits a ranked candidate list. **Claude/MCP layer** then curates: removes names with known binary events inside the trade horizon, sector-clusters the list, and writes a one-line thesis per name. No order is placed by the reasoning layer.

### 1.3 Entry trigger (breakout confirmation — direction comes from the market)

Define the *coil box* over the last `BOX_LOOKBACK` days (default 10):
```python
box_high = df["high"].tail(BOX_LOOKBACK).max()
box_low  = df["low"].tail(BOX_LOOKBACK).min()
pivot    = box_high                                   # long-breakout pivot
```
**Long entry** (default bias = long only; shorts optional, see §1.7) fires when **either**:
- **Daily-close confirmation (default):** today's `close > pivot * (1 + BREAK_BUFFER)` (buffer default 0.1%) **and** today's `volume >= VOL_EXPANSION * vol_50` (default 1.5×). Enter next session at open or on a stop-limit at the pivot.
- **Intraday confirmation (optional):** price trades through `pivot + 0.1×ATR` on an intraday bar with the daily volume already pacing ≥ 1.5× by VWAP-time. Use a buy-stop-limit: stop at `pivot + 0.1×ATR`, limit `+ MAX_CHASE` (default 0.5×ATR) to cap slippage.

**No-chase rule:** if price is already `> pivot + ENTRY_MAX_EXT×ATR` (default 1.0) when the signal is seen, **skip** — the move left without you.

### 1.4 Stop (ATR-based, always defined before entry)

```python
init_stop = entry_price - ATR_STOP_MULT * atr14.iloc[-1]   # default mult = 1.5
# alternative structural stop: box_low - 0.25*ATR ; use the TIGHTER of the two if it
# still respects ACCOUNT_RISK_PER_TRADE, else the ATR stop.
```
- The stop is a hard, pre-entered order (or, for options, the defined max loss).
- **Time stop:** if the breakout has not gained `>= 1R` within `TIME_STOP_DAYS` (default 8 sessions), exit — coils that don't expand are dead money and often fail.

### 1.5 Targets and trade management

- **R unit** = `entry_price - init_stop` (per share).
- **T1:** take `SCALE1_FRAC` (default 50%) off at `+1.0R`. Move stop to breakeven.
- **Runner:** trail the remainder with a Chandelier stop:
```python
chandelier = df["high"].rolling(CHAND_LEN).max() - CHAND_MULT * atr14   # len 22, mult 3.0
trail_stop = max(prior_trail, chandelier.iloc[-1])
```
- **Hard target (optional):** measured move = box height projected from the pivot: `pivot + (box_high - box_low)`. Use as a place to tighten, not necessarily exit.

### 1.6 Equities vs options expression

Decide expression *after* the squeeze passes, using `iv_summary`:

| Condition | Expression | Rationale |
|---|---|---|
| IV is **cheap** (`iv_rank_1y <= 0.30`) and breakout pending | **Long call** or **call debit spread** (50–60Δ long leg, 20–30 DTE beyond expected move horizon) | Buy cheap optionality before vol expands; defined risk = premium. |
| IV is **mid/high** (`iv_rank_1y > 0.30`) | **Shares** with ATR stop | Long options bleed when IV is rich and may expand less; stock keeps it clean. |
| Want defined risk but IV not cheap | **Call debit spread** | Caps the IV you pay; defined max loss. |
| Earnings inside horizon | **Defined-risk options only** (debit spread) or **skip** | Never hold a naked shares breakout through a binary you didn't intend to trade. |

Option strike/expiry defaults: long leg delta ≈ 0.55, expiry ≥ `2 × TIME_STOP_DAYS` of calendar time + buffer (default **20–30 DTE**), short leg of debit spread at the measured-move target strike. Max debit per spread sized by §1.8.

### 1.7 Position sizing

**Shares:**
```python
risk_dollars = equity * ACCOUNT_RISK_PER_TRADE * quality   # quality tilt 1.0 or 1.5, capped at cap below
shares = floor(risk_dollars / (entry_price - init_stop))
shares = min(shares, floor(equity * MAX_SINGLE_NAME_NOTIONAL / entry_price))
```
**Options (defined risk):** `risk_dollars = equity * ACCOUNT_RISK_PER_TRADE`; `contracts = floor(risk_dollars / (max_loss_per_contract))` where `max_loss = net_debit * 100`. Never let one spread exceed `ACCOUNT_RISK_PER_TRADE`. Quality tilt caps at `1.5×` and is disabled if portfolio heat would exceed `MAX_PORTFOLIO_HEAT`.

### 1.8 When NOT to trade (hard rules)

- Universe filters fail (illiquid, sub-$10, wide spreads).
- **No squeeze** (gate false) — do not force a breakout on a stock that isn't coiled.
- Price already extended `> 1.0×ATR` past pivot (no-chase).
- Broad-market regime hostile: **SPY below its 50DMA and falling, or VIX > VIX_RISK_OFF** (default 28) — breakout failure rate spikes; either stand down or cut size 50% and longs-only.
- Earnings or known binary inside the trade horizon and you are using *naked shares* — switch to defined-risk options or skip.
- Volume did **not** expand on the break (`< 1.5× avg`) — most failed breakouts.
- The stop required to respect structure is so wide that `risk_dollars` yields `< 1` share/contract — position is uninvestable at your risk budget; skip.

### 1.9 Worked example (Engine A)

*Account equity $50,000. `ACCOUNT_RISK_PER_TRADE` 0.5% → $250 risk.*

Symbol **XYZ**, prior close $48.00. Nightly scan:
- BBWP = 0.06 (tightest 6%), `squeeze_on` True → **is_squeeze True**.
- ATR14 = $1.00; `atr_contraction` 0.80 (≤0.85) ✓; NR7 today ✓; 2 inside days in last 5 ✓; vol last-5 avg 0.65× ✓ → **confirmations = 4 ≥ 2**. VCP not present (`quality = 1.0`).
- Coil box (10d): high $48.50, low $46.80. Pivot = $48.50.

Next session XYZ closes $48.95 (> 48.50×1.001) on volume 1.7× avg → **breakout confirmed**. Not extended (48.95 < 48.50 + 1.0×ATR = 49.50). Enter $49.00 next open.

- `init_stop = 49.00 − 1.5×1.00 = 47.50`. R = $1.50.
- `shares = floor(250 / 1.50) = 166`; notional $8,134 < 15% cap ($7,500)? → 7,500 cap → 153 shares. Use **153 shares** (~$229 risk).
- IV check: `iv_rank_1y` = 0.22 (cheap). Operator *could* instead buy a 55Δ ~25-DTE call debit spread (long 49 / short 52) for ~$1.20 debit; 2 spreads = $240 max loss ≈ risk budget. Either is compliant.
- Management: at $50.50 (+1R) sell 76 shares, stop to BE on the rest; trail runner with Chandelier (22, 3.0). Time stop: if not +1R by day 8, exit.

---

## 2. Engine B — Premium Harvesting

**Thesis (crypto→equities translation):** "collect premiums during cycles" → systematically **sell option premium** where it is statistically rich, on liquid names, with defined or covered risk, and manage mechanically. Edge source: the **volatility risk premium** (implied > realized on average) plus disciplined profit-taking. This is a *grind*, not a lottery; it makes many small wins and must avoid the few large losses.

### 2.1 Required data inputs

- `options_chain` (greeks, OI, bid/ask) — required.
- `iv_summary` (`iv_rank_1y`, `iv_pct_1y`, `hv20`) — required for the rich-vol filter.
- `bars_daily` — trend/context and support/resistance for strike placement.
- `earnings_calendar` — **avoidance** (no new short premium with earnings inside the cycle unless explicitly an earnings trade in Engine C).
- `corp_actions` — dividends/splits drive early-assignment risk on short calls.
- `account_state` — cash/buying-power for CSPs and covered calls.

### 2.2 Instruments and when each applies

| Instrument | Use when | Risk profile |
|---|---|---|
| **Cash-Secured Put (CSP)** | Bullish/neutral on a name you'd happily own; have cash to be assigned | Assignment at strike; "get paid to set a limit buy" |
| **Covered Call (CC)** | Own 100 shares, neutral/mildly bullish; want income / partial exit | Caps upside above strike |
| **Vertical credit spread** (bull put / bear call) | Directional-neutral with **defined** risk, no desire to be assigned shares | Max loss = width − credit |
| **Iron Condor** | Range-bound, high IV, no near-term catalyst | Defined, two-sided |

### 2.3 Entry filters (all must pass)

```python
ENTRY_OK = (
    iv_rank_1y >= IVR_MIN                      # default 0.30 (only sell when vol is paid)
    and rel_spread <= MAX_REL_SPREAD           # 0.10
    and open_interest >= MIN_OI                # 250
    and DTE_MIN <= dte <= DTE_MAX              # 30..45
    and abs(short_delta) within DELTA_BAND     # 0.16..0.30
    and no_earnings_before(expiry)             # earnings avoidance
)
```
- **IV rank gate (`IVR_MIN` 0.30):** never sell cheap premium; ideally `>= 0.40` for condors. There is no edge selling low IV.
- **Delta target = probability proxy:** short strike at **16–30 delta** (default **0.20** for spreads/condors, **0.30** for CSP/CC where assignment is acceptable). 16Δ ≈ 1σ.
- **DTE 30–45:** best theta-vs-gamma tradeoff; avoid < 21 DTE (gamma risk) for new positions.
- **Liquidity:** OI ≥ 250, both legs `bid > 0`, relative spread ≤ 10%, penny-or-nickel increments preferred.
- **Trend context (soft):** place bull puts under support / below rising 20DMA; bear calls above resistance / below falling 20DMA; condors only when 20DMA is flat and ADX low (`ta.adx(...) < 20`).

### 2.4 Strike construction (defaults)

```python
# Bull put spread (neutral-to-bullish)
short_put  = strike_at_delta(chain, target=-0.20, dte~35)
long_put   = short_put - WIDTH                       # WIDTH default 5 (or 1 strike for ETFs)
credit     = mid(short_put) - mid(long_put)
require    credit / WIDTH >= MIN_CREDIT_FRAC          # default 0.33 (collect ≥ 1/3 of width)

# Iron condor: symmetric, both short legs ~0.16Δ, equal widths
# CSP: single short put ~0.30Δ, strike = price you'd accept owning the stock
# Covered call: short call ~0.30Δ above cost basis / above resistance
```
> `MIN_CREDIT_FRAC` rejects spreads that pay too little for the risk (avoids "picking up pennies" too far OTM where one loss erases many wins).

### 2.5 Management rules (mechanical)

- **Profit target:** close at **`PROFIT_TAKE` = 50%** of max credit (condors/verticals). For CSP/CC, 50% of credit *or* roll for more credit. Taking 50% early beats holding to expiry on risk-adjusted return and frees capital.
- **Time exit:** close/roll at **`DTE_EXIT` = 21 DTE** regardless of P/L if not already closed — gamma risk rises sharply into the last weeks.
- **Defensive roll:** if the short strike is **tested** (price reaches short strike, or short delta ≥ `ROLL_DELTA` default 0.30→0.45 for spreads / 0.50 for tested condor side):
  - Roll **out** in time (and the tested side away) **for a net credit only**. Never roll for a debit just to avoid taking a loss.
  - Condors: roll the *untested* side in to recenter and add credit when the tested side is rolled.
- **Hard max loss / stop:** close if loss reaches **`STOP_MULT` × credit received** (default **2×** for verticals/condors → max loss capped well inside the structural max). For CSP, the "stop" is the decision to accept assignment vs roll.
- **Assignment handling:**
  - CSP assigned → you own 100 shares at strike (cost basis = strike − credit). Immediately consider selling a covered call (the "wheel").
  - CC assigned (called away) → shares sold at strike; realize gain; redeploy into a new CSP.
  - Watch **dividend/ex-date early-assignment risk** on ITM short calls: if extrinsic value of the short call < upcoming dividend, expect early assignment — close or roll before ex-date.
- **Earnings:** if an earnings date appears inside an open cycle (calendar updates), reduce/close the position to defined risk before the event unless intentionally running an Engine-C trade.

### 2.6 Position sizing

- **Defined-risk (verticals/condors):** `max_loss = (WIDTH − credit) × 100` per spread; `contracts = floor(equity × ACCOUNT_RISK_PER_TRADE / max_loss)`. Cap total short-premium defined risk at `MAX_PORTFOLIO_HEAT` (5%).
- **CSP:** sized by **cash to secure**: `contracts = floor(allocatable_cash / (strike × 100))`; cap single-name notional at `MAX_SINGLE_NAME_NOTIONAL` (15%). Only on names you genuinely want to own.
- **Covered call:** one contract per 100 shares owned; never sell more calls than shares (no naked calls — *hard rule*).
- Diversify across **uncorrelated underlyings/sectors**; avoid stacking same-direction risk (e.g., five bull puts on tech megacaps = one bet).

### 2.7 When NOT to trade (hard rules)

- **IV rank below `IVR_MIN`** — no premium edge; stand down.
- **Earnings inside the expiry** (and not an Engine-C trade) — skip or wait until after the report.
- Illiquid chain (OI < 250, spread > 10%, no nickel strikes) — fills and exits will eat the edge.
- **Naked calls** — never. Undefined upside risk.
- Selling puts on a name you would **not** want to own at the strike.
- Account already at `MAX_PORTFOLIO_HEAT` or in `MAX_DD_PAUSE` drawdown.
- Credit < `MIN_CREDIT_FRAC` of width — risk/reward too poor.
- Major macro event (FOMC, CPI) inside a few days for **index** condors unless you explicitly want that exposure and have widened wings.

### 2.8 Worked example (Engine B)

*Equity $50,000; risk/trade 0.5% → $250.*

ETF **QQQ**-like underlier at $400. `iv_rank_1y` = 0.55 (rich) ✓. No earnings (ETF). Chain liquid.

Build a **bull put spread**, 35 DTE:
- Short put 20Δ at strike $380 (mid $4.20), long put $375 (mid $2.90). Width 5. **Credit = $1.30**.
- `credit/width = 0.26` — **below `MIN_CREDIT_FRAC` 0.33 → reject**. Move closer: short 25Δ $384 / long $379, credit $1.75 → 0.35 ✓.
- `max_loss = (5 − 1.75) × 100 = $325` per spread. `contracts = floor(250 / 325) = 0` → too rich for one spread at this width. Drop width to **$3** (short $384 / long $381) credit $1.10, `max_loss = (3 − 1.10)×100 = $190`; `contracts = floor(250/190) = 1`. Use **1 spread**, max loss $190.
- Management: GTC buy-to-close at 50% (≈ $0.55 debit). Exit/roll at 21 DTE. If $384 tested, roll out for credit and recenter. Hard stop if loss hits 2× credit ($220 — but capped by $190 max, so effectively hold-to-defined-max only if it gaps).

---

## 3. Engine C — Earnings-Vol Plays

**Thesis (crypto→equities translation):** "bets on big earnings days" → trade the **scheduled volatility event** where pricing is most likely wrong. The market prices an **implied move** into options; sometimes it is too small (buy vol), sometimes too large (sell vol). **IV crush** — the collapse of implied volatility immediately after the report — is the dominant post-event force and the whole reason short-premium earnings trades exist (and why naive long options lose even when direction is right). **Defined risk only. Always.**

### 3.1 Required data inputs

- `earnings_calendar` (`report_date`, `BMO/AMC`, confirmed) — required; trade is keyed off it.
- `options_chain` for the **front expiry that contains the event** and the next one (for calendars) — required (greeks, IV, bid/ask, OI).
- `iv_summary` (`iv30`, `term_structure_slope`) — front-month IV elevation vs back month.
- `bars_daily` — to compute **historical realized earnings moves** (the key comparison).
- `corp_actions` — exclude contaminated history (splits).

### 3.2 Core measurements

**3.2.1 Implied move (front straddle method)**
```python
# ATM straddle of the expiry that first contains the report date
atm_call, atm_put = atm_straddle(chain, expiry_containing_event)
implied_move_pct = (mid(atm_call) + mid(atm_put)) / underlier_price
# refinement (expected move): 0.85 * straddle is a common practical approximation
expected_move_pct = 0.85 * (mid(atm_call)+mid(atm_put)) / underlier_price
```

**3.2.2 Historical realized earnings move**
```python
def hist_earnings_moves(bars, earnings_dates, n=8):
    moves = []
    for d in last_n(earnings_dates, n):                # last ~8 reports (~2y)
        # close before report -> open or close after, depending on BMO/AMC
        pre = close_on_or_before(bars, d_pre)
        post = first_session_after(bars, d)
        moves.append(abs(post/pre - 1))
    return {
        "mean": np.mean(moves), "median": np.median(moves),
        "max": np.max(moves), "moves": moves
    }
```

**3.2.3 Edge ratio**
```python
edge_ratio = implied_move_pct / hist["median"]   # >1 implied rich; <1 implied cheap
```
- `edge_ratio >= RICH_THRESH` (default **1.25**) → **implied may be overpriced** → candidate **short premium**.
- `edge_ratio <= CHEAP_THRESH` (default **0.85**) → **implied may be underpriced** → candidate **long premium**.
- Between thresholds → **no trade** (no edge).

> Honesty note: 8 samples is a tiny, noisy dataset and option market makers see the same history. This filter finds *candidates*, not certainties. Require defined risk and small size; the edge, if any, is thin and only shows up over many trades.

**3.2.4 IV-crush context**
Confirm the event is actually inflating front-month vol: `front_iv / back_iv >= TERM_RICH` (default **1.20**), i.e., inverted term structure. If front isn't elevated vs back, there's little crush to harvest (skip short-vol) and long vol is less likely to be "cheap due to the event."

### 3.3 Trade selection

| Signal | Trade | Structure (defaults) |
|---|---|---|
| Implied **cheap** (`edge_ratio ≤ 0.85`) **and** front-vol not extremely inverted | **Long straddle/strangle**, defined by definition (debit) | ATM straddle or 1σ strangle in the event expiry; size so debit ≤ risk budget |
| Implied **rich** (`edge_ratio ≥ 1.25`) **and** `front/back ≥ 1.20` | **Iron condor** (defined short vol) | Short legs just **outside** the implied move (≈ implied move ÷ price → place shorts at ~1.0–1.2× implied move); wings `WIDTH` beyond; harvest crush |
| Implied **rich** but you want lower path risk | **Short calendar? No** → use **double calendar / call+put calendar** to be long back-month vol, short front-month crush | Same strike front & back; profits from front IV collapsing faster |
| Anything else | **No trade** | — |

**Hard rule: defined risk only.** No naked straddles short, no naked strangles short. Short vol is expressed exclusively as iron condors or calendars (both defined). Long vol is naturally defined (max loss = debit).

### 3.4 Pre vs post timing

- **Short premium / condor:** enter **just before the close on the last session before** the report (BMO → enter prior day's close; AMC → enter that day's close). You want maximum IV inflation at entry and to capture the **overnight crush**. **Exit the morning after** (BMO: at/after the open following the report; AMC: next morning) — do not hold for theta, the crush already happened.
- **Long premium:** also enter the session before, but be aware you are paying the inflated IV. Prefer names where front IV is *not* maximally inverted (you don't want to overpay for the crush you'll suffer). Exit promptly after the move resolves — within the first 1–2 sessions — to limit IV-crush bleed.
- **Calendars:** enter 1–3 days before; the back month retains vol while the front collapses. Exit after the event once front IV has crushed (typically the next day).

### 3.5 Position sizing (fraction of account)

- **Per earnings trade risk = `EARN_RISK_PER_TRADE` default 0.5%** of equity (same as base), and **never more than `EARN_MAX_FRAC` 1.0%** even with conviction.
- Long straddle/strangle: `contracts = floor(equity × EARN_RISK_PER_TRADE / (debit × 100))`. The debit *is* the max loss.
- Iron condor: `contracts = floor(equity × EARN_RISK_PER_TRADE / ((WIDTH − credit) × 100))`.
- **Aggregate earnings exposure** across all simultaneous reports capped at `EARN_PORTFOLIO_CAP` (default 2% of equity) — earnings nights cluster and correlate (sector reactions).
- Because outcomes are binary and fat-tailed, **size small and diversify across many uncorrelated reports** rather than betting big on one.

### 3.6 When to SKIP (hard rule set)

Skip the trade if **any** is true:

- `CHEAP_THRESH < edge_ratio < RICH_THRESH` — no statistical edge.
- Fewer than `MIN_EARN_HISTORY` (default 6) clean historical reports to estimate the realized move.
- Chain illiquid for the event expiry (OI < 250, relative spread > 12% — earnings weeks widen spreads; be strict).
- For short vol: front/back term ratio `< 1.20` (insufficient crush to harvest).
- Report **unconfirmed** or date ambiguous in `earnings_calendar`.
- The defined-risk structure can't be built within `EARN_RISK_PER_TRADE` (e.g., straddle debit alone exceeds budget at 1 contract) — skip, don't upsize.
- Any temptation to use an **undefined-risk** structure — skip; this engine is defined-risk only.
- Macro event overlaps (FOMC same morning) muddying the single-name vol read.
- You cannot watch/exit the morning after (short-vol trades require a prompt exit) — if operating purely from an iPhone with no GTC exit staged, pre-stage the closing order or skip.

### 3.7 Worked example (Engine C)

*Equity $50,000; `EARN_RISK_PER_TRADE` 0.5% → $250.*

Stock **ABC** at $100, reports **AMC tomorrow** (confirmed).
- Event-expiry ATM straddle: call mid $4.80 + put mid $4.70 = $9.50 → **implied_move = 9.5%**.
- Last 8 earnings realized moves: median **6.0%**, max 12%, ≥6 clean samples ✓.
- `edge_ratio = 9.5 / 6.0 = 1.58 ≥ 1.25` → **implied rich** → short-vol candidate.
- Term structure: front IV 78%, back IV 55% → ratio **1.42 ≥ 1.20** ✓ (real crush to harvest).
- Liquidity OK (OI > 1k, spreads ~5%).

Build an **iron condor**, shorts just outside the implied move (±9.5% ≈ $90.5 / $109.5 → round to liquid strikes $90 put / $110 call), wings `WIDTH = 5` ($85 put / $115 call):
- Credit ≈ $1.60. `max_loss = (5 − 1.60) × 100 = $340` per condor. `contracts = floor(250 / 340) = 0` → too big at width 5. Narrow wings to `WIDTH = 3`: short $90/$110, long $87/$113, credit ≈ $1.05, `max_loss = (3 − 1.05) × 100 = $195`; `contracts = floor(250/195) = 1`. Use **1 condor**, max loss $195.
- Timing: enter near today's close (AMC report tonight). **Exit tomorrow morning** after IV crush, target close at 50–70% of credit. Do not hold for theta.
- Skip-check: had `edge_ratio` been 1.05, or fewer than 6 history points, or front/back < 1.20 → **no trade**.

---

## 4. Consolidated DEFAULTS block (config-ready)

> Put this in `config/strategies.yaml` (or `defaults.py`). Every magic number above references a key here. Operator overrides per-symbol or per-regime; the reasoning layer may *propose* changes but only the human commits config edits.

```yaml
# ---------- shared / risk ----------
shared:
  min_price_usd: 10
  min_adv_usd: 20_000_000
  min_oi: 250
  max_rel_spread: 0.10
  account_risk_per_trade: 0.005     # 0.5% of equity at risk per trade
  max_portfolio_heat: 0.05          # 5% total defined risk open
  max_positions: 8
  max_single_name_notional: 0.15    # 15% of equity in one name (shares)
  max_dd_pause: 0.10                # pause new risk past 10% drawdown
  vix_risk_off: 28
  paper_mode: true                  # LIVE requires explicit, guarded human flip

# ---------- A: volatility-compression breakout ----------
breakout:
  bb_length: 20
  bb_std: 2.0
  bbwp_lookback: 126
  bbwp_threshold: 0.10              # tightest 10% of ~6 months
  kc_length: 20
  kc_scalar: 1.5
  atr_length: 14
  atr_contraction_max: 0.85         # ATR <= 85% of 50d avg
  inside_days_min_5: 2
  volume_dryup_max: 0.70            # recent vol <= 70% of 50d avg
  confirmations_required: 2
  box_lookback: 10
  break_buffer: 0.001               # 0.1% above pivot
  vol_expansion: 1.5                # breakout volume >= 1.5x 50d avg
  entry_max_ext_atr: 1.0            # no-chase if > 1 ATR past pivot
  atr_stop_mult: 1.5
  time_stop_days: 8
  scale1_frac: 0.50                 # take 50% at +1R
  chand_len: 22
  chand_mult: 3.0
  options_iv_rank_cheap_max: 0.30   # buy calls/debit spreads only if IVR <= 0.30
  option_long_delta: 0.55
  option_dte_min: 20
  option_dte_max: 30

# ---------- B: premium harvesting ----------
premium:
  ivr_min: 0.30                     # only sell when vol is paid
  ivr_min_condor: 0.40
  delta_band: [0.16, 0.30]
  delta_default_spread: 0.20
  delta_default_csp_cc: 0.30
  dte_min: 30
  dte_max: 45
  width_default: 5                  # 1 strike for ETFs
  min_credit_frac: 0.33             # collect >= 1/3 of width
  profit_take: 0.50                 # close at 50% of max credit
  dte_exit: 21                      # close/roll at 21 DTE
  roll_delta: 0.30                  # short delta that triggers defensive roll review
  stop_mult: 2.0                    # close at 2x credit loss (verticals/condors)
  adx_max_for_condor: 20
  no_earnings_in_cycle: true
  no_naked_calls: true              # hard

# ---------- C: earnings-vol plays ----------
earnings:
  rich_thresh: 1.25                 # implied/realized -> short vol candidate
  cheap_thresh: 0.85                # implied/realized -> long vol candidate
  term_rich: 1.20                   # front_iv / back_iv for crush
  min_earn_history: 6               # clean prior reports required
  hist_lookback_reports: 8
  expected_move_factor: 0.85        # 0.85 * straddle approximation
  earn_risk_per_trade: 0.005
  earn_max_frac: 0.01
  earn_portfolio_cap: 0.02
  max_rel_spread_earnings: 0.12
  defined_risk_only: true           # hard: no naked short vol
  short_vol_exit: "morning_after"   # exit promptly post-crush
```

---

## 5. How the layers cooperate (operator workflow)

1. **Nightly (Python core):** pull `docs/02` data → run all three scans → emit ranked candidate JSON (signals, computed sizing, defined risk, proposed structures). Pure functions, unit-tested, no network in the math path.
2. **Review (Claude + MCP):** Claude reads the candidate list, cross-checks earnings/macro calendars via MCP, flags correlation/sector clustering, writes a plain-English thesis and a *go / no-go / resize* recommendation per candidate. **It does not place orders.**
3. **Decide (human, iPhone):** operator approves a subset in Claude Code. In **paper mode** (default) the core logs simulated fills. Going **live** requires flipping `paper_mode` in config plus an explicit confirmation step — a deliberate, guarded human action, never automatic.
4. **Manage (Python core, scheduled):** mechanical rules from §1.5 / §2.5 / §3.4 generate exit/roll/stop actions; Claude summarizes; human confirms anything live.
5. **Journal:** every trade (paper or live) logs entry rationale, signal snapshot, defaults version, and outcome for honest, out-of-sample evaluation.

## 6. Validation before trusting any engine

- **Unit tests** for every formula in §0.6, §1.2, §3.2 against hand-computed fixtures.
- **Walk-forward / out-of-sample paper period** (default ≥ 60 trading days) before any live capital.
- Track per-engine **expectancy** (`avg_win × win_rate − avg_loss × loss_rate`), max drawdown, and slippage-vs-model. If an engine's *paper* expectancy isn't positive after costs, it does not graduate to live — full stop.

---

### Cross-references
- `docs/02-data-sources` — feeds, schemas, caching, and the field contracts referenced in each "Required data inputs" section.
- `docs/03-indicator-and-risk-core` — the deterministic Python implementations of helpers, indicators, sizing, and the risk gate.
- `docs/06-execution` (planned) — paper vs live, order routing, and the guarded live-trading switch.

*Reminder: research tool, not investment advice. Most retail systematic strategies underperform. Size small, paper first, be honest with the journal.*
