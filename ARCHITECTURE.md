# ARCHITECTURE — AiStudios Trading System

> **The master document.** Read this first. It ties every other doc into one
> narrative: how a price bar becomes a signal, a signal becomes a sized
> candidate, a candidate survives a deterministic risk gate, an approved order
> reaches a broker, and a fill teaches the system something for tomorrow.
>
> **Scope:** US equities, ETFs, and **options**. Not crypto.
> **Default mode:** **PAPER.** Going live is a separate, explicit, multi-step, human-only action.
> **Honesty clause:** This is an edge-research and risk-discipline tool, **not a money printer.** Most retail algorithms underperform a low-cost index fund after fees, slippage, taxes, and behavioral error. Nothing here is financial, legal, or tax advice.

**Companion docs (the detail lives here; this doc is the spine):**
- [`docs/02-data-sources.md`](docs/02-data-sources.md) — providers, MCP-vs-REST, free-tier reality.
- [`docs/03-agent-architecture.md`](docs/03-agent-architecture.md) — the agent roster and the judgment/arithmetic line.
- [`docs/04-strategies.md`](docs/04-strategies.md) — the three strategy engines, with formulas.
- [`docs/05-risk-and-safety.md`](docs/05-risk-and-safety.md) — guardrails, sizing, kill switch, compliance.
- [`docs/06-execution-and-stack.md`](docs/06-execution-and-stack.md) — brokers, the adapter, repo + stack.
- [`docs/07-roadmap.md`](docs/07-roadmap.md) — the staged build plan (Phase 0 → guarded live).

---

## 1. The one idea that makes this safe

There is a **hard line between judgment and arithmetic**, and we never let one side do the other's job.

| | **Reasoning layer** | **Deterministic core** |
|---|---|---|
| Who | Claude (Agent SDK) + MCP/REST | Plain Python: `numpy`/`pandas`/`pandas-ta`, pure functions, unit-tested |
| Does | Reads messy text, ranks candidates, writes a thesis, reflects post-trade, allocates a *share* of a fixed risk budget | Computes indicators, sizes positions, **enforces risk limits**, builds and submits orders, reconciles fills |
| Never does | Computes a dollar amount, approves an order, raises a limit, flips paper→live, clears the kill switch | Makes a subjective call or writes prose |

The sentence to remember (from `docs/03`): **the LLM ranks and narrates; the gate and the math are sealed Python the model cannot reach.** The LLM is an advisor with no hands (`docs/05` §0). It does not own a `place_order` tool, a `set_risk_limit` tool, or any path to the broker. Those functions exist only in the deterministic core, invoked by the Python loop, and they refuse to act unless deterministic preconditions are met.

This is enforced by **capability design, not by asking the model nicely**:

1. **Tool partitioning** — Claude's tools *read* data and *write* notes/rankings. No order tool exists in its surface.
2. **One-way data shape** — Claude's outputs are typed as `Ranking` / `Note` / `Allocation(multiplier ∈ [0, cap])`. The schema makes it *impossible* to emit a dollar size or an approval token.
3. **The gate owns the broker handle** — only `core/risk.py` can mint an approval; only `core/execution.py` accepts one. No approval, no order.
4. **Fail-closed everywhere** — missing config, stale data, a garbled model response, or any exception resolves to **VETO / no-trade / PAPER**, never to an unchecked order or an accidental live route.

---

## 2. From the crypto-MCP playbook to equities/options

This system is a translation of a 24/7 "10 crypto MCPs" stack into the realities of US equities, ETFs, and options. The shape of the agent system carries over; **the guardrails and the clock are heavier** (`docs/03` §2).

| Crypto idea | Equities/options analog | Architectural consequence |
|---|---|---|
| 24/7 always-on loop | Market hours 9:30–16:00 ET, ~9 holidays, weekends closed | A **market-calendar guard** gates the whole loop. Most of the day the system sleeps. |
| On-chain ledger (Dune) — *see every wallet* | **No public ledger exists.** The honest analog is **options flow + SEC filings + short interest**: *free but lagged* (13F ~45d, short interest ~2wk) plus *timely but paid* (options sweeps, dark pool). | This is the genuine edge section (`docs/02` §2) — and a sober reminder that the crypto "I can see the whale's wallet" advantage does **not** cleanly exist here. |
| DEX/CEX execution | **Broker adapter** (Alpaca paper default; Tradier sandbox second) | Execution is a thin, swappable Python adapter pointed at paper. |
| Perp/spot tokens, degen leverage | Equities, ETFs, **defined-risk options only** | Hard caps live in deterministic config, not a prompt. No naked short options. |
| Mempool/gas | Spread, slippage, halts, PDT, T+1 settlement | The risk module encodes microstructure and regulation, not just position size. |

