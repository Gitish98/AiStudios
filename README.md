# AiStudios

**An agentic research + edge tool for US equities, ETFs, and options.** Claude does the
judgment; deterministic Python does the math, the risk checks, and the trading. It runs a
market-hours loop, proposes trades against three documented strategies, and gates every order
through a non-bypassable risk engine. You operate it from your phone.

> ### Read this before anything else
> **This is not a money printer.** It is a tool for testing trading hypotheses *cheaply and
> safely*. Most retail algorithms underperform simply buying and holding a broad index, once you
> account for fees, the bid-ask spread, slippage, borrow costs, taxes, and your own behavior under
> real losses. Backtests and paper curves are optimistic by construction. **The honest value here
> is hard limits that make a wrong hypothesis cost a small, bounded amount instead of your
> account.** Nothing in this repo is financial, legal, or tax advice. The default mode is **paper**.
> If you are not prepared to lose the capital you allocate, do not go live.
>
> **New here? Start with [`docs/00-START-HERE.md`](docs/00-START-HERE.md)** — a phone operator's
> orientation — then [`docs/01-overview.md`](docs/01-overview.md).

---

## 60-second mental model

Two layers, with a hard line between them:

- **Claude (the advisor with no hands)** reads news, filings, and chains; ranks candidates;
  writes a one-line thesis; reflects on yesterday's trades. It has tools to *read data* and
  *write notes* — and **no tool to place an order, change a risk limit, go live, or clear the
  kill switch.**
- **Deterministic Python (the sealed core)** computes indicators, sizes positions, enforces every
  risk limit, builds and submits orders, and reconciles fills against the broker. Same inputs →
  same decisions, every time.

The loop, once a day on a market-hours-guarded schedule:

```
pre-market scan  →  intraday signals  →  SIZING (Python)  →  RISK GATE ★ (Python, non-bypassable)
   →  [optional one-tap phone approval]  →  EXECUTION (paper by default)  →  EOD reconcile + journal
```

**Claude proposes; the gate disposes.** The only arrow into "place an order" comes out of the risk
gate. There is no other path.

> One honest nuance: the Orchestrator's per-strategy *allocation multiplier* (and a strategy's
> *quality tilt*) are LLM-influenced scalars that scale dollar size *within caps*. They are clamped
> by Python, capped in how much they can move per day, and the LLM may only *lower* allocation — it
> can never raise it beyond what deterministic realized performance supports. The portfolio-heat gate
> binds regardless. See [`docs/05-risk-and-safety.md`](docs/05-risk-and-safety.md) §2.2.

---

## Quickstart (paper mode — the only mode that runs without a deliberate, logged toggle)

> Requires Python 3.11+. Pure-Python indicators (**pandas-ta**, no system/C dependencies) so it
> installs from a phone with one command. Paper is the default; you have to *work* to go live, and
> the work is loud and logged.

```bash
git clone <this-repo> && cd AiStudios
pip install -r requirements.txt

cp .env.example .env                                   # fill in PAPER keys only (Alpaca paper, Tradier sandbox)
cp config/config.example.yaml config/config.yaml       # risk limits, mode: paper
cp config/watchlist.example.yaml config/watchlist.yaml # small, liquid universe
                                                       # .env and your real configs are gitignored — NEVER commit them

python cli.py status          # sanity-check: account, mode banner (should say PAPER), open orders
python cli.py run-cycle       # one paper cycle: fetch → indicators → risk gate → (Claude triage)
                              #   → route → fill → reconcile → journal
python cli.py dashboard       # (optional) rebuild the single self-contained dashboard.html
```

You need **no broker capital** to develop: Alpaca's paper account is free and first-class (options
enabled at Level 3); Tradier's sandbox gives free option chains. See
[`docs/02-data-sources.md`](docs/02-data-sources.md) for the full free-tier reality and
[`docs/06-execution-and-stack.md`](docs/06-execution-and-stack.md) for the brokers.

> **Honest data caveat:** Tradier *sandbox* greeks/IV are frequently null/intermittent, and Alpaca
> free options data is the *indicative* (15-min-delayed) feed. The core therefore **falls back to
> computing Black-Scholes greeks + IV from chain mids** when vendor greeks are missing — strike
> selection done off delayed/indicative quotes must be re-validated against real quotes before any
> live order. Don't treat free greeks as guaranteed or real-time.

