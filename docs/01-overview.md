# 01 — System Overview (one page)

> **Scope:** US equities, ETFs, and **options**. Not crypto.
> **Default mode:** **PAPER.** Going live is a separate, explicit, multi-step,
> human-only action.
> **Honesty clause:** A research/edge tool, not a money printer. Most retail
> algos underperform a low-cost index fund after costs. Nothing here is financial
> advice. New here? Read [`00-START-HERE.md`](00-START-HERE.md) first.

---

## The one idea

There is a **hard line between judgment and arithmetic**, and neither side ever
does the other's job.

| | **Reasoning layer — Claude + MCP** | **Deterministic core — plain Python** |
|---|---|---|
| Does | Reads messy text, ranks candidates, writes a thesis, reflects post-trade | Computes indicators, sizes positions, **enforces every risk limit**, builds/submits orders, reconciles fills |
| Never does | Computes a dollar amount, approves an order, raises a limit, flips paper→live, clears the kill switch | Makes a subjective call or writes prose |

**Claude is an advisor with no hands.** It has tools to *read data* and *write
notes/rankings* — and **no** tool to place an order, set a risk limit, go live, or
clear the kill switch. Those functions exist only in sealed Python the model
cannot reach. Same inputs → same decisions, every time.

> One bounded exception, stated honestly: the Orchestrator's per-strategy
> *allocation multiplier* and a strategy's *quality tilt* are LLM-influenced
> scalars in `[0, cap]` that do scale dollar size. They are clamped by Python,
> their day-over-day change is capped, the LLM may only *lower* allocation (never
> raise it beyond what deterministic realized performance supports), and the
> portfolio-heat gate binds regardless. See [`05-risk-and-safety.md`](05-risk-and-safety.md) §2.2.

---

## The pipeline (six moves)

```
DATA ─► SIGNALS ─► DECISION ─► RISK GATE ★ ─► EXECUTION ─► REFLECTION
                                  (the only arrow into "place an order"
                                   comes out of the risk gate)
```

1. **DATA** — Python pulls bars, option chains (greeks/IV), news, filings,
   earnings, short interest. Numbers come from APIs, never from Claude reading a
   price out of prose. Claude writes cited, tagged *notes*.
2. **SIGNALS** — Python computes eligibility + a raw score per strategy; Claude
   *ranks* the already-eligible candidates and writes a one-line thesis. No sizes,
   no orders.
3. **DECISION** — Claude allocates a *share of a fixed risk envelope* per strategy
   (clamped, fail-safe). Python sizes the position by formula.
4. **RISK GATE** — `core/risk.py` → `APPROVE | VETO(reason)`. Deterministic,
   mandatory, LLM-free, **fail-closed**. A VETO is final for the cycle.
5. **EXECUTION** — one broker-agnostic adapter, **paper by default**, idempotent
   `client_order_id`, limit orders only, partial-fill aware. The broker is the
   source of truth on every reconcile.
6. **REFLECTION** — Claude reasons over Python-computed facts; lessons *bias*
   tomorrow's allocation within caps. They can never relax a limit.

---

## The crypto→equities translation (why this isn't a crypto bot)

| Crypto idea | Equities/options analog | Consequence |
|---|---|---|
| 24/7 loop | Market hours + ~9 holidays | A market-calendar guard gates the loop; most of the day it sleeps. |
| On-chain ledger (Dune) | **No public ledger.** Honest analog = options flow + SEC filings + short interest: *free but lagged*, or *timely but paid*. | The crypto "see the whale's wallet" edge does **not** cleanly exist here. |
| DEX/CEX execution | Broker adapter (Alpaca paper default) | Thin, swappable Python, pointed at paper. |
| Degen leverage | **Defined-risk options only** | Hard caps in config, not in a prompt. Gross leverage capped on equity. |

---

## The three strategies (all defined-risk where options are involved)

1. **Volatility-Compression Breakout** — find coiled stocks (squeeze, ATR
   contraction, volume dry-up), ride the expansion with a hard ATR stop.
2. **Premium Harvesting** — sell *statistically rich* option premium (IV rank ≥
   0.30) as defined/covered risk, managed mechanically. Never naked calls.
3. **Earnings-Vol Plays** — defined-risk bets around earnings, comparing the
   implied move to the historical realized move, harvesting IV crush.

A strategy earns real capital only after an out-of-sample paper period
(≥60 trading days **and** ≥40 closed trades) with positive after-cost expectancy
that survives a worst-case slippage haircut. Most should never graduate.

---

## What protects you (the guardrails, in one breath)

Per-trade ≤ 0.5% risk · per-position ≤ 5% · portfolio heat ≤ 6% · **gross
leverage ≤ 1.0× equity** · options BP ≤ 30% · **defined-risk only (un-overridable)**
· **separate overnight/gap caps** (the −3%/day kill switch is *intraday only* and
cannot bound an overnight gap) · daily-loss intraday kill switch · stale/crossed/
wide-quote breakers · PDT counter (margin <$25k) **and a settled-cash/GFV check
(cash accounts)** · fat-finger bounds · idempotent orders · broker-truth reconcile
· one-tap kill switch the LLM cannot clear.

All limits are config the **human** owns. The model reads them to behave sensibly;
it never writes them.

---

## The stack, briefly

Python 3.11+ core (pandas, **pandas-ta** — no TA-Lib/system deps, so it installs
from a phone). Claude Agent SDK + MCP for reasoning. SQLite state (one file) →
optional Supabase. One CLI entrypoint (`cli.py`). `status` prints to the chat;
an optional single-file HTML dashboard exists. Cron + market-hours guard *where a
host is available* — phone-only operators use manual trigger or a managed
scheduled task.

---

## Current status

Design-complete; implementation staged (see [`07-roadmap.md`](07-roadmap.md)).
Build order is **deterministic-core-first**: the risk gate, sizing, the adapter,
idempotency, and reconcile are written and tested to the point of boredom *before*
the Claude layer is added on top. The repo is mid-transition from its previous
life (a screenplay toolkit) to the trading system described here.

---

*Last reviewed: 2026-06-30. Verify current broker, options-approval, PDT,
wash-sale, and data-provider rules before risking real capital. Default is paper.*
