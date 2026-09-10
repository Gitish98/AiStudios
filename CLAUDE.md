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

**Session 2026-08-04/05 — first-trade fallout + assignment defense (see git log):**
The FIRST REAL TRADE (SPY 736/735 put debit, 1-of-10 partial fill, 2026-07-29)
plus two adversarial hunts (24 + 25 agents) drove: partial-fill ADOPTION (broker
truth wins; max_loss rescales); the five-day close_pending freeze fix (impossible
marks REJECTED not clamped — a >width value is bad quotes, not max profit; a
vanished close resolves against POSITIONS); position-SCOPED leg checks (the
whole-account bug — three reviewers found it in my own fix within hours); sim-
fallback guard (a failed IB connect must never let sim's empty book fabricate
fills or delete rows); broker_book_empty guard (paper-account reset signature);
and the **Tier 1 assignment defense**: reconcile flags ANY broker equity as
critical drift -> the cycle FREEZES that underlying (no entries, no closes) until
`python cli.py unfreeze SYM`; closes are leg-verified against the broker before
placement (none_held/partial -> freeze, never a blind reversal); expiry books
ONLY what the broker confirms (still-held legs get a REAL close, reason
expiry_close; unverifiable -> hold loudly); `core/exdiv.py` estimates ex-div
dates (3rd-Friday heuristic, honestly labeled) and force-exits short ITM calls
within 4 days of one. Dashboard: live marks/unrealized/target-progress per
position, execution-quality (slippage) panel, per-symbol decision trace, frozen
banner. First trade's slippage: intended 0.31 debit, filled 0.326 = 5.2% adverse
(measured, not modeled).

> **THE STANDING LESSONS:** (1) "0 signals" / a green cycle is a QUESTION, not a
> status. (2) Absence/ambiguity must be UNREPRESENTABLE as a confident number
> (None, never 0.0 or a clamped boundary). (3) The BROKER's book is truth; ours
> is a hypothesis — resolve against positions, which survive session rolls.
> (4) Deferred findings on the money path go live faster than you expect: the
> partial-fill finding was deferred as "not catastrophic" and hit on trade #1.

**Tier 2 DONE (2026-08-05, commit e4d8585):** closes now ESCALATE 15%/attempt
(cap 45%, floor $0.01; attempts counted from close_pending journal events; the
booked exit value stays the honest mark); `IBKRAdapter.mark_option` marks a
position's own legs in ~8s instead of a ~60-120s chain fetch (verified live) and
cron timeout is 1800s with duration_secs journaled per cycle; `early_close_et()`
skips the 15:30 cron on 13:00-ET half days; every fill records `fill_mode`
(paper|live) and both `cli.py performance` and the dashboard state the
paper-optimism caveat until live fills exist.

**Tier 3 DONE (2026-08-05, commit 225744d) — eliminated structurally, then
preflight-reviewed BEFORE deployment (12 findings confirmed against the
uncommitted diff, incl. a CRITICAL in the new guard itself — the rolling
'broker' kv could be blinded by one sim cycle; now a sticky write-once
`ever_real_broker` marker + a factory `BrokerBuild.degraded` flag):** holidays
are COMPUTED (Easter computus + NYSE rules, any year; old lists = test
fixture); live cycles outside 09:30-16:00 ET skip with exit 3 + an
`offhours_skip` journal (tz-drift alarm; half-days exit 0); a degraded-sim
cycle over a real-history store refuses to run (exit 1, `--force` = journaled
override); unconverted non-USD equity fails CLOSED to 0; flock (cron) + a
cross-platform Python lock (every entrypoint) serialize cycles;
WAL+busy_timeout on the prod DB with checkpoint-on-close so single-file backups
stay valid; one tracked position per option contract, enforced at entry
(overlap = refused, corrupt row = fail closed). VM verified: system tz
America/New_York, sticky marker seeded, mcal installed (rules are fallback).

**Session 2026-09-01 — the silent measurement (commit `9990d41`):** trade #3
(SPY 763/762 x10, filled 2026-08-31) was promoted to `open` with
`entry_slip_ps: null` while the broker held the average costs the whole time.
IB's fill feed is session-scoped, so a per-cycle cron never sees its own
execution; `_finalize_pending_entries` resolves against the POSITIONS feed, and
its `any_held` (partial) branch reconstructed the price while its `all_held`
(FULL fill — the common case) branch discarded it. Fixed with one shared
`_capture_entry_fill_from_book` plus `_backfill_missing_entry_fills`, which
retries every cycle while we hold the legs, so a miss is no longer permanent.