**Never commit secrets.** All keys live in `.env` (gitignored) or a secrets manager. Paper and live
keys are *different env vars*; the system checks they differ before it will even consider a live
order.

---

## Operating it from an iPhone

The whole ergonomic is **one command in, a plain-text summary out, right here in the chat.** Review
is *reading*, not operating.

| Command | What it does |
|---|---|
| `python cli.py status` | **Primary phone surface.** Plain-text summary printed into this conversation — mode banner, account, open positions/orders, reconcile drift. Zero file transport, zero browser. |
| `python cli.py run-cycle` | Run one paper cycle (also what a scheduled job calls). |
| `python cli.py dashboard` | (Optional) Rebuild `dashboard/out/dashboard.html` — one self-contained file. The HTML is a nice-to-have; `status` already tells you everything on a phone. |
| `python cli.py reconcile` | Force a broker-truth diff right now. |
| `python cli.py kill` | **Panic button.** Flip the kill switch — the gate rejects everything until a human clears it. |
| `python cli.py go-live` | The *only* path to live: interactive, guarded, journaled (see below). |

`status` shows the **PAPER/LIVE banner**, positions + P&L, today's signals **and the risk-gate
decision for each (including vetoes and why)**, recent fills/partials, and any reconcile drift.
Optional checkpoints (configurable) push the day's strategy mix and each gate-approved order to your
phone for **one-tap approve/skip** before anything submits.

**Going live is deliberately hard.** `go-live` requires, together: `mode: live`, a *separate* live
key env var (different from paper), a dated `LIVE_ENABLED` file written out-of-band, a per-session
typed confirmation, and a small-size **ramp tier**. Miss any one → you stay paper. An LLM cannot
satisfy any of these. The kill switch is one tap to trip and slow-and-manual to clear — on purpose.
(Full gate, and an honest note on which of these are true security boundaries vs. UX:
[`docs/05-risk-and-safety.md`](docs/05-risk-and-safety.md) §1.)

> **Scheduling on a phone.** A phone-only operator has no always-on local cron. Phase 0/1 default to
> **manual** trigger (`python cli.py run-cycle`). Unattended scheduling uses the harness's managed
> recurring-task primitive (a scheduled job you approve), not a self-hosted `crontab`. The
> market-hours guard lives in code either way.

---

## The honest safety picture (read this, not just the headline)

- **The −3%/day kill switch is INTRADAY ONLY.** It watches your P&L *while the market is open*. It
  **cannot** bound an **overnight gap** — earnings, halt-then-reopen, takeover bid, macro shock — which
  is the single largest real risk to a retail options/equity book. Overnight/gap risk is capped
  *separately and explicitly* (max overnight defined-risk, max aggregate gap loss; see
  `config/config.yaml` → `overnight:` and [`docs/05`](docs/05-risk-and-safety.md) §2). Treat an
  overnight options position as risking its **full defined max loss**, not an intraday stop.
- **Naked/undefined-risk options are an un-overridable hard reject** in the starter. There is no
  `allow_undefined_risk` flag to flip. A single naked short call into a takeover gap can exceed your
  whole account; the strongest structural protection is not one env var away.
- **Gross leverage is capped on equity (≤ 1.0× for the starter), not on broker buying power.** Broker
  buying power is a *leveraged* figure (Reg-T 2:1, up to 4:1 for PDT accounts); we deliberately do not
  size to it.
- **Cash-account operators are protected from Good-Faith Violations**, not just PDT — the system
  tracks settled vs. unsettled cash and blocks/flags the trades that incur a GFV.

---

## What's in here (repo map)