Provider choices, free-tier limits, and which integrations are real MCP servers vs. our own REST wrappers are all in `docs/02`. The principle from there: **anything holding broker credentials or touching orders is either the official Alpaca MCP or our own audited code — never an unaudited community server.**

---

## 3. The system diagram

The Python backbone owns control flow. It *calls* Claude for judgment; Claude never calls the loop.

```
┌──────────────────────────── PYTHON BACKBONE (owns the loop) ─────────────────────────────┐
│  cron ─► market-calendar guard ─► phase runner (pre-market │ intraday │ EOD)              │
│                                                                                           │
│   ┌─────────────────────── REASONING LAYER — Claude Agent SDK ───────────────────────┐   │
│   │  [Claude] Orchestrator / PM     [Claude] Signal-ranking (per strategy)            │   │
│   │  [Claude] Research summarizer    [Claude] Reflection / journaling                 │   │
│   │  tools exposed to Claude = READ data, WRITE notes/rankings ONLY                   │   │
│   │  (no place_order, no set_risk_limit, no go_live, no clear_kill)                   │   │
│   └───────────────┬──────────────────────────────────────┬───────────────────────────┘   │
│                   │ MCP servers + REST (read-mostly)      │ direct Python calls           │
│                   ▼                                       ▼                                │
│   ┌──────────────────────────────┐        ┌──────────────────────────────────────────┐   │
│   │  DATA / MCP LAYER             │        │  DETERMINISTIC CORE  (sealed, no LLM)     │   │
│   │  • market data (Alpaca)       │        │  indicators.py   options_math.py          │   │
│   │  • options chains+greeks      │        │  sizing  ─►  risk.py ◄── THE VETO GATE     │   │
│   │    (Tradier sandbox/ORATS)    │        │  execution.py (route→ack→fill)            │   │
│   │  • news/filings/flow          │        │  reconcile.py    journal.py    store.py   │   │
│   │  • broker = ADAPTER, NOT an   │        │  brokers/ (alpaca, tradier, factory,      │   │
│   │    MCP tool Claude can call    │        │            paper_guard)                  │   │
│   └──────────────────────────────┘        └──────────────────────────────────────────┘   │
│                                                                                           │
│   State: SQLite (default, one file) ──► Supabase (optional, hosted/multi-device)          │
│   Output: one self-contained dashboard.html  ──►  reviewed on an iPhone                    │
└───────────────────────────────────────────────────────────────────────────────────────────┘
```

**Key invariant:** the only arrow into EXECUTION comes out of the RISK GATE. There is no edge from any Claude agent directly to Execution, and no edge from anywhere to the broker except through `core/execution.py`, which re-asserts `risk.check()` as belt-and-suspenders.

---

## 4. The pipeline as one narrative: data → signals → decision → risk → execution → reflection

This is the whole system in six moves. Each move names the doc that specifies it.

### 4.1 DATA — *ingest, normalize, never let Claude read a number out of prose* (`docs/02`, `docs/03` §3.2)
The Ingestors pull OHLCV bars, option chains (with greeks/IV), news, filings, earnings calendars, and short interest from MCP servers and REST wrappers. They produce two distinct things:
- **Deterministic feature tables** (price, volume, IV, OI) — written to the store by Python. These are the only numbers allowed downstream.
- **Claude research notes** — short, cited, sentiment/uncertainty-tagged text, explicitly labeled as *judgment*. A note can flag risk; it can never become a dollar amount.

The honest constraint: **free tiers are delayed and throttled.** Paper-trade against the *same* delay you will trade live on, cache aggressively in Python so the agent loop doesn't burn rate limits, and treat the "edge" data (timely options flow, dark pool, daily borrow) as *paid* — it is not free.

### 4.2 SIGNALS — *Python computes eligibility and a raw score; Claude only ranks* (`docs/04`, `docs/03` §3.3)
One Signal agent per strategy family. Each is a **pair**:
- **Python part** computes the strategy's indicators and emits **eligible candidates** with a raw score. Hard eligibility filters — price floor, dollar-volume floor, spread cap, OI floor, has-options-chain — live here and are non-negotiable (`docs/04` §0.4).
- **Claude part** ranks the *already-eligible* candidates, writes a one-line thesis, flags ambiguity, and may **demote** but never **promote** an ineligible name. For options, Claude chooses among **pre-constructed, pre-vetted defined-risk structures** Python generated; it never invents a strike or expiry freehand.

Output per candidate: `{symbol/structure, side, raw_score, claude_rank, thesis, confidence}` — **no sizes, no orders.**