**The preflight review then found a WORSE bug in that fix** (20 raised, 11
confirmed, 9 refuted): `_legs_for_position` matched on strike alone — no option
RIGHT, no structure awareness — so an iron condor (whose call wing lives only in
`legs_json`) was priced off 2 legs of 4. Reproduced: a condor with ZERO slippage
produced a fabricated 0.60/sh adverse fill that PASSED `plausible_slip_ps` and
would have entered the go-live gate as MEASURED evidence. The leg spec is now
derived once (`_expected_legs`) and the capture is all-or-nothing. Also fixed:
`_position_is_held` returned a bare `False` from a tuple signature (caller's
unpack raised TypeError and aborted the whole cycle).

**NEW — `core/selfaudit.py` + `cli.py audit`.** Standing lesson #2 (absence must
be UNREPRESENTABLE as a confident number) worked perfectly here: `_usable`
refused IB's 0.0, nothing was corrupted. But a NULL is honest AND SILENT — this
sat eight days until a human read the journal. So the rule gains a second half:

> **absence must also be UNIGNORABLE.** Eight invariants, journaled every cycle
> INCLUDING when clean (an alarm that only writes on failure is indistinguishable
> from one switched off), shown on the dashboard, appended to the heartbeat body,
> and exiting non-zero so a monitor can act. Crying wolf is a bug in an alarm:
> the weekend/holiday false CRITICAL, routine one-cycle reconcile drift, and
> expired-worthless spreads counted as unmeasured exits were all fixed because an
> audit that is never clean is an audit nobody reads.

Trade #3's price was recovered live: filled -0.369 vs 0.36 intended = **0.009/sh
adverse (2.5%)**, vs trade #1's 5.2%. 344 tests.

**Session 2026-09-08 — Error 10091, and what chasing it found (commit below):**
~2,330 `Error 10091` lines per cycle turned out to be LOG NOISE, proven by a
read-only probe: under `reqMarketDataType(3)` both legs of the open SPY spread
delivered bid/ask/last/close/sizes AND model greeks; the full text ends "Delayed
market data is available"; it names the one unsubscribed component (the ETF's
home-exchange real-time top-of-book — ARCA for SPY/DIA/IWM/XLF/XLE, NASDAQ.NMS
for QQQ/XLK), which is deliberate (docs: paper-trade the delay you'd trade live
on). Cost was the disk: 1.1 MB/cycle, cron.log at 83 MB, no rotation (fixed:
logrotate weekly x8 with `su trader trader`; `core/ib_noise.py` now counts the
notices — first of each code still prints every cycle, unclassified codes stay
loud, census journaled with cycle_end and printed as one CLI line).

**The real finding:** the LIVE factory read paper's `ibkr_market_data_type` key
(3 in every real config); the documented "live default = 1" was dead code, and
none of the five go-live gates looked at data. Arming live would have priced
live LIMIT orders off 15-minute-stale option mids with the gate all green —
lesson #4 exactly. Now: a SIXTH gate `market_data` requires an explicit,
separate `ibkr_live_market_data_type: 1` (fail-closed); the factory reads only
that key; `selfaudit` re-asks it every cycle (`live_on_delayed_data`, CRITICAL,
gated on ARMED-NOW — config mode + arming kv — because the store's `mode` kv is
the LAST COMPLETED cycle's adapter and went stale after `go-paper`).

Preflight review (9 agents): 3 confirmed, all low, all fixed; 3 refuted on
reachability but acted on anyway — the census label now states the data type,
because under type 1 the same codes mean "NOT delivered", the opposite of
"expected". Heartbeat: healthchecks.io LIVE (cron `0 10 * * 1-5`
America/Toronto, grace 2h), email path tested end-to-end incl. a deliberate
failure; `.healthcheck_url` was documented as gitignored but never was — fixed.
362 tests.

**Session 2026-09-08 (later) — the November tier-1 plan, and the ramp that had no
evidence (commit below):** operator decision: **November = tier-1 live
VALIDATION** ($250/position, 2 positions), NOT full deployment; "the money"
follows evidence. Preparing for it found: `ramp_advancement_status` had NO
CALLER — nothing computed `clean_days`, nothing computed live-only metrics,
`ready_to_advance` was surfaced nowhere; the docs' "graduated only" was enforced
by no code; and the factory built `GoLiveGate(config)` WITHOUT the store.

Built: `core/ramp.py` (evidence engine), gate **#7 `graduated`** (tier 1 passes
as validation; tier ≥2 needs a graduated paper record AND tier N−1's live
evidence, fail-closed without a store), `cli.py ramp` (the seven gates read-only
+ evidence), a dashboard *Live ramp* card, docs/05 §1.3–1.4, docs/00, docs/07,
and **docs/10 §10 — the operator's November checklist** (the one-session wall,
entitlements on the LIVE login, nine steps no model can do).

