# CLAUDE.md — project state & working agreement

> Read this first. It's the handoff: where the project is, the non-negotiables,
> and how we work. Source of truth is **git** (committed code + docs), not any
> chat session.

## What this is
**AiStudios** — an agentic, **paper-first** options trading system for US equities/
ETFs (Ontario operator, **IBKR**, non-registered **margin** account, defined-risk
only). Claude reasons; deterministic Python owns the math, risk, and execution.

**Ethos (do not drift from this):** honest over optimistic. It is a *bounded-risk
research tool, not a money printer*. Most retail algos lose after costs. We bound
losses; we never promise gains. Nothing here is financial/legal/tax advice.

## Non-negotiable invariants
1. **The deterministic risk gate (`core/risk.py`) is the only path to an order, and is non-bypassable.** It fails closed.
2. **Defined-risk only.** No naked/undefined-risk positions, ever.
3. **The LLM advisor (`agent/advisor.py`) is veto-only and read-only.** It can drop a gate-approved trade or lower its size (clamp [0,1]); it can NEVER add a trade, raise size, place an order, or touch the gate/kill switch. Off by default.
4. **Paper is the default.** `cli.py go-live` is disabled; live needs a multi-gate ramp (not built yet).
5. **Never commit secrets.** `.env`, `config/config.yaml`, `data/`, `exports/`, `dashboard/out/` are gitignored.
6. **Fill-aware lifecycle:** positions are `open` only when filled, `closed` only when filled; `pending` = working order (counts for risk, not expected at broker by reconcile).
7. **Test everything that touches money/risk.** 220 tests pass via `python -m tests.run` (also pytest-compatible). Add a regression test for every fix.

## Architecture (key files)
- `core/risk.py` — the gate. `core/execution.py` — the cycle (manage → size → gate → advisor veto → fill-aware place → reconcile). `core/manage.py` — exits/management + pending-close finalizer. `core/positions.py` — structure-aware P&L/exits (4 verticals + iron condor). `core/sizing.py` — fixed-fractional sizing. `core/costs.py` + `core/performance.py` — cost model + metrics + graduation gate. `core/reconcile.py` — broker-truth diff. `core/store.py` — SQLite state.
- `core/brokers/` — `base.py` (interface), `sim.py` (deterministic, no keys), `ibkr.py` (real, paper), `alpaca.py`, `factory.py` (paper-only, sim fallback).
- `strategies/` — `premium_harvest.py` (put credit spreads), `breakout.py` (debit verticals), `earnings.py` (iron condors).
- `core/data/` — DataProvider interface + Finnhub/Tiingo/FMP/AlphaVantage + DataHub + research brief. `core/data/iv` bootstrap lives in store/execution.
- `agent/advisor.py` — the veto-only reasoning layer.
- `backtest.py` — replays the REAL cycle over history (cost-aware, modeled option prices).
- `cli.py` — status / run-cycle / dry-run / dashboard / reconcile / performance / export-trades / kill / go-live (guarded, five-gate) / go-paper.
- `core/golive.py` — the five-gate `GoLiveGate` (the fail-closed paper→live path).
- `docs/00`–`13` — full documentation. **`docs/09` (home setup) + `docs/10` (cloud VM) + `bootstrap.py` are the IBKR connect path.**

## Run it
```bash
python bootstrap.py            # one-time: config from templates, deps, tests, status
python -m tests.run            # the suite (expect: all pass)
python cli.py run-cycle        # one paper cycle (sim until IBKR Gateway is connected)
python cli.py performance      # net-of-cost stats + graduation status
```

## Status (v0.1-platform)
**Done & tested:** 3 strategy engines; non-bypassable gate; fill-aware lifecycle;
management/exits; reconcile; kill switch; IV-rank bootstrap; data-provider layer;
cost-aware backtest; position sizing; veto-only advisor; performance tracker +
tax export; pro dashboard; **the guarded five-gate go-live ramp (`core/golive.py`)
— built by hand, adversarially reviewed (Workflow), paper-validated**; Windows/
UTF-8 portability fixes (bootstrap/cli/config read on cp1252 consoles). **40+ bugs
killed across ~10 adversarial review rounds.**