```
AiStudios/
├── docs/00-START-HERE.md       ← phone operator's orientation (start here)
├── docs/01-overview.md         ← one-page system overview
├── ARCHITECTURE.md             ← the master doc: data→signals→decision→risk→execution→reflection
├── README.md                   ← you are here
├── .env.example                ← NAMES of required keys, fake placeholders (real .env is gitignored)
├── .gitignore                  ← .env, secrets, *.db, __pycache__, dashboard output
├── cli.py                      ← single entrypoint: run-cycle, dashboard, status, reconcile, kill, go-live
├── config/
│   ├── config.example.yaml     ← mode (paper|live), risk limits, overnight caps, leverage, sizing, ramp
│   └── watchlist.example.yaml  ← small, liquid universe of ETFs + large-caps
├── core/                       ← DETERMINISTIC PYTHON — no LLM calls, fully unit-tested
│   ├── broker.py               ← Phase 0: the functions execution needs (Alpaca paper). ABC + 2nd
│   │                              adapter arrive in Phase 3 when there's actually a broker to swap.
│   ├── data/                   ← SQLite store + thin REST clients (Tradier chains, …)
│   ├── indicators.py           ← pandas-ta wrappers (RSI, ATR, BBWP, …) — stable renamed schema
│   ├── options_math.py         ← IV rank/percentile (guarded), implied move, Black-Scholes greeks fallback
│   ├── risk.py                 ← THE RISK GATE — sizing, exposure, leverage, overnight, PDT+GFV, kill switch
│   ├── execution.py            ← order lifecycle: route → ack → fill → reconcile (idempotent)
│   ├── reconcile.py            ← broker-truth diff vs. journal (broker always wins)
│   └── journal.py              ← append-only event log
├── strategies/                 ← the three engines, behind one Strategy interface
├── agent/                      ← REASONING LAYER: Claude + MCP (read data / propose signals only)
├── dashboard/                  ← build.py → one optional self-contained dashboard.html (gitignored output)
├── tests/                      ← the gate, idempotency, partial fills, formula fixtures
└── docs/                       ← the design docs (see below)
    ├── 02-data-sources.md      ← providers, MCP-vs-REST, free-tier reality
    ├── 03-agent-architecture.md ← the agent roster + the judgment/arithmetic line
    ├── 04-strategies.md        ← the three engines, with formulas
    ├── 05-risk-and-safety.md   ← guardrails, sizing, overnight, kill switch, compliance
    ├── 06-execution-and-stack.md ← brokers, the adapter, repo + stack
    └── 07-roadmap.md           ← the staged build plan (Phase 0 → guarded live)
```

## The three strategies (all defined-risk where options are involved)

1. **Volatility-Compression Breakout** — find coiled stocks (Bollinger/Keltner squeeze, ATR
   contraction, volume dry-up) and ride the expansion with a hard ATR stop.
2. **Premium Harvesting** — systematically sell *statistically rich* option premium (IV rank ≥ 0.30)
   as defined or covered risk, managed mechanically (close at 50%, exit/roll at 21 DTE). Never naked calls.
3. **Earnings-Vol Plays** — defined-risk bets around scheduled earnings, comparing the implied move
   to the historical realized move, harvesting IV crush with iron condors or buying cheap vol.

Details and formulas: [`docs/04-strategies.md`](docs/04-strategies.md).

---

## Current status

**Design-complete; implementation staged.** The design docs (`docs/02`–`docs/06`), the master
`ARCHITECTURE.md`, and the orientation docs (`docs/00`, `docs/01`) are written. The build follows
[`docs/07-roadmap.md`](docs/07-roadmap.md), **deterministic-core-first**:

- **Now:** specs locked. The repo is mid-transition from its previous life (a screenplay toolkit) to
  the trading system described here. Module paths above are the target layout.
- **Next (Phase 0):** a paper-trading MVP — **one** defined-risk strategy, **one** broker (Alpaca
  paper), **two** data providers (Alpaca + Tradier sandbox), manual trigger, the risk gate,
  idempotency, reconcile, and the `status`/dashboard surfaces. Shippable and testable on its own.
- **Then:** scheduler + market-hours guard → a single Claude triage pass → the second and third
  strategies + the broker-agnostic adapter and a 2nd broker → and only after an out-of-sample paper
  period (≥60 trading days **and** ≥40 closed trades) with positive after-cost expectancy that
  survives a worst-case slippage haircut, a **guarded, small-size** live ramp.

A green paper curve is *evidence to keep researching*, not proof of edge. Slippage, real fills,
taxes, and your behavior under real losses are not in the paper sim. Most strategies should never
reach live — that is the system working.

---

*Last reviewed: 2026-06-30. This is a research/operations tool, not financial advice. Verify current
broker, options-approval, PDT, wash-sale, and data-provider rules before risking real capital. The
default is paper — keep it that way until an edge is proven.*
