# 06 — Execution & Stack

> **Scope:** US equities, ETFs, and **options**. Not crypto.
> **Architecture:** Claude + MCP/REST is the *reasoning layer*; a deterministic Python core does indicators, risk, and execution. This doc covers the **execution layer** (brokers + a broker-agnostic adapter) and the **full repo + tech stack** for the reborn AiStudios.
> **Default mode:** PAPER. Live trading is a separate, explicit, guarded human action (see §2.7). Paper is the only thing that runs without a deliberate, logged toggle.
> **Honesty clause:** This is a research/edge tool, not a money printer. Most retail algos underperform after costs, slippage, and fills you didn't model. The adapter below exists to make *bad* assumptions visible (paper vs. live divergence, partial fills, rejects), not to hide them. Nothing here is financial advice.

Companion docs: `02-data-sources.md` (where quotes/chains/flow come from), `04-execution.md`/`05-security.md` (referenced by the data-sources doc; this file is the consolidated execution + stack reference).

---

## 0. How to read this doc

Three parts, in order of "what do I need to decide first":

1. **§1 — Which broker(s).** Compare the four real automatable brokers for equities **and** options. Pick a starter.
2. **§2 — The adapter.** Define one broker-agnostic interface so the rest of the system never imports a broker SDK directly. Order lifecycle, idempotency, retries, partial fills, paper/live switching.
3. **§3 — The repo + stack.** Full directory tree, tech choices, and how it stays one-command-simple and iPhone-reviewable.

The throughline: **the deterministic core decides; the adapter executes; Claude reasons about both but touches neither orders nor keys directly.**

---

## 1. Broker comparison — automatable APIs for equities AND options

The bar: a real REST/library API, options support (not just equities), and a paper/sandbox environment so we never have to risk capital to develop. Four brokers clear it.

### 1.1 Comparison table

