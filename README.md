# AiStudios

**An agentic, paper-first options trading research platform for US equities and ETFs.**
Deterministic Python owns the math, the risk checks, and the execution; the LLM is a
veto-only advisor with no hands. Every order passes through a non-bypassable risk gate,
every position is defined-risk, and the default — and so far only exercised — mode is
**paper** (IBKR paper account, or a deterministic sim that needs no keys at all).

> ### Read this before anything else
> **This is not a money printer.** It is a tool for testing trading hypotheses *cheaply and
> safely*. Most retail algorithms underperform simply buying and holding a broad index, once you
> account for fees, the bid-ask spread, slippage, taxes, and your own behavior under real losses.
> Backtests and paper curves are optimistic by construction. **The honest value here is hard
> limits that make a wrong hypothesis cost a small, bounded amount instead of your account.**
> Nothing in this repo is financial, legal, or tax advice. If you are not prepared to lose the
> capital you allocate, do not go live.
>
> **New here? Start with [`docs/00-START-HERE.md`](docs/00-START-HERE.md)**, then
> [`docs/01-overview.md`](docs/01-overview.md).

---

## 60-second mental model

Two layers, with a hard line between them:

- **Deterministic Python (the sealed core)** — three rule-based strategy engines generate
  signals; `core/sizing.py` sizes them (fixed-fractional); **`core/risk.py` — the gate — is the
  only path to an order and fails closed**; `core/execution.py` runs the cycle and places orders
  fill-aware; `core/reconcile.py` diffs local state against broker truth (broker wins).
  Same inputs → same decisions, every time.
- **The LLM advisor (`agent/advisor.py`) is veto-only and read-only.** It can drop a
  gate-approved trade or shrink its size (clamp to [0, 1]). It can **never** add a trade, raise
  size, place an order, or touch the gate or the kill switch. It is **off by default**.

The daily cycle:

```
manage/exit open positions → generate signals → SIZING → RISK GATE ★ (non-bypassable)
    → advisor veto (optional, can only drop/shrink) → fill-aware placement → reconcile vs. broker
```

**The engines propose; the gate disposes.** There is no other path to an order.

---

## Quickstart (no broker keys required)

Requires Python 3.11+. With no keys configured, the broker factory falls back to a
deterministic **sim** broker, so the whole loop runs out of the box.

```bash
git clone https://github.com/Gitish98/AiStudios.git && cd AiStudios
python bootstrap.py            # one-time: config from templates, deps, tests, status
python -m tests.run            # the suite — 220 tests, expect all pass (pytest-compatible)
python cli.py run-cycle        # one paper cycle (sim until an IBKR Gateway is connected)
python cli.py performance      # net-of-cost stats + the paper→live graduation status
```

To trade against a real **IBKR paper** account, run IB Gateway and follow
[`docs/09-home-setup.md`](docs/09-home-setup.md) (home machine) or
[`docs/10-cloud-vm-setup.md`](docs/10-cloud-vm-setup.md) (always-on headless VM — the
deployed setup). **Never commit secrets**: `.env`, `config/config.yaml`, `data/`, and
`exports/` are gitignored; [`.env.example`](.env.example) documents the key names.

> **IBKR gotcha:** IBKR serves market data to only **one** login at a time. A phone app or a
> second Gateway session causes `Error 10197` and a login tug-of-war. Keep exactly one session.
> Paper uses free **delayed** data by design.

---

## Operating it

| Command | What it does |
|---|---|
| `python cli.py status` | Mode banner, account, open positions/orders, gate decisions, drift. |
| `python cli.py run-cycle` | One full paper cycle (also what the daily cron fires). |
| `python cli.py dry-run` | The cycle without placing anything. |
| `python cli.py performance` | Net-of-cost metrics + graduation-gate status. |
| `python cli.py reconcile` | Force a broker-truth diff right now. |
| `python cli.py export-trades` | Trade export (e.g., for taxes). |
| `python cli.py dashboard` | Rebuild the self-contained HTML dashboard. |
| `python cli.py kill` | **Panic button.** Gate rejects everything until a human clears it. |
| `python cli.py go-live` | The *only* path to live: interactive, five-gate, journaled. |
| `python cli.py go-paper` | Stand live back down instantly. |