### 4.3 DECISION — *the Orchestrator allocates a share of a fixed budget; sizing is a formula* (`docs/03` §3.1, §4.2)
The [Claude] Orchestrator decides which strategies are active today and what **fraction of a deterministically fixed risk envelope** each gets, informed by regime (VIX, SPY trend), the calendar (FOMC → trim gross), and yesterday's journal. Its "allocation" is a set of multipliers in `[0, cap]`, **clamped by Python**. It can never raise the total envelope. Then sizing happens in pure Python (`core/sizing.py`): given a candidate, account equity, and its allocated slice, return a **quantity** and a **max-loss**. Claude contributes nothing to this math — its rank only affects *which* candidates reach sizing and in what order.

### 4.4 RISK — *the non-bypassable veto gate* (`docs/05`, `docs/03` §4.3)
Every sized order passes through `core/risk.py` → `APPROVE | VETO(reason)`. It is deterministic, mandatory, LLM-free, and **fail-closed**. At minimum it enforces (full list in `docs/05` §2, §9):

- Per-trade max loss ≤ cap; per-position notional ≤ 5% equity.
- **Portfolio heat** ≤ 6% equity — the sum of `(entry − stop) × size` across all open positions, with correlated names (>0.8) treated as **one cluster** so a "diversified" book that is secretly one bet can't pass.
- **Daily loss limit** (−3% of start-of-day equity) → trips the **kill switch** (flatten-only).
- Options: **defined-risk only** (naked short rejected), options BP ≤ 30%, min DTE, OI/spread floors.
- Regulatory/microstructure: **PDT** count (margin <$25k), settled-cash/buying-power, options approval tier, no orders into a halt, market-open check, **staleness guard** (stale/crossed/wide quote → block).
- Sanity/fat-finger: qty > 0, price sane vs. mid, no over-fill, idempotency.

A VETO is final for the cycle and is journaled with an inputs hash. There is no appeal path to Claude.

### 4.5 EXECUTION — *one adapter, paper by default, idempotent* (`docs/06`)
Approved orders flow through the **broker-agnostic `BrokerAdapter`** (`docs/06` §2). Nothing outside `core/brokers/` imports a broker SDK. The lifecycle is `signal → risk gate → route → ack → fill → reconcile → journal`:
- **Idempotency**: every order carries a deterministic `client_order_id = hash(cycle, signal, symbol, side, qty, ts)`. A retry after an ambiguous timeout resends the *same* id; the broker dedupes. This is the specific defense against a network drop doubling your size.
- **Order safety**: limit / marketable-limit only (never blind market), slippage caps, fat-finger bounds, partial-fill accounting (exits reference *filled* qty, not requested qty).
- **Paper/live is a property of the constructed instance**, not an `if` scattered through the code. With no explicit guarded live action, the factory returns `AlpacaAdapter(is_paper=True)`. You cannot accidentally be live.
- After every order, `reconcile()` diffs broker truth against the journal; **the broker wins every disagreement.**

### 4.6 REFLECTION — *Claude reasons over Python-computed facts; lessons bias, never bypass* (`docs/03` §3.6)
At EOD, `core/reconcile.py` computes realized P&L deterministically. Then the [Claude] Reflection agent attributes P&L, notes what held and what didn't, and emits **per-strategy performance signals** that bias *tomorrow's* allocation — **within caps**. Lessons are advisory text. They can tilt a multiplier; they can never relax a limit. The EOD digest is pushed to the operator's phone.

---

## 5. The autonomous market-hours loop

The loop runs only when the market calendar (e.g. `pandas-market-calendars`, cross-checked with the broker clock) says the market is open. On weekends/holidays the guard short-circuits, so a dumb daily cron is safe — each phase is a short, idempotent, re-runnable process invocation, not a long-running daemon (`docs/03` §8, `docs/06` §3.1).

```
08:00 ET  PRE-MARKET SCAN  (cron fires → calendar guard: open today?)
          Orchestrator → Ingestors: overnight news, gaps, earnings, econ calendar
              Python: feature tables (RSI, ATR, BBWP, IV rank, implied move) → store
              Claude: cited research notes + tags (judgment only)
          Orchestrator: read regime + journal → set TODAY's risk-budget allocation (clamped)
          ► active strategies + per-strategy budget %  →  pushed to phone for optional review
            [operator may VETO a strategy or trim the envelope; can NEVER raise caps]

09:30+    INTRADAY LOOP  (every N minutes while open; reconcile at start)
          per Signal agent:  Python eligible candidates ─► Claude rank + thesis + confidence
          Orchestrator: merge, de-conflict (two strategies, opposite sides → resolve)
            │
            ▼  SIZING (deterministic)         core.sizing(candidate, equity, slice) → qty, max_loss
            ▼  RISK GATE ★ non-bypassable ★   core.risk.check(...) → APPROVE | VETO(reason)
            ▼  [HUMAN CHECKPOINT if manual]   gate-approved order queues for one-tap approve/skip
            ▼  EXECUTION (paper default)       Execution.submit → re-assert risk.check → fill | reject
                                               idempotent client_order_id; partial-fill aware
            ▼  RECONCILE + JOURNAL             broker truth vs local; every transition is a row

16:05     EOD RECONCILE + JOURNAL
          reconcile broker vs local → realized P&L (deterministic)
          Reflection (Claude over facts): per-trade analysis, lessons, per-strategy signal
              ──► biases TOMORROW's allocation (within caps)
          ► EOD digest pushed to phone

ANY TIME  KILL SWITCH — automatic (daily-loss / reconcile-mismatch / data-breaker / runaway-rate)
          or one-tap manual from the iPhone. Stopping is instant; clearing is slow, manual,
          and requires a named root cause. The asymmetry is the point.
```