| Broker | Assets | Options support | Paper / Sandbox | API quality | Cost | Gotchas |
|---|---|---|---|---|---|---|
| **Alpaca** | US equities, ETFs, options, crypto | **Up to Level 3.** Single-leg + multi-leg (spreads) via the **same Orders API** as equities. Order types: market, limit, stop, stop_limit (stop/stop_limit single-leg only). Snapshots include **Greeks**. | **Native, excellent.** Paper account is first-class, free, unlimited; **options enabled by default and paper gets Level 3 automatically.** Same API surface as live — just swap the base URL/keys. | **Best-in-class REST.** Clean, modern, great docs, official Python SDK, **official MCP server** (defaults to paper). Easiest to automate of the four. | **Free** API + commission-free. Real-time SIP/OPRA is a paid data add-on; free data is IEX real-time / 15-min-delayed SIP. | **No native bracket/OTO for options** (you build exits yourself). Free options data is delayed/limited. Multi-leg supported but the level system rejects strategies above your account level with an error. |
| **Interactive Brokers (IBKR)** | Equities, ETFs, options, futures, FX, bonds — **deepest universe**, global. | **Deepest options support** of the four: complex multi-leg, combos, full Greeks, granular routing/exchange selection. Pro-grade. | **Yes**, paper account mirrors live. **But:** the live account must be **open, funded, IBKR Pro** before the paper account is usable, and **only one trading session per username at a time.** | Powerful but **steeper**. `ib_insync` (community, well-maintained sync/async wrapper, the de-facto Python choice) or the **Client Portal / Web API** (REST/WebSocket). | API is free; **market-data subscriptions cost** and are à-la-carte. Account has activity/inactivity nuances. | **Operationally heavy.** TWS/IB Gateway must be running; **gateway restart needs 2FA approval on the phone** (kills unattended restarts); **historical-data pacing violations** if you over-request; `reqAccountValues()` returns hundreds of KV pairs. The most capable and the most fiddly. |
| **Tradier** | US equities, ETFs, options | **Excellent options API.** Equities + options, multi-leg, and **Greeks + IV powered by ORATS** included in chains. Options-trader-favorite. | **Yes — clean, free sandbox** (separate Sandbox token vs. Production token). Orders stay in the sandbox; **data is delayed in sandbox.** | **Very good, simple REST.** Clean docs, easy auth (bearer token). No official MCP — **we write a thin REST wrapper** (per `02-data-sources.md`). | API access is **free with an account**; live trading has competitive/flat-fee options pricing. | **No official MCP** (wrap it ourselves). **Sandbox data is delayed**, so latency-sensitive logic must be validated against the delay you'll actually trade on. Production token needs a funded account. |
| **TastyTrade** | US equities, ETFs, options, futures | **Options-centric by design** — built by/for options traders. Multi-leg, full chains. | **Yes**, certification/sandbox at `api.cert.tastyworks.com`. **Resets every 24h** (positions/balances wiped); **quotes 15-min delayed**; OAuth2 supported. | Good REST + account-streamer WebSocket. **OAuth2 access tokens last 15 min** → must refresh frequently. Official JS SDK; Python via community libs. | **Free** API with account; competitive options commissions. | **Sandbox resets daily** (can't hold a paper position overnight to test multi-day logic). **15-min token expiry** adds session-management overhead. Smaller ecosystem than Alpaca/IBKR. |

### 1.2 Recommendation — **Alpaca (paper) + Tradier (sandbox)** as the starter

This pairing is deliberate and matches the data-sources stack (`02-data-sources.md` §3.1):

- **Alpaca = execution + account + equities/options orders.** Native paper trading, free, the cleanest REST, an **official MCP server that defaults to paper**, and the *same* Orders API for equities and options. This is where orders go. Paper-first is the literal default, not a configuration we have to remember.
- **Tradier sandbox = free option chains with Greeks + IV (ORATS).** Alpaca's free options data is thin; Tradier's sandbox hands us delayed chains *with Greeks and IV* at zero cost, which is exactly what the strategy/risk math needs. We use it primarily as a **data** source, and it's our **second execution adapter** so the abstraction in §2 is exercised by two real brokers from day one — not theoretical.

**Why not IBKR or TastyTrade first:**
- **IBKR** is the right *destination* once an edge is validated and we want depth, breadth, and serious routing — but the gateway-must-run + 2FA-restart + one-session constraints make it hostile to a lightweight, iPhone-operated, cron-driven loop. **Defer to the Pro stack.**
- **TastyTrade**'s 24h sandbox reset and 15-min token churn add friction for little starter benefit when Alpaca already covers options paper trading at Level 3.

> **The point of two starter brokers is the adapter.** If everything works through Alpaca *and* Tradier behind one interface, swapping in IBKR or TastyTrade later is a new adapter file, not a rewrite. Build the seam now while it's cheap.

---

## 2. The broker adapter — one interface, many brokers

**Principle:** nothing outside `core/brokers/` ever imports `alpaca`, `tradier`, `ib_insync`, or any broker SDK. The risk engine, the scheduler, the journal, and Claude's tools all speak to **one `BrokerAdapter` interface**. Brokers are plugins. Paper vs. live is a property of the *instance you construct*, never an `if` scattered through the code.

### 2.1 The interface

```python
# core/brokers/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

class OrderSide(str, Enum):   BUY = "buy"; SELL = "sell"
class OrderType(str, Enum):   MARKET = "market"; LIMIT = "limit"; STOP = "stop"; STOP_LIMIT = "stop_limit"
class TimeInForce(str, Enum): DAY = "day"; GTC = "gtc"; IOC = "ioc"; FOK = "fok"
class AssetClass(str, Enum):  EQUITY = "equity"; OPTION = "option"

@dataclass(frozen=True)
class OrderRequest:
    client_order_id: str          # idempotency key — WE generate it, deterministically (§2.4)
    symbol: str                   # "AAPL" or OCC option symbol "AAPL250117C00150000"
    asset_class: AssetClass
    side: OrderSide
    qty: float                    # contracts for options, shares for equities
    type: OrderType
    time_in_force: TimeInForce
    limit_price: float | None = None
    stop_price: float | None = None
    legs: list["OrderLeg"] | None = None   # multi-leg options spreads

class BrokerAdapter(ABC):
    """Every method is broker-agnostic. Implementations live in alpaca.py, tradier.py, ..."""
    name: str
    is_paper: bool                # surfaced everywhere; the dashboard shows it in red/green

    # --- read side (safe, idempotent, retryable) ---
    @abstractmethod
    def get_account(self) -> "Account": ...                 # equity, buying power, PDT flag
    @abstractmethod
    def get_positions(self) -> list["Position"]: ...        # incl. option positions w/ qty, avg, P&L
    @abstractmethod
    def get_quote(self, symbol: str) -> "Quote": ...        # bid/ask/last/ts (note: may be delayed)
    @abstractmethod
    def get_option_chain(self, underlying: str, expiration: str | None = None
                         ) -> list["OptionContract"]: ...   # strikes, greeks, IV, OI, bid/ask

    # --- write side (dangerous; idempotent via client_order_id) ---
    @abstractmethod
    def place_order(self, req: OrderRequest) -> "OrderAck": ...
    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> "CancelAck": ...

    # --- truth side (the system's conscience) ---
    @abstractmethod
    def get_order(self, client_order_id: str) -> "OrderStatus": ...
    @abstractmethod
    def reconcile(self) -> "ReconcileReport": ...
    # reconcile() = pull broker's truth (positions + open/closed orders) and diff it against
    # our local journal. Returns drift: orders we think are open that the broker filled/canceled,
    # positions that don't match, fills we never journaled. This is how we catch a missed webhook
    # or a crash mid-cycle. Run it at the start AND end of every cycle.
```

Read methods are safe and freely retryable. Write methods are guarded and idempotent. `reconcile()` is the safety net that assumes our local state can be wrong and the broker is the source of truth.

### 2.2 The order lifecycle

```
  signal ──▶ risk gate ──▶ route ──▶ ack ──▶ fill ──▶ reconcile ──▶ journal
    │           │            │         │       │          │            │
 strategy    HARD stop    pick the   broker  partial/  diff broker   append-only
  output     (§2.3):      adapter    accepts  full,    truth vs.     event log;
 (intent,    sizing,      (Alpaca/   & returns multiple local       every state
  not an     exposure,    Tradier),  broker_  fills    book; fix    transition is
  command)   PDT, level,  attach     order_id  over     drift        a journal row
             buying pwr,  client_              time
             paper/live   order_id
```

Step by step:

1. **Signal** — the strategy emits an *intent* (`{symbol, side, qty, type, why}`), never a raw broker call. Signals are data; they get logged before anything acts on them.
2. **Risk gate** (§2.3) — a deterministic, mandatory checkpoint. It can **veto** (reject + journal reason) or **resize** (e.g. clamp to max position). Nothing reaches a broker without passing. If the gate is down, the answer is *no order*, not *order anyway*.
3. **Route** — pick the adapter for this asset/account, **generate the `client_order_id`** (§2.4), build the `OrderRequest`. Multi-broker routing rules live here (e.g. "options → Alpaca paper", later "size > X → IBKR").
4. **Ack** — `place_order()` returns an `OrderAck` with the broker's `order_id` and accepted/rejected status. **Ack ≠ fill.** A rejected ack is journaled and the lifecycle ends.
5. **Fill** — fills arrive async: via **webhook/stream** if available, else **poll `get_order()`**. Handle **partial fills** (§2.5) — `qty=10` may fill as `4 + 6`, or `4` and the rest expires.
6. **Reconcile** (§2.6) — diff broker truth against our journal. Catch anything the fill path missed (dropped webhook, crash). Broker wins every disagreement.
7. **Journal** (§2.6) — append-only event log. Every transition (signal, gate decision, ack, each fill, cancel, reconcile drift) is a row. The journal *is* the system of record for review and post-mortems; the dashboard reads it.

### 2.3 The risk gate (where most of the safety lives)

The gate is **deterministic Python**, not Claude. Claude can *propose*; the gate *disposes*. It enforces, at minimum:

- **Per-trade sizing cap** (max $ or % equity per position) and **per-trade max loss**.
- **Aggregate exposure caps** (gross/net, per-underlying, total option premium at risk).
- **Buying-power / margin** check against `get_account()` *before* routing.
- **Options level** check (don't send a Level 3 strategy to a Level 1 account — both Alpaca and the gate enforce; defense in depth).
- **PDT / day-trade-count** awareness.
- **Paper/live consistency:** in paper, the gate still runs identically — so the numbers you review are the numbers you'd trade. No "it's only paper, skip the checks."
- **Kill switch:** a single config/flag (and a journaled manual command) that makes the gate reject *everything*. The iPhone operator's panic button.

### 2.4 Idempotency

The single most important correctness property. **We generate a deterministic `client_order_id` for every order and the broker treats it as an idempotency key.**

- ID = a stable hash of `(cycle_id, signal_id, symbol, side, qty, intended_ts)`. The *same* logical order, retried, produces the *same* ID.
- All four brokers honor a client-supplied order id and **reject duplicates** — so a retry after an ambiguous timeout (we sent it, never got the ack) **cannot** create a second position. We resend the same ID; the broker says "already have it" and hands back the original.
- The journal is keyed on `client_order_id` too, so reconcile can always join "what we intended" to "what the broker did."

> The failure we are specifically engineering against: *network drops between `place_order()` and the ack.* Without idempotency, the safe-looking retry doubles your size. With it, the retry is a no-op.

### 2.5 Retries & partial fills

**Retries** — split by method class:
- **Read methods** (`get_*`): retry freely with exponential backoff + jitter; they're idempotent and safe.
- **`place_order`**: retry **only** on ambiguous/transport failures (timeout, 5xx, connection reset), **always reusing the same `client_order_id`** (§2.4). **Never** retry a *clean reject* (insufficient buying power, bad symbol, level violation) — that's a real answer; journal it and stop.
- **`cancel_order`**: idempotent — cancelling an already-filled/cancelled order is a safe no-op.
- Respect broker rate limits (IBKR pacing, the throttles in `02-data-sources.md` §5). Back off, don't hammer.

**Partial fills** — treat the order as a *running quantity*, never a boolean:
- Track `filled_qty`, `remaining_qty`, and a list of fill events with prices. Compute the **true average fill price** from the events, not the limit price.
- A `qty=10` order may settle as `4 @ 1.20` then `6 @ 1.22`; or `4` fills and `6` expires at DAY end. Both are normal. The position is `+4` until the rest fills.
- **Exits must reference the *actual* filled quantity.** If you open with a partial fill, the stop/target order covers `filled_qty`, not the originally requested `qty`. This is a classic way to end up accidentally net-short or over-hedged.
- Each fill is its own journal row; the position is the sum, reconciled against the broker.

### 2.6 Reconcile & journal

- **Journal** = append-only event log (SQLite table or JSONL), one row per state transition. Immutable; you correct by appending, never editing. It answers "what did the system believe, decide, and do, and when." It is the audit trail and the dashboard's data source.
- **Reconcile** = at the start and end of *every* cycle, pull the broker's positions + order statuses and diff against the journal. Any drift (broker filled an order we thought was open; a position size mismatch; a fill we never recorded) is logged loudly and, where safe, auto-corrected to **broker truth**. After a crash, reconcile is what makes restart safe: we don't trust our last-known state, we re-derive it from the broker.

### 2.7 Paper / live switching — behind the adapter, guarded

The whole point of the abstraction: **identical code path, the only difference is which adapter instance was constructed.**

- **Paper is the default.** With no explicit live flag, the factory returns `AlpacaAdapter(is_paper=True)` / `TradierAdapter(sandbox=True)`. You cannot "accidentally" be live.
- **Going live is an explicit, guarded, human action.** It requires, together: (a) `mode: live` set in config, **and** (b) a separate live-key env var present (paper and live keys are *different env vars* — see `05-security.md`), **and** (c) an interactive, journaled confirmation (typing the account's equity, or a one-time confirm token). Any one missing → stays paper.
- **`is_paper` is surfaced everywhere** — every order ack, every journal row, and the dashboard banner (green = PAPER, red = LIVE). The operator on an iPhone always knows which world they're in at a glance.
- The risk gate runs **identically** in both, so paper review is faithful to live behavior.

> **Honest caveat:** paper fills are optimistic. Sandbox/paper engines fill at or near the quote and ignore slippage, queue position, and liquidity. Treat paper P&L as an *upper bound*, and validate against the data delay you'll actually trade on (§1, and `02-data-sources.md` §5). The adapter makes paper and live *code-identical*; it does not make paper *market-identical*.

---

## 3. Repo structure & tech stack for the reborn AiStudios

### 3.1 Tech stack

| Layer | Choice | Why |
|---|---|---|
| **Deterministic core** | **Python 3.11+** | Indicators (pandas-ta/TA-Lib), risk gate, sizing, execution, reconcile, IV-rank/implied-move math. Deterministic, testable, reproducible. The part that touches money is plain code, not an LLM. |
| **Reasoning layer** | **Claude Agent SDK + MCP** (`anthropic` SDK, already a dependency) | Thesis generation, catalyst reading, "should we even look at this" triage. Calls MCP servers (Alpaca official; Perplexity/Tavily/Exa) and our REST wrappers. **Claude never places an order or holds a key directly** — it proposes; the core's risk gate disposes. |
| **State** | **SQLite** (default) → **Supabase** (optional) | SQLite is a single file: zero-ops, perfect for one operator, trivially backed up, works on a laptop or a tiny box. Tables: `journal`, `positions`, `signals`, `orders`, `runs`, `config_audit`. Upgrade to Supabase only if you need multi-device/hosted access or want the dashboard to read remotely. The data layer is abstracted so the swap is contained. |
| **Dashboard** | **Single self-contained HTML file** | Reuse the *exact* approach the prior AiStudios used for `preview.html` — one file, inlined CSS/JS, no build step, no server required. Generated by a Python command from the journal/positions. **Open it on an iPhone and review.** (See §3.3.) |
| **Config** | **YAML** (strategy params, risk limits, broker routing, mode) + **`.env`** (secrets only) | YAML is human-readable and diffable for *behavior*; `.env` holds keys and is **gitignored, never committed** (`05-security.md`). Changing risk limits is a YAML edit + a journaled config-audit row. |
| **Scheduling** | **cron + a market-hours guard** | A `cron` entry (or `launchd`/systemd timer) kicks a cycle; a deterministic `is_market_open()` check (calendar + holidays + early closes via the broker's clock API) gates whether it actually trades. One paper cycle = one process invocation. |
| **Packaging** | `requirements.txt` (or `pyproject.toml`/`uv`) | Keep it boring and installable. `pip install -r requirements.txt` and go. |

### 3.2 Repo structure

```
AiStudios/                          # reborn: trading system (was: screenplay toolkit)
├── README.md                       # what it is, the honesty clause, one-command quickstart
├── requirements.txt                # anthropic, pandas, pandas-ta, pyyaml, httpx, alpaca-py, ...
├── pyproject.toml                  # (optional) packaging / tool config
├── .env.example                    # NAMES of required keys, no values. Real .env is gitignored
├── .gitignore                      # .env, *.db, __pycache__, dashboard output, secrets
├── config/
│   ├── config.yaml                 # mode (paper|live), risk limits, sizing, broker routing
│   ├── strategies.yaml             # per-strategy params (entries, exits, universe)
│   └── universe.yaml               # tickers/ETFs to watch
├── cli.py                          # SINGLE entrypoint: run-cycle, dashboard, reconcile, status, go-live
├── core/                           # DETERMINISTIC PYTHON — no LLM calls, fully testable
│   ├── brokers/
│   │   ├── base.py                 # BrokerAdapter ABC + dataclasses (§2.1)
│   │   ├── alpaca.py               # AlpacaAdapter (paper default)
│   │   ├── tradier.py              # TradierAdapter (sandbox default)
│   │   ├── factory.py              # build adapter from config; ENFORCES paper-unless-guarded (§2.7)
│   │   └── paper_guard.py          # the live-mode confirmation gate
│   ├── data/
│   │   ├── store.py                # SQLite (or Supabase) data layer — journal, positions, orders
│   │   └── clients/                # thin REST wrappers (Tradier chains, Finnhub, FINRA, ApeWisdom...)
│   ├── indicators.py               # pandas-ta wrappers: RSI, MACD, ATR, ... deterministic
│   ├── options_math.py             # IV rank, IV percentile, implied move, straddle pricing
│   ├── risk.py                     # THE RISK GATE (§2.3) — sizing, exposure caps, PDT, kill switch
│   ├── execution.py                # order lifecycle orchestrator (§2.2): route→ack→fill→reconcile
│   ├── reconcile.py                # broker-truth diff vs. journal (§2.6)
│   └── journal.py                  # append-only event log writer
├── strategies/
│   ├── base.py                     # Strategy interface: produce Signal[] from data
│   └── example_covered_call.py     # one honest, simple, documented starter strategy
├── agent/                          # REASONING LAYER — Claude + MCP
│   ├── mcp_config.json             # registered MCP servers (Alpaca paper, Tavily, ...) — no secrets inline
│   ├── tools.py                    # Claude tool defs that wrap core/ (read-only + propose-signal)
│   └── triage.py                   # Claude pass: thesis/catalyst, returns proposals (never orders)
├── dashboard/
│   ├── build.py                    # reads journal/positions → emits ONE self-contained HTML file
│   ├── template.html               # inlined CSS/JS skeleton (reuses prior preview.html approach)
│   └── out/dashboard.html          # generated artifact (gitignored) — open on iPhone
├── scripts/
│   ├── run_paper_cycle.sh          # what cron calls; wraps `python cli.py run-cycle`
│   └── install_cron.sh             # installs the market-hours-guarded schedule
├── tests/
│   ├── test_risk.py                # the gate MUST be tested — sizing, caps, kill switch, paper/live
│   ├── test_adapter_contract.py    # contract tests every BrokerAdapter must pass (Alpaca + Tradier)
│   ├── test_idempotency.py         # same client_order_id never double-places (§2.4)
│   └── test_partial_fills.py       # running-qty accounting, exits sized to filled_qty (§2.5)
└── docs/
    ├── 02-data-sources.md          # (exists) data providers, MCP-vs-REST, free tiers
    └── 06-execution-and-stack.md   # (this file) brokers, adapter, repo + stack
```

### 3.3 How it stays iPhone-friendly and simple to operate

The operating model is one command in and one HTML file out — the same lightweight, mobile-first ergonomics the prior AiStudios had (`aistudios.py` CLI + self-contained `preview.html`), now pointed at trading.

**One command to run a paper cycle:**

```bash
python cli.py run-cycle          # paper by default. fetch → indicators → risk gate →
                                 # (optional Claude triage) → route → ack/fill → reconcile → journal
                                 # then auto-regenerates dashboard/out/dashboard.html
```

That single invocation is also the unit `cron` schedules. The market-hours guard means a cron firing outside RTH is a cheap no-op, not a bad trade.

**One file to review (on the phone):**

```bash
python cli.py dashboard          # rebuild the single self-contained HTML from the journal
```

`dashboard.html` is **one file, no server, no build, no dependencies** — inlined CSS/JS. AirDrop/host/sync it and open in mobile Safari. It shows: a giant **PAPER (green) / LIVE (red)** banner, current positions + P&L, today's signals and the risk-gate decision for each (incl. *vetoes* and *why*), recent fills and partials, and any reconcile drift. Review is reading, not operating.

**Other commands (all journaled):**

```bash
python cli.py status             # quick text summary (account, open orders, drift) — fast on a phone
python cli.py reconcile          # force a broker-truth diff right now
python cli.py kill               # flip the kill switch — gate rejects everything until cleared
python cli.py go-live            # the ONLY path to live: interactive, guarded, journaled (§2.7)
```

**Why it stays simple:**

- **One entrypoint (`cli.py`), one config language (YAML + `.env`), one state file (SQLite), one dashboard file.** Nothing to deploy, nothing to host (until you choose Supabase).
- **Paper is the default everywhere** — you have to *work* to go live, and the work is loud and logged.
- **The phone never operates blind:** review is an HTML file, going live is an explicit guarded command, and the kill switch is one word.
- **Deterministic core = reproducible:** the same inputs give the same risk decisions and the same orders, so reviewing paper output actually tells you something. Claude adds reasoning on top; it never becomes the thing standing between a signal and a fill.

---

## 4. Open decisions (for `00-overview.md` / project owner)

- [ ] Confirm starter execution: **Alpaca paper + Tradier sandbox** (recommended) — Alpaca for orders, Tradier for free Greeks/IV chains and a second adapter.
- [ ] Fill delivery: **webhook/stream vs. poll `get_order()`** per broker (Alpaca stream is good; sandbox/paper varies). Default to polling for simplicity, add streams if latency matters.
- [ ] State backend: **SQLite** (recommended for one operator) vs. **Supabase** (only if you need hosted/multi-device).
- [ ] Scheduler: **cron** (simplest) vs. systemd/launchd timer. Either way, the **market-hours guard is in code**, not in the schedule.
- [ ] When (if ever) to add **IBKR** — defer until an edge is validated and depth/routing justify the operational weight (gateway + 2FA + one-session).
- [ ] Secrets: separate **paper vs. live env vars**, `.env` gitignored, never committed (`05-security.md`). The `go-live` command is the only thing that reads the live key.

---

*Last reviewed: 2026-06-30. Broker API features, options levels, and sandbox behavior change frequently — re-verify order-type support, paper/sandbox semantics, and rate limits against each broker's current docs before relying on anything here. This is a research/operations tool, not financial advice.*