**NOT done / honest gaps:**
- **Live is untested** — the go-live ramp is built + reviewed but only the operator
  can exercise the REAL live order path on a live IBKR Gateway. Shipped paper-
  validated, NOT live-validated. This is the #1 next step.
- **IBKR paper connect: ACHIEVED, data-validated, and DEPLOYED on an always-on
  cloud VM** (DigitalOcean, Ubuntu 24.04, ~$12/mo). IB Gateway runs headless via
  IBC (systemd + Xvfb, auto-login, survives IBKR's nightly restart); AiStudios runs
  in a venv on this branch with a cron (`0 10 * * 1-5` ET) firing a paper cycle each
  weekday. Account (CAD-base → USD), option chains, greeks/IV all flow with zero
  data errors; day-1 IV snapshots banked. Fixed live: CAD-base `get_account`,
  unqualified-contract filter, NaN-safe field parsing, delayed market-data default.
  Reach it: `ssh trader@<vm-ip>` then `ais status` / `ais kill`. Full runbook:
  docs/10. **Order placement path not yet exercised** — 0 signals until IV rank
  bootstraps (see below).
  - **IBKR gotcha (operational):** market data is served to only ONE session at a
    time — a phone app / web Client Portal / 2nd Gateway causes `Error 10197`
    ("competing live session") AND an "Existing session detected" login tug-of-war.
    Keep exactly ONE login = the VM's Gateway. Paper uses DELAYED data (free).
- IV rank needs **~20 sessions** to bootstrap on a real broker; earnings/news need
  a provider key (else those engines stand aside — they warn loudly).
- Backtest option prices are **modeled (BS)**, not real fills — necessary, not
  sufficient.
- Deferred: sector/correlation caps (need a sector map); options-flow data; auto
  ramp-tier advancement (today `ramp_advancement_status` only REPORTS readiness;
  the human edits the tier — by design).

**Next, in priority:** (1) let the VM's daily cron accrue ~20 cycles so IV rank
bootstraps and premium_harvest starts signalling (then the paper ORDER path gets
exercised autonomously); (2) operator live-validation of the ramp at tier 1
(smallest size); (3) sector caps; (4) more data (Polygon/options flow, an earnings
key for earnings_vol); (5) tighten the VM API bind to localhost, passphrase the SSH
key before live.

> **NEXT SESSION START HERE:** branch **`aistudios/phase4-ibkr-live`**. Three things
> are DONE this phase: the five-gate go-live ramp (`core/golive.py`, factory unlock,
> risk ramp cap, `cli.py go-live`/`go-paper`, docs/05 §1.4); the data-validated IBKR
> **paper** connect; and a full **autonomous cloud-VM deployment** (headless Gateway
> via IBC + weekday cron; docs/10; `ssh trader@<vm-ip> ais status`; 220 tests). Live
> is UNtested. Next is passive: the VM banks IV history daily (~20 sessions) until
> signals/orders fire on their own, then a deliberate hand-run live-validation at
> ramp tier 1 — never let an LLM satisfy a go-live gate. Keep exactly ONE IBKR
> session logged in (the VM's) or Error 10197 returns.

## How we work (conventions)
- **Branches:** descriptive, searchable. `aistudios/<area>-<short-desc>` (e.g.
  `aistudios/phase4-ibkr-live`). Milestones get an annotated **tag** (`v0.1-platform`).
- **Back up always:** commit + push after every meaningful step. Work lives in git.
- **Commits:** explain the *why* and the honest caveats, not just the what.
- **Quality bar:** for risk-bearing changes, build → adversarially review (Workflow
  fan-out) → fix → lock with a regression test. This caught 40+ real bugs.
- **Division of labor:** agents/workflows build *isolated* modules in parallel;
  anything touching order flow or the gate is integrated by hand and reviewed.

## Agents across sessions (important)
Background agents/**workflows do NOT survive into a new session** — they run inside
the current session's runtime, and this is an **ephemeral remote container** that's
reclaimed on inactivity. A fresh session starts with **no live agents running**.
What *does* carry over: all committed code, docs, and this file. So a new session
re-reads CLAUDE.md + the docs and **re-deploys** whatever agents the next task
needs. Nothing is lost; agents are simply re-launched, not resumed.