**Going live is deliberately hard.** `core/golive.py` implements a five-gate ramp: config mode,
a genuinely live Gateway port, an out-of-band `LIVE_ENABLED` file, a date-bound confirmation
**typed at the terminal** (an LLM cannot satisfy it), and a small-size ramp tier that the risk
gate clamps to even when armed. Miss any gate → you stay paper. Details:
[`docs/05-risk-and-safety.md`](docs/05-risk-and-safety.md) §1.4 and
[`docs/14-next-go-live-ramp.md`](docs/14-next-go-live-ramp.md).

---

## The three strategies (all defined-risk, no exceptions)

1. **Premium Harvesting** (`strategies/premium_harvest.py`) — put credit spreads when IV rank
   is statistically rich; managed mechanically (profit-take, time exit).
2. **Volatility-Compression Breakout** (`strategies/breakout.py`) — debit verticals on squeeze
   expansion.
3. **Earnings-Vol** (`strategies/earnings.py`) — iron condors around scheduled earnings,
   implied vs. historical realized move. Stands aside loudly without an earnings data key.

Naked/undefined-risk positions are a hard reject in the gate — there is no flag to allow them.
Formulas and management rules: [`docs/04-strategies.md`](docs/04-strategies.md).

---

## Repo map

```
AiStudios/
├── cli.py                  ← single entrypoint (see table above)
├── bootstrap.py            ← one-time setup: config, deps, tests, status
├── backtest.py             ← replays the REAL cycle over history (cost-aware, modeled BS prices)
├── core/
│   ├── risk.py             ← THE GATE — the only path to an order; fails closed
│   ├── execution.py        ← the cycle: manage → size → gate → veto → fill-aware place → reconcile
│   ├── manage.py           ← exits/management + pending-close finalizer
│   ├── positions.py        ← structure-aware P&L/exits (4 verticals + iron condor)
│   ├── sizing.py           ← fixed-fractional position sizing
│   ├── costs.py            ← commission/slippage model
│   ├── performance.py      ← net-of-cost metrics + the paper→live graduation gate
│   ├── golive.py           ← the five-gate go-live ramp (fail-closed)
│   ├── reconcile.py        ← broker-truth diff (broker always wins)
│   ├── store.py            ← SQLite state
│   ├── brokers/            ← base interface, sim (no keys), ibkr, alpaca, paper-only factory
│   └── data/               ← DataProvider interface + Finnhub/Tiingo/FMP/AlphaVantage + research brief
├── strategies/             ← the three engines behind one interface
├── agent/advisor.py        ← the veto-only reasoning layer (off by default)
├── tests/                  ← 220 tests — everything that touches money/risk is tested
└── docs/00–14              ← full documentation (start at 00)
```

---

## Status (v0.1-platform)

**Done and tested:** the three strategy engines; the non-bypassable gate; fill-aware order
lifecycle; management/exits; broker reconcile; kill switch; IV-rank bootstrap; the data-provider
layer; cost-aware backtest; sizing; the veto-only advisor; performance tracking + tax export;
dashboard; the guarded five-gate go-live ramp; a validated **IBKR paper** connection deployed on
an always-on cloud VM running one cycle each weekday. 220 tests pass. 40+ bugs killed across
~10 adversarial review rounds.

**Honest gaps, in the open:**

- **Live is untested.** The go-live ramp is built, reviewed, and paper-validated — but nothing
  has ever been validated against a live order path. Paper-validated ≠ live-validated.
- **IV rank needs ~20 real sessions to bootstrap** on a fresh broker connection, so
  premium-harvest signals start at zero and accrue; the paper *order* path exercises itself
  only after that.
- **Backtest option prices are modeled (Black-Scholes), not real fills** — necessary evidence,
  never sufficient.
- Earnings/news engines need a provider key, else they stand aside (loudly). Sector/correlation
  caps are deferred.

A green paper curve is *evidence to keep researching*, not proof of edge. Most strategies should
never reach live — that is the system working.

---

*Last reviewed: 2026-07-07. Research/operations tool, not financial advice. Verify current
broker, options-approval, and tax rules before risking real capital. The default is paper —
keep it that way until an edge is proven net of costs.*