**Degraded mode:** if the Claude layer is unavailable, the system can run a no-judgment, formula-only pass or simply skip the day. It fails safe, never unsafe — a system that is already safe *without* the LLM, with judgment added on top.

---

## 6. How the three strategies plug in

All three are `Strategy` implementations behind one interface (`strategies/base.py`): each produces `Signal[]` from the shared data contract, and every magic number references `config/strategies.yaml`. They share the universe filters, the risk budget, and the same downstream sizing → gate → execution path. They differ only in their **signal math** and their **options expression** — and every one of them is **defined-risk where options are involved** (`docs/04`).

| | **A — Volatility-Compression Breakout** | **B — Premium Harvesting** | **C — Earnings-Vol Plays** |
|---|---|---|---|
| Crypto analog | "thin vol that could break out" | "collect premium through cycles" | "bets on big earnings days" |
| Core signal (Python) | BBWP ≤ 10%, TTM squeeze on, ATR contraction, NR7/inside days, volume dry-up, optional VCP quality tilt | IV rank ≥ 0.30, short strike at 16–30Δ, 30–45 DTE, credit ≥ ⅓ width | implied move (ATM straddle) vs. 8-report realized median → `edge_ratio`; term-structure ≥ 1.20 for crush |
| Direction | The market resolves it (breakout pivot); long-biased | Neutral-to-directional, defined or covered | Long vol if implied cheap (≤0.85), short vol (condor) if rich (≥1.25) |
| Expression | Shares (ATR stop) or call debit spread if IV cheap | CSP, covered call, vertical credit spread, iron condor | Long straddle/strangle OR iron condor / calendar — **defined-risk only, always** |
| When NOT to | No squeeze; extended >1 ATR; VIX > 28; vol didn't expand; naked shares into earnings | IV rank < 0.30; earnings in cycle; illiquid chain; **never naked calls** | No edge band (0.85–1.25); <6 clean reports; insufficient crush; can't build defined-risk in budget |
| Worked example | `docs/04` §1.9 | `docs/04` §2.8 | `docs/04` §3.7 |

**Where Claude touches them, and where it doesn't:** Claude curates the nightly candidate list (removes names with known binary events inside the horizon, sector-clusters, writes a go/no-go/resize thesis) and, for options, *picks among pre-vetted defined-risk structures*. It never computes a signal, sizes a trade, or places an order. The Orchestrator decides today's *mix* of A/B/C as budget multipliers — e.g. low-VIX trend day favors A, a high-IV-rank no-catalyst tape favors B, an earnings cluster lights up C — all clamped to the fixed envelope. A strategy graduates from paper to live only after an **out-of-sample paper period (≥60 trading days)** with positive expectancy *after modeled costs* (`docs/04` §6); otherwise it does not earn capital. Full stop.

---

## 7. What this architecture deliberately refuses to do

- It will not let the LLM size a position or approve an order. Ever.
- It will not trade naked/undefined-risk options by default.
- It will not run when the calendar says the market is closed.
- It will not go live without an explicit, separate, human-provisioned credential **and** a dated confirmation **and** the size ramp (`docs/05` §1).
- It will not let an LLM flip paper→live or clear the kill switch.
- It will not pretend a good paper curve is proof of edge.

**Build order (see `docs/07`):** the deterministic core first — `risk.py`, `sizing.py`, the adapter, idempotency, reconcile — tested to the point of boredom. The Claude layer is the upgrade that adds judgment *on top of a system that is already safe without it.*

---

*Last reviewed: 2026-06-30. Provider tiers, MCP availability, and broker/options/regulatory rules change frequently — re-verify against current docs before relying on anything here. Research/operations tool, not financial advice. Default is paper; keep it that way until an edge is proven and you can afford to be wrong.*