The first ramp.py was reviewed by 20 agents: **14 confirmed, every one an
OVERSTATEMENT of evidence** — tier attribution by first-arming date (10 clean
tier-1 days counted as tier-2's), timeout-killed cycles counted as clean days,
kill-engaged and frozen days counted as clean, `--asof` replays padded the
count, defaults instead of the configured cost model, a swallowed journal read
== "zero breaches", the auto-kill path never journaled, and the factory/store
gap that made tier ≥2 unreachable while `go-live` printed LIVE ARMED. All fixed,
each with a test. Rules now: evidence is attributed PER DAY to the tier ARMED
that day; a day counts only if every live cycle completed with kill off and
nothing frozen and asof == its own ET date; unreadable evidence is reported as
unavailable, never as clean. 388 tests.

> **STANDING LESSON (5):** an evidence engine's failure modes all point one way —
> optimistic. Review it for OVERSTATEMENT specifically, and make every
> "could not read" an explicit "unavailable", never a zero.

**Session 2026-09-09/10 — the execution report, and a partial close that would
have vanished (commit below):** the 10:00 cycle closed the SPY 763/762 x10 at
21 DTE (`manage_at_dte`, limit 0.39 = the mark); **6 of 10 filled at exactly
0.39** in three tranches, the DAY remainder cancelled at the close. The order
feed reported `Cancelled, filled 0.0` (session-scoped). The partial-close
finalizer would have resized to 4 and DISCARDED six contracts of realized P&L
("no recoverable fill price"). But **`reqExecutions()` is ACCOUNT-scoped for
the day**, across client ids: all nine fills were there with prices. The
"IB's fill feed is session-scoped" belief was true of `trades()` and false of
the execution report. Raw fills were hand-journaled that night
(`executions_snapshot`) before the reset; the finalizers now consult the report
(live + journaled snapshots, deduped), both finalizers measure fills the feed
reports at 0.0, and a partial close BOOKS its closed units
(`store.split_closed_units`, `"<parent>#partN"`) when the report's quantity
agrees with the broker's book.

**Two review rounds, 22 confirmed, all fixed** — every one an OVERSTATEMENT or
a wrong-size order (lesson 5): executions were keyed by coid alone while a
close's coid is REUSED by every re-placement (the remainder's re-close would
have netted the first tranche's fills — now scoped to the attempt's IB
`order_id`); the feed can list a dead attempt AND a live one under one coid and
last-wins picked the dead one, releasing the live close and placing a THIRD
(`_orders_by_attempt` prefers the current attempt); the DEAD-order branch
ignored partial fills and would have sent a 10-lot close against 4 held
(dead + not all held => resolve by the book — THE 10:00 ET CASE); zero-price
snapshot rows diluted the average (refused); the per-cycle snapshot missed the
15:30 cycle's own late fills (`cli.py snapshot-executions`, cron 17:00 ET);
`plausible_slip_ps(0.0)` read a perfect fill as NO DATA (fixed) — which then
made the SIM's by-construction 0.0 "measured" and the backtest cost-blind
(`fill_mode="sim"` stamped, never evidence); split rows inflated the trade
count (grouped by parent, metrics AND dashboard); a HIGH audit paged every
cycle and would have masked real failures (CRITICAL pages, HIGH rides);
unbooked partials had no retry (`_retry_unbooked_partials`) and no alarm
(`unbooked_partial_close`, HIGH 15 days). Also: on-box books backup with
rotation + off-box rclone hook (`scripts/backup_books.sh`, cron 17:30 ET),
`disk_low` audit invariant, heartbeat pings now log their HTTP result.

Rehearsed on a copy of the VM's real store before deploy: Day 2 books 6 @0.39
and shrinks to 4; Day 2 PM leaves the live re-close alone beside its dead
sibling; Day 3 measures the remainder at 0.33, not a blend. 420 tests.

**Next, in priority:** (1) ~~activate the heartbeat~~ DONE 2026-09-08;
(2) SPY/QQQ/IWM/DIA now have real 252-day IV ranks, so the paper ORDER path can fire
as soon as a credit-ratio-worthy spread appears — watch for the first fill and verify
the fill/slippage capture end-to-end; (3) **operator live-validation at tier 1 — November 2026; checklist in docs/10 §10; `ais ramp` shows how close**; (4) XLK/XLF/XLE still bootstrap locally (~43 more sessions); (5) sector caps
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
