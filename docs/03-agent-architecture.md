# 03 — Agent Architecture

> **Scope.** This document specifies the multi-agent topology for an autonomous **US equities / ETFs / options** research-and-trading system. It is the architecture layer; data sources live in `02-data-sources.md`, strategy families in `04-strategies.md`, and risk policy in `05-risk.md`.
>
> **Not financial advice.** This is a research and edge-discovery tool, not a money printer. Most retail algorithms underperform a low-cost index fund after fees, slippage, and taxes. The default mode is **paper trading**. Going live is an explicit, guarded, human action (see [§9](#9-human-in-the-loop--going-live)). Nothing here is a recommendation to buy or sell any security.

---

## 1. Design philosophy: a hard line between *judgment* and *arithmetic*

The single most important idea in this system is the **division of labor** between the LLM and deterministic code. We treat them as two different kinds of employee with two different job descriptions, and we never let one do the other's job.

| Layer | Who | Does | Never does |
|---|---|---|---|
| **Reasoning** | Claude (via Agent SDK) + MCP/APIs | Synthesis of messy text, ranking candidates, narrative judgment, "is this news already priced in?", ambiguous tie-breaks, post-trade reflection | Compute an indicator, size a position, decide if an order is within risk limits, place an order |
| **Deterministic core** | Plain Python (`numpy`/`pandas`/`pandas-ta`), pure functions, fully unit-tested | Indicators, position sizing math, **risk-limit enforcement**, order construction, broker I/O, reconciliation, persistence | Make a subjective call, write prose, "decide" anything that isn't a formula |

Concretely:

- **Claude proposes; Python disposes.** Claude can say "I rank AAPL as the strongest momentum candidate today and here's why." Python decides whether AAPL is even *eligible*, what *size* the order is (formula-driven), and whether the resulting order *passes the risk gate*.
- **The risk gate is deterministic Python and is non-bypassable.** There is no prompt, no tool, and no agent path that lets Claude approve an order the risk module rejected. The LLM literally does not have a tool that places an order; only the Execution module does, and it calls the risk module first. (See [§4 Risk Manager](#43-risk-manager-the-veto-gate) and [§6 control flow](#6-why-claude-cannot-bypass-the-gate-structurally).)
- **Every number that touches money is reproducible.** Given the same inputs, the deterministic core produces the same sizing and the same gate decision every run. LLM output is advisory metadata attached to a candidate, never the source of a dollar amount.

Why this matters: LLMs are excellent at *reading the room* and terrible at being a calculator or a compliance officer. Inverting that — letting the model do arithmetic or "use judgment" on a risk limit — is how retail autonomous systems blow up.

---

## 2. Translating the crypto-MCP playbook to equities/ETFs/options

The source playbook is written for 24/7 crypto. Equities are different in ways that *change the architecture*, not just the tickers.

| Crypto-MCP idea | Equities / ETFs / options analog | Architectural consequence |
|---|---|---|
| 24/7 trading, always-on loop | **Market hours** 9:30–16:00 ET, plus pre/post sessions; weekends + ~9 holidays/yr closed | A **scheduler with a market calendar** gates the whole loop. Most of the day the system is asleep. |
| On-chain wallet / DEX | **Broker account** via a broker adapter (Alpaca paper by default) | Execution agent is a thin, swappable adapter; paper endpoint is the default target. |
| Perp/spot tokens | Equities, ETFs, **options contracts** (defined-risk spreads preferred) | Options add an "instrument selection" sub-step (strike/expiry/structure) that is *formula-driven*, with Claude only ranking among pre-vetted structures. |
| Mempool / gas | **Spread, liquidity, slippage, halts, PDT rule, settlement (T+1)** | Risk module must encode market-microstructure and regulatory limits, not just position size. |
| "Degen" leverage | **Defined-risk only by default**: no naked short options, capped notional, capped per-trade loss | Hard caps live in deterministic config, not in a prompt. |
| Token sentiment scraping | News, filings, transcripts, analyst notes via **data MCPs** | Same ingestion shape; Claude summarizes/ranks, never trades on raw scrape. |

The shape of the agent system carries over; the *guardrails and the clock* are heavier.

---

## 3. Agent roster (the org chart)

Seven roles. Some are LLM agents (Claude), some are deterministic Python modules we still call "agents" because they participate in the message flow. The label in **[brackets]** tells you which kind it is — this is load-bearing.

```
                          ┌───────────────────────────────────────────┐
                          │  ORCHESTRATOR / PORTFOLIO MANAGER  [Claude] │
                          │  owns the loop, allocates risk budget       │
                          └───────────────┬─────────────────────────────┘
              ┌───────────────────────────┼───────────────────────────────┐
              ▼                           ▼                               ▼
   ┌────────────────────┐    ┌───────────────────────┐      ┌──────────────────────┐
   │ DATA & RESEARCH     │    │   SIGNAL AGENTS        │      │  REFLECTION /        │
   │ INGESTORS  [Python  │    │  one per strategy      │      │  JOURNALING [Claude  │
   │ + Claude summarize] │    │  family  [Python calc  │      │  over Python facts]  │
   │ pull via MCP/APIs   │    │  + Claude rank/judge]  │      │  post-trade analysis │
   └─────────┬──────────┘    └───────────┬───────────┘      └──────────▲───────────┘
             │ facts/features            │ ranked candidates           │ trade outcomes
             └──────────────┬────────────┘                             │
                            ▼                                          │
              ┌───────────────────────────────────────┐               │
              │     RISK MANAGER  [Python ONLY]        │               │
              │     HARD VETO GATE — every order       │               │
              │     must pass; non-bypassable          │               │
              └───────────────┬───────────────────────┘               │
                              │ approved + sized orders               │
                              ▼                                       │
              ┌───────────────────────────────────────┐               │
              │     EXECUTION  [Python adapter]         │ ──fills──────┘
              │     broker adapter, PAPER by default    │
              └───────────────────────────────────────┘
```

### 3.1 Orchestrator / Portfolio Manager — **[Claude]**

The conductor. Owns the market-hours loop and the **risk budget allocation** across strategies.

- **Responsibility:** Decide *which strategies are active today* and *what fraction of the day's risk budget* each gets, based on regime (e.g., high-VIX → trim mean-reversion, favor trend), recent strategy performance from the journal, and the calendar (FOMC day → reduce gross). It sequences the sub-agents, merges their candidate lists, and resolves conflicts (two strategies want opposite sides of the same name).
- **Inputs:** Account state, open positions, the day's risk budget config, regime features (from ingestors), recent journal summaries, calendar/event flags.
- **Outputs:** A **risk-budget allocation** (per-strategy % of a *deterministically fixed* total risk envelope), an ordered task plan, and a merged, de-conflicted candidate list handed to sizing → risk gate.
- **Hard boundary:** The Orchestrator allocates *shares of a budget the deterministic core defines*; it can never raise the total risk envelope, override a limit, or push an order past the gate. Its "allocation" is a set of multipliers in [0, cap], clamped by Python.

### 3.2 Data & Research Ingestors — **[Python pulls; Claude summarizes]**

- **Responsibility:** Pull market data, fundamentals, news, filings, calendars from MCP servers and APIs; normalize into typed records; let Claude turn unstructured text into structured, ranked, cited summaries.
- **Inputs:** MCP/API endpoints (market data, news, economic calendar, options chains), watchlist, lookback windows.
- **Outputs:** (a) **Deterministic feature tables** (OHLCV, IV, volume, etc.) written to the store; (b) **Claude research notes** — short, cited summaries with a sentiment/uncertainty tag, explicitly labeled as judgment, never as a number that sizes a trade.
- **Boundary:** Numeric features come from the API/Python, never from Claude "reading" a price out of an article. Claude's output is text + tags only.

### 3.3 Signal Agents (one per strategy family) — **[Python computes; Claude ranks]**

One agent per strategy family (e.g., `trend_following`, `mean_reversion`, `earnings_drift`, `options_premium`). Each is a pair:

- **Python part:** Computes the strategy's indicators and emits **eligible candidates** with a raw score, deterministically. Eligibility (liquidity floor, price floor, spread cap, has-options-chain, etc.) is hard-coded here.
- **Claude part:** Given the eligible candidates *plus* research notes, **ranks** them, writes a one-line thesis each, flags ambiguous ones, and can *demote* (never *promote* an ineligible name). For options, Claude picks among **pre-constructed, pre-vetted defined-risk structures** that Python generated; it never invents a strike/expiry freehand.
- **Inputs:** Feature tables, research notes, strategy config (its slice of the risk budget).
- **Outputs:** A ranked candidate list per strategy: `{symbol/structure, side, raw_score, claude_rank, thesis, confidence}`. **No sizes, no orders** — sizing happens later in deterministic code.

### 3.4 Risk Manager — **[Python ONLY, no LLM]**

The **hard VETO gate**. Covered in [§4.3](#43-risk-manager-the-veto-gate). The most important module in the repo.

### 3.5 Execution agent — **[Python adapter, paper by default]**

- **Responsibility:** Translate an approved, sized order into a broker API call and confirm the fill. Swappable adapter (`PaperBroker`, `AlpacaPaper`, `AlpacaLive`). **Default = paper.**
- **Inputs:** Approved + sized orders (only ever produced *after* the gate). Outputs: fills, broker ack/errors, updated positions → persisted.
- **Boundary:** Calls `risk.check()` itself as a final pre-trade assertion even though orders arrive pre-approved (belt and suspenders). Live adapter is feature-flagged off and requires a human-set env + confirmation token.

### 3.6 Reflection / Journaling agent — **[Claude over Python-computed facts]**

- **Responsibility:** After fills and at EOD, analyze what happened, attribute P&L, detect rule violations or near-misses, and **update notes/lessons** that the Orchestrator and Signal agents read tomorrow.
- **Inputs:** Deterministic trade records, realized/unrealized P&L (computed by Python), the original theses, slippage vs. expected.
- **Outputs:** A journal entry per trade and an EOD summary: what worked, what didn't, hypotheses, and **per-strategy performance signals** that feed tomorrow's risk-budget allocation. Lessons are *advisory text*; they can bias allocation within caps but can never relax a limit.

---

## 4. The deterministic core in detail

These modules are plain Python, pure where possible, and **unit-tested to the point of boredom**. They are the ground truth.

### 4.1 Indicators (`core/indicators.py`)
Pure functions over price/volume frames → numbers. RSI, ATR, moving averages, realized vol, IV rank, etc. No I/O, no LLM, deterministic. Tested against known fixtures.

### 4.2 Sizing (`core/sizing.py`)
Given a candidate + account equity + the candidate's allocated risk slice, returns a **quantity** and a **max-loss**. Formula-driven (e.g., risk-per-trade = `min(strategy_budget, account_risk_pct * equity)`, then `qty = floor(risk_per_trade / per_unit_risk)` where per-unit-risk is ATR-based for stock or the defined max-loss for an option spread). **Claude provides no input to this function.** Its rank only affects *which* candidates reach sizing and in what order, never the math.

### 4.3 Risk Manager (the VETO gate) — `core/risk.py`
A single function, conceptually:

```python
def check(order, account, positions, config, calendar) -> RiskDecision:
    """Returns APPROVE or VETO(reason). Pure, deterministic, no LLM, no network for the decision."""
```

It enforces, at minimum:

- **Per-trade max loss** ≤ cap; **per-position notional** ≤ cap.
- **Portfolio gross/net exposure** ≤ caps; **sector/single-name concentration** ≤ caps.
- **Daily loss limit / kill-switch**: if realized+unrealized daily P&L breaches the floor, **veto everything and flatten-only mode**.
- **Options-specific:** defined-risk only (reject undefined/naked structures), max contracts, min DTE, liquidity/OI/spread floors.
- **Regulatory/microstructure:** PDT rule check, settled-cash/buying-power check, no orders into a halted symbol, market-open check via calendar, no orders when data is stale.
- **Sanity:** order quantity > 0, price sane vs. last, no duplicate/over-fill.

**Properties that make it a true gate:**
1. **Deterministic** — same inputs → same verdict, fully testable.
2. **Mandatory** — Execution refuses to act on any order without an `APPROVE` token from `risk.check()`; there is no other code path to the broker.
3. **No LLM input** — Claude cannot pass arguments to `check()` and has no tool that returns an APPROVE. A VETO is final for this cycle.
4. **Fail-closed** — any exception, missing config, or stale data → VETO, not APPROVE.

### 4.4 Reconciliation (`core/reconcile.py`)
EOD: pull broker positions/fills, compare to the local store, flag and persist discrepancies, compute realized P&L. Deterministic. Feeds the journal.

---

## 5. Message & decision FLOW — the market-hours loop

The loop runs only when the market calendar says the market is open (or in pre/post windows we explicitly enable). On weekends/holidays the scheduler no-ops.

```
T-90m  PRE-MARKET SCAN
        scheduler(open today?) ──► Orchestrator
        Orchestrator ──► Ingestors: pull overnight news, gaps, earnings, econ calendar
        Ingestors:  Python features ─┐
                    Claude notes  ────┴──► store
        Orchestrator: read regime + journal ──► set TODAY's risk budget allocation (clamped)
        ► Output: active strategies + per-strategy budget %  (human can review on phone)

09:30+ INTRADAY SIGNALS  (loop every N minutes while open)
        Orchestrator ──► each Signal agent
            Python: compute indicators → eligible candidates (hard filters)
            Claude: rank + thesis + confidence (judgment only)
        Orchestrator: merge lists, de-conflict, order by priority

  →    SIZING (deterministic)
        core.sizing(candidate, equity, allocated_slice) → qty + max_loss
        (Claude provides NO input here)

  →    RISK GATE  ★ non-bypassable ★
        for each sized order: core.risk.check(...) → APPROVE | VETO(reason)
        VETO ⇒ dropped this cycle, reason journaled. No appeal path to Claude.

  →    EXECUTION  (paper by default)
        Execution.submit(approved_order)  [re-asserts risk.check]  → fill | reject
        fills + acks → store

        [HUMAN CHECKPOINT if configured: orders queue for one-tap approve/skip on phone]

16:00  EOD RECONCILIATION + JOURNAL
        core.reconcile: broker vs local → discrepancies, realized P&L (deterministic)
        Reflection/Journaling (Claude over facts): per-trade analysis, lessons,
            per-strategy performance signal ──► influences TOMORROW's allocation (within caps)
        ► EOD summary pushed to operator's phone.
```

Key invariant: **the only arrow into "EXECUTION" comes out of "RISK GATE."** There is no edge from any Claude agent directly to Execution.

---

## 6. Why Claude cannot bypass the gate (structurally)

This is enforced by *capability design*, not by asking the model nicely:

1. **Tool partitioning.** The Claude agents are given tools for *reading* data and *writing* notes/rankings. They are **not** given a `place_order` tool or a `set_risk_limit` tool. Those functions exist only in the deterministic core, invoked by the Python loop, never exposed to the model.
2. **One-way data shape.** Claude's outputs are typed as `Ranking` / `Note` / `Allocation(multiplier∈[0,cap])`. The schema makes it *impossible* to emit a dollar size or an APPROVE token.
3. **Gate owns the broker handle.** Only `core.risk` can mint an `Approval`, and only `Execution` accepts an `Approval`. No `Approval`, no order. Claude can't construct one (it's not in any tool's output schema).
4. **Fail-closed everywhere.** Missing/garbled Claude output degrades to "no candidate," never to "unchecked order."

If you remember one sentence from this doc: *the LLM ranks and narrates; the gate and the math are sealed Python the model cannot reach.*

---

## 7. State persistence

Keep it boring and local first.

### 7.1 Default: SQLite
A single `portfolio.db` file. Tables (minimum):

- `accounts` (equity snapshots), `positions`, `orders`, `fills`
- `features` (computed indicators, keyed by symbol+timestamp)
- `candidates` (with Claude rank/thesis/confidence)
- `risk_decisions` (every APPROVE/VETO + reason — the audit log)
- `journal` (per-trade notes, EOD summaries, lessons)
- `config_runs` (the budget/allocation actually used each day)

SQLite is enough for a single-operator, single-account system, is trivially backed up (copy the file), works offline, and needs no infra. **Never store broker keys in the DB** — keys live in env/secret manager (see [§10](#10-secrets--safety)).

### 7.2 Optional hosted upgrade: Supabase (via Supabase MCP)
When the operator wants multi-device access, dashboards, or hosted history, swap the persistence adapter to Supabase (Postgres). A **Supabase MCP** exists, so the system can read/inspect tables from the agent layer for *read-only research/dashboards*. The same table schema migrates over. This is an upgrade, not a requirement — start on SQLite.

> Adapter pattern: `store.py` exposes `Store` with `SQLiteStore` and `SupabaseStore` implementations behind one interface. Switching is a config flag, not a rewrite. Trade-writing always goes through deterministic Python regardless of backend.

---

## 8. Scheduling & market-hours awareness

- **Market calendar is authoritative.** Use a US-market calendar (e.g., `pandas-market-calendars`) for sessions, half-days, and the ~9 annual holidays. The loop checks `calendar.is_open(now)` before doing anything; otherwise it no-ops and sleeps.
- **Cron / scheduler.** A cron-style schedule triggers the *phases*: pre-market scan (~08:00 ET), intraday loop start (09:30 ET) running every N minutes until 15:55, EOD reconcile+journal (16:05 ET). On holidays/weekends the calendar check short-circuits, so a dumb cron that fires daily is fine — the calendar guard handles "should I actually run."
- **Timezone discipline.** Everything internal is timezone-aware UTC; display in ET. DST handled by the calendar lib, not by hand.
- **Staleness guard.** If market data is older than a threshold when a phase fires, the risk gate fails closed and the cycle is skipped + journaled.

This maps cleanly onto the host's scheduling primitives (cron) so the operator doesn't need a long-running daemon babysitter — each phase is a short, idempotent run.

---

## 9. Human-in-the-loop & going live

The operator runs this from an **iPhone via Claude Code**, so checkpoints are designed to be *one-tap and reviewable*, not dashboards.

- **Paper by default, always.** The Execution adapter ships pointed at a paper endpoint. Live trading requires (a) an explicit env flag, (b) a live broker key the operator sets themselves, and (c) a per-session confirmation token. Absent any one → paper.
- **Pre-market review checkpoint (optional, recommended).** The day's active strategies + risk-budget allocation are pushed to the phone; the operator can veto a strategy or trim the envelope before the loop starts. They can never *raise* caps from the phone beyond config maxima.
- **Order-approval checkpoint (configurable).** In `manual` mode, every gate-approved order **queues** for one-tap approve/skip on the phone before Execution submits. In `auto` mode (paper only by recommendation), approved orders submit directly. A live account in `auto` mode requires an extra explicit opt-in flag.
- **Kill switch.** A single command/flag flips the system to **flatten-only**: cancel working orders, no new entries, optionally close positions. The daily-loss limit triggers this automatically.
- **EOD digest.** Reflection agent's summary is pushed to the phone: P&L, what the journal learned, anything that hit the gate.

Sober note for the operator: a green paper-trading curve is *evidence to keep researching*, not proof of edge. Slippage, fills, taxes, and your own behavior under real losses are not in the paper sim.

---

## 10. Secrets & safety

- **No secrets in the repo, the DB, logs, or journal entries.** Broker/API keys come from environment variables or a secret manager, loaded at runtime, never committed, never printed.
- **Least privilege.** Paper key for the default path; the live key is a separate credential the operator provisions only when going live.
- **Audit log is sacred.** Every `risk_decision` (APPROVE/VETO + inputs hash + reason) is persisted. This is how you debug "why did/didn't it trade?" without trusting the LLM's recollection.
- **Determinism for replay.** Because sizing and the gate are pure functions, any day can be re-run from stored inputs to reproduce the exact decisions — essential for debugging and for trusting the system.

---

## 11. Mapping to Claude Agent SDK + MCP + Python backbone

```
┌──────────────────────────── PYTHON BACKBONE (the loop owner) ────────────────────────────┐
│  scheduler ──► market-calendar guard ──► phase runner (pre-mkt / intraday / EOD)          │
│                                                                                            │
│   calls Claude Agent SDK for the [Claude] roles ─────────────┐                             │
│   ┌──────────────────────────────────────────────────────┐  │                             │
│   │  Claude Agent SDK  (the reasoning layer)              │  │                             │
│   │   • Orchestrator agent      • Signal-ranking agent    │  │                             │
│   │   • Research-summary agent  • Reflection agent        │  │                             │
│   │   tools exposed to Claude = READ data, WRITE notes/   │  │                             │
│   │   rankings ONLY  (no place_order, no set_risk_limit)  │  │                             │
│   └───────────────┬──────────────────────────────────────┘  │                             │
│                   │ MCP servers (read-mostly)                │ direct Python calls          │
│                   ▼                                          ▼                              │
│   ┌─────────────────────────────┐         ┌────────────────────────────────────────────┐  │
│   │  MCP layer                   │         │  DETERMINISTIC CORE  (sealed, no LLM)      │  │
│   │  • market-data MCP/API       │         │  indicators.py  sizing.py                  │  │
│   │  • news / filings MCP        │         │  risk.py  ◄── THE GATE  execution.py        │  │
│   │  • options-chain MCP/API     │         │  reconcile.py  store.py (SQLite|Supabase)  │  │
│   │  • Supabase MCP (read/dash)  │         └────────────────────────────────────────────┘  │
│   │  • broker = adapter, NOT an  │                                                          │
│   │    MCP tool Claude can call  │                                                          │
│   └─────────────────────────────┘                                                          │
└────────────────────────────────────────────────────────────────────────────────────────────┘
```

- **Agent SDK** hosts the four Claude roles as agents with scoped, read-mostly tools. The SDK is invoked *by* the Python loop, never the other way around — Python owns control flow.
- **MCP servers** are the integration surface for *data* (market data, news, options chains) and the optional Supabase store. Crucially, **the broker is reached via a deterministic Python adapter, not via an MCP tool Claude can call**, so there is no model-accessible path to "place order."
- **Python backbone** is the loop owner, the scheduler client, and the home of the deterministic core. If the Claude layer is entirely unavailable, the system can still run a *degraded, no-judgment* pass (formula-only candidates) or simply skip the day — it fails safe, never unsafe.

---

## 12. End-to-end sequence example: one trade

A single momentum trade on a liquid ETF, paper account, manual order approval enabled.

```
08:02 ET  scheduler fires PRE-MARKET ─► calendar.is_open(today)=True ─► proceed
08:02     Orchestrator → Ingestors: pull overnight news + gaps for watchlist
08:03     Ingestors: market-data MCP → OHLCV; news MCP → 3 articles on QQQ
          Python: compute features (RSI, ATR, 20/50 MA) → store
          Claude (research): "QQQ: constructive overnight, no idiosyncratic risk
                              flagged. confidence: medium." (text + tag only)
08:04     Orchestrator: regime=trend-friendly (VIX low), journal says trend strat
          performed well this week → allocate trend_following 40% of fixed risk
          envelope (clamped to cap). [pushed to phone for optional review — operator
          taps APPROVE]

09:30     INTRADAY loop tick
09:35     Signal[trend_following]:
            Python: QQQ passes eligibility (price>floor, spread<cap, ADV ok),
                    raw_score=0.81, side=LONG
            Claude: ranks QQQ #1 of 5; thesis "clean breakout, vol expanding";
                    confidence=medium  (NO size, NO order)
09:35     Orchestrator: merge + de-conflict → QQQ LONG is top candidate

09:35     SIZING (deterministic):
            equity=100k(paper); trend slice risk=$400; per-unit risk=ATR-based stop
            → qty = 120 shares, max_loss=$396  (Claude contributes nothing here)

09:35     RISK GATE  ★
            risk.check(order=QQQ +120, account, positions, config, calendar):
              market open ✔  notional<cap ✔  max_loss $396≤$500 ✔
              gross exposure after add < cap ✔  daily loss limit not breached ✔
              data fresh ✔  qty>0, price sane ✔
            → APPROVE  (decision + inputs hashed → risk_decisions table)

09:35     HUMAN CHECKPOINT (manual mode): order queued to phone
            "BUY 120 QQQ ~ $XXX, max loss $396, thesis: clean breakout. [APPROVE]/[SKIP]"
            operator taps APPROVE

09:36     EXECUTION (paper adapter):
            re-asserts risk.check → still APPROVE
            PaperBroker.submit(BUY 120 QQQ) → FILLED @ price
            fill + ack → store; position updated

15:58     (later) stop/target managed by deterministic rules; assume exit fills.

16:05     EOD RECONCILE + JOURNAL
            reconcile: broker vs local match ✔; realized P&L computed (Python)
            Reflection (Claude over facts): "QQQ trend long worked; entry slippage
              2c better than modeled; thesis held. Lesson: low-VIX breakouts on
              index ETFs continue to perform — modest bump to trend allocation
              tomorrow, within caps."
            EOD digest pushed to phone.
```

Notice every dollar figure and every gate decision came from deterministic Python; every *judgment* ("constructive," "clean breakout," "bump allocation") came from Claude — and the two never traded places.

---

## 13. What this architecture deliberately refuses to do

- It will not let the LLM size a position or approve an order. Ever.
- It will not trade naked/undefined-risk options by default.
- It will not run when the calendar says the market is closed.
- It will not go live without an explicit, separate, human-provisioned credential + confirmation.
- It will not pretend a good paper curve is proof of edge.

Build the deterministic core first and test it to death. The Claude layer is the upgrade that adds judgment on top of a system that is already safe without it.
