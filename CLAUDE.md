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
7. **Test everything that touches money/risk.** 261 tests pass via `python -m tests.run` (also pytest-compatible). Add a regression test for every fix.

## Architecture (key files)
- `core/risk.py` — the gate. `core/execution.py` — the cycle (manage → size → gate → advisor veto → fill-aware place → reconcile). `core/manage.py` — exits/management + pending-close finalizer. `core/positions.py` — structure-aware P&L/exits (4 verticals + iron condor). `core/sizing.py` — fixed-fractional sizing. `core/costs.py` + `core/performance.py` — cost model + metrics + graduation gate. `core/reconcile.py` — broker-truth diff. `core/store.py` — SQLite state.
- `core/brokers/` — `base.py` (interface), `sim.py` (deterministic, no keys), `ibkr.py` (real, paper), `alpaca.py`, `factory.py` (paper-only, sim fallback).
- `strategies/` — `premium_harvest.py` (put credit spreads), `breakout.py` (debit verticals), `earnings.py` (iron condors).
- `core/data/` — DataProvider interface + Finnhub/Tiingo/FMP/AlphaVantage + DataHub + research brief. `core/data/iv` bootstrap lives in store/execution.
- `agent/advisor.py` — the veto-only reasoning layer.
- `backtest.py` — replays the REAL cycle over history (cost-aware, modeled option prices).
- `cli.py` — status / run-cycle / dry-run / dashboard / reconcile / performance / export-trades / kill / go-live (guarded, five-gate) / go-paper.
- `core/golive.py` — the five-gate `GoLiveGate` (the fail-closed paper→live path).
- `core/vrp.py` + `cli.py vrp` — measures the volatility risk premium (the strategy's premise) on decades of Cboe index history.
- `core/clusters.py` — correlation clusters; SPY/QQQ/DIA/XLK share ONE risk budget.
- `core/fills.py` — realized fill/slippage capture (IB's fill feed is session-scoped: capture at fill time or lose it).
- `core/data/cboe.py` — free 252-day IV rank for SPY/QQQ/IWM/DIA from Cboe VIX-family history.
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

**Repo hygiene / OSS cleanup (2026-07-07):** the public GitHub repo now presents
honestly — description fixed (was a leftover film-scripts stub), README rewritten
to v0.1-platform reality (branch commit `c8d4343`), **MIT LICENSE** added. `main`
was a one-commit stub; PR #1 (merged) gave it the README + LICENSE only — all code
still lives on this branch. Two operational notes: (a) direct pushes to `main` are
permission-blocked — deliver via PR; (b) on the eventual phase4→main merge,
README.md conflicts trivially — take the branch version (main's copy carries a
"development lives on the branch" note + absolute links). Checked 2026-07-07: the
project does NOT qualify for Anthropic's Claude-for-OSS program (solo repo, no
ecosystem reach) — don't re-litigate without new facts.

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
- IV rank needs **60+ sessions** (`core.options_math.MIN_IV_OBSERVATIONS`) before it
  is trusted — a short window makes the rank ACTIVELY MISLEADING (in a gently
  rising regime today is the max, so it prints ~100% at a below-average IV).
  Earlier docs said ~20; that was wrong. Earnings/news need
  a provider key (else those engines stand aside — they warn loudly).
- Backtest option prices are **modeled (BS)**, not real fills — necessary, not
  sufficient.
- Deferred: sector/correlation caps (need a sector map); options-flow data; auto
  ramp-tier advancement (today `ramp_advancement_status` only REPORTS readiness;
  the human edits the tier — by design).

**Session 2026-07-24 — audit, then nine real bugs (see git log):**
A 28-agent adversarial audit + a 33-agent tooling scout (all findings verified,
15 of 19 tool recommendations killed by the challenge phase) drove a pass that
fixed, in order of danger:
1. **Fills never registered** — `_is_filled` required `filled_qty>0`, but IB reports
   `Filled` with `filled=0.0` cross-process (our cron). Positions would go invisible:
   never managed, never exited, still consuming risk budget. Six would brick it.
2. **Slippage was unmeasurable** and the data unrecoverable (session-scoped feed).
   `core/fills.py` + new position columns now capture it AT fill time.
3. **The dashboard shipped a cost-blind graduation gate** — it would have shown
   "eligible" exactly when the honest gate said no. Now delegates to core.performance.
4. **IV rank from 2 observations** — printed ~95% on real data ("sell!") when the
   honest 252-day rank said 29.7%. Floor is now 60 obs (`MIN_IV_OBSERVATIONS`).
5-8. **Four separate chain bugs**, each producing an identical healthy-looking
   "0 signals": wrong single expiry (premium_harvest could NEVER fire), 732-contract
   requests silently throttling greeks, open interest never requested (needs generic
   tick `101`), and IB's `-1.0` marketPrice sentinel emptying the chain after hours.
9. **Correlation + overnight caps** were configured and documented as enforced while
   no code evaluated them.

> **THE LESSON, now a rule:** five distinct bugs produced the same symptom — a green
> cycle reporting **"0 signals."** On this system that is a QUESTION, not a status.
> Verify it every time; do not read silence as selectivity.

**VRP MEASURED (the existential question, answered):** `cli.py vrp` over 2011-2026
(3,747 paired days) says the premium is REAL and has NOT decayed: SPY mean VRP
**+3.58%** (last 5y +3.65% vs older +3.55%), QQQ +2.90%, IWM +3.48%, DIA +3.34%;
IV exceeded subsequent RV on 77-84% of days. The audit's "decayed to zero" claim is
contradicted for this sample/measure. BUT the tail is the whole story: worst episode
was Feb 2020 at IV 14% vs RV 81% (**-67%**), and 17% of days are negative and CLUSTER
in crashes. Raw VRP in vol points is NOT P&L — costs and the spread's long wing eat
much of it, and our own cost-aware backtest still showed 57% wins with negative net
expectancy. The finding validates the DEFINED-RISK ARCHITECTURE as much as the
strategy: that -67% tail is exactly what the long wing exists to bound.

**Next, in priority:** (1) **activate the heartbeat** — `scripts/run_cycle.sh`
pings a monitor on start/success/failure but is DORMANT until a healthchecks.io URL
is written to the gitignored `.healthcheck_url` on the VM (operator action, 2 min);
(2) SPY/QQQ/IWM/DIA now have real 252-day IV ranks, so the paper ORDER path can fire
as soon as a credit-ratio-worthy spread appears — watch for the first fill and verify
the fill/slippage capture end-to-end; (3) operator live-validation of the ramp at
tier 1; (4) XLK/XLF/XLE still bootstrap locally (~43 more sessions); (5) sector caps
(needs GICS metadata) and `max_overnight_risk_at_event_pct` (needs the earnings date
in RiskContext); (6) tighten the VM API bind to localhost, passphrase the SSH key
before live. NOTE: cycles now take ~6 min (was ~90s) because chains stream with a
settle delay — well inside the 900s timeout, so no tuning needed yet.

> **NEXT SESSION START HERE:** branch **`aistudios/phase4-ibkr-live`**. Three things
> are DONE this phase: the five-gate go-live ramp (`core/golive.py`, factory unlock,
> risk ramp cap, `cli.py go-live`/`go-paper`, docs/05 §1.4); the data-validated IBKR
> **paper** connect; and a full **autonomous cloud-VM deployment** (headless Gateway
> via IBC + weekday cron; docs/10; `ssh trader@<vm-ip> ais status`; 220 tests). Live
> is UNtested. Next is passive: the VM banks IV history daily (60+ sessions) until
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
