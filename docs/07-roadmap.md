# 07 — Roadmap (Staged Build Plan)

> **Scope:** US equities, ETFs, and **options**. Not crypto.
> **Default mode:** **PAPER.** Live trading appears only in the final phase, and only as an
> explicit, guarded, small-size human action.
> **Build philosophy (from `docs/03` §13):** build the **deterministic core first and test it to
> death.** The Claude reasoning layer is an upgrade added *on top of a system that is already safe
> without it.* Each phase below is **concrete, shippable, and testable on its own** — you could stop
> after any phase and have something real and safe.
> **Honesty clause:** This is a research/edge tool, not a money printer. A phase "ships" when its
> exit criteria pass, not when the code merely runs. Nothing here is financial advice.

**Companion docs:** [`ARCHITECTURE.md`](ARCHITECTURE.md), [`docs/02`](docs/02-data-sources.md),
[`docs/03`](docs/03-agent-architecture.md), [`docs/04`](docs/04-strategies.md),
[`docs/05`](docs/05-risk-and-safety.md), [`docs/06`](docs/06-execution-and-stack.md).

---

## How to read a phase

Each phase has: **Goal**, **Build** (what you write), **Ship test** (what must demonstrably work),
and **Exit criteria** (the gate to the next phase). The thread through every phase: *deterministic
core before judgment, paper before live, small before large, loud-and-logged before automatic.*

Three rules hold across all phases:
- **Paper is the default; live requires the full §1 gate in `docs/05`.** No phase relaxes this.
- **No secrets in the repo, ever.** `.env` is gitignored from Phase 0, line one.
- **The risk gate runs identically in paper and live.** The numbers you review in paper are the
  numbers you'd trade.

---

## Phase 0 — Paper-trading MVP (one strategy, one broker sandbox, manual trigger)

**Goal:** the smallest end-to-end system that can take a real decision all the way to a paper fill,
safely, and let you review it on a phone. This is the foundation everything else bolts onto.

**Build:**
- `core/brokers/base.py` — the `BrokerAdapter` ABC + dataclasses (`docs/06` §2.1).
- `core/brokers/alpaca.py` — **Alpaca paper** adapter (read + write + reconcile). One broker only.
- `core/brokers/factory.py` + `paper_guard.py` — factory returns `is_paper=True` unless the full
  guarded live action is satisfied. Phase 0 has **no live path enabled at all.**
- `core/indicators.py` + `core/options_math.py` — only the math one strategy needs.
- `core/sizing.py` — fixed-fractional sizing (the default and usually the right answer, `docs/05` §3.1).
- **`core/risk.py` — the veto gate** (`docs/05` §2): per-trade max loss, per-position notional,
  portfolio heat, daily loss limit / kill switch, defined-risk-only, stale-quote breaker, fat-finger
  bounds. Fail-closed.
- `core/execution.py` — the lifecycle: route → ack → fill → reconcile, with **idempotent
  `client_order_id`** and **partial-fill accounting** (`docs/06` §2.4–2.5).
- `core/reconcile.py` + `core/journal.py` + `core/data/store.py` (SQLite).
- **One strategy** — start with the simplest, lowest-frequency one to keep the surface small. The
  honest starter is a single documented defined-risk strategy (e.g. **Engine B's covered call /
  cash-secured put**, `strategies/example_covered_call.py`); it exercises options plumbing without
  intraday timing pressure.
- `cli.py` — `run-cycle` (manual trigger), `status`, `reconcile`, `kill`, `dashboard`.
- `dashboard/build.py` + `template.html` — the single self-contained `dashboard.html`.
- `.env.example` (names only), `.gitignore` (`.env`, `*.db`, dashboard output), `config/*.yaml`.

**Ship test:** `python cli.py run-cycle` runs end-to-end against **Alpaca paper**: pulls data,
computes the signal, sizes it, passes it through the gate, places a paper order, handles the
ack/fill (including a partial fill), reconciles against the broker, and writes the journal. `python
cli.py dashboard` produces one HTML file showing the PAPER banner, the position, the gate decision
(including a deliberately-triggered **veto** with its reason), and any drift. `python cli.py kill`
makes the gate reject the next order.

**Exit criteria (all required):**
- [ ] `tests/test_risk.py` — sizing, every cap, the kill switch, and fail-closed paths all pass.
- [ ] `tests/test_idempotency.py` — the same `client_order_id`, resent, never double-places.
- [ ] `tests/test_partial_fills.py` — running-qty accounting; exits sized to *filled* qty.
- [ ] `tests/test_adapter_contract.py` — the Alpaca paper adapter satisfies the contract.
- [ ] A reconcile mismatch (simulated) trips the kill switch and halts new orders.
- [ ] No secret anywhere in the repo; `.env` confirmed gitignored.

---

## Phase 1 — Autonomous market-hours loop (scheduled, calendar-guarded, still paper, still one strategy)

**Goal:** the same MVP, now running itself on a schedule that *only acts when the market is open* —
without a babysitter, and without any new market risk.

**Build:**
- `core/` market-hours guard: `is_market_open()` from a US market calendar (holidays, half-days)
  cross-checked against the broker clock (`docs/03` §8, `docs/06` §3.1).
- The three **phases** of the loop wired as idempotent invocations: pre-market scan (~08:00 ET),
  intraday cycle (every N min, 09:30–15:55), EOD reconcile + journal (16:05 ET).
- `scripts/run_paper_cycle.sh` + `scripts/install_cron.sh` — a dumb daily cron; the **calendar guard
  in code** decides whether to actually run. A cron firing on a holiday is a cheap no-op.
- **Staleness guard** wired in: if data is older than threshold when a phase fires, the gate
  fails-closed and the cycle is skipped + journaled.
- **Reconcile at the start *and* end of every cycle** so a crash mid-cycle is safe to restart from.

**Ship test:** the cron-driven loop runs an unattended paper day: no-ops before/after hours and on a
holiday, runs the three phases on a normal day, and produces an EOD journal + dashboard with zero
manual intervention. Killing the process mid-cycle and restarting reconciles cleanly to broker truth.

**Exit criteria:**
- [ ] Calendar guard verified against a known holiday and a known half-day (no orders attempted).
- [ ] A forced stale-data condition skips the cycle and journals the reason (no order placed).
- [ ] Crash-and-restart mid-cycle reconciles to broker truth with no duplicate positions.
- [ ] 5 consecutive unattended paper days complete with a clean reconcile each day.

---

## Phase 2 — Reasoning layer (Claude + MCP) on top, read-and-rank only

**Goal:** add judgment — candidate ranking, theses, regime read, post-trade reflection — **without
giving the model any capability it shouldn't have.** The system must remain fully functional in a
degraded, no-LLM pass.

**Build:**
- `agent/mcp_config.json` — registered MCP servers (Alpaca paper for data; Tavily/Perplexity for
  research). No secrets inline. Only the **official Alpaca MCP** or our own audited code touches
  broker context (`docs/02` §4).
- `agent/tools.py` — Claude tool defs that wrap `core/` as **read-only + propose-signal**. There is
  **no** `place_order`, `set_risk_limit`, `go_live`, or `clear_kill` tool. Outputs are typed
  `Ranking` / `Note` / `Allocation(multiplier ∈ [0, cap])` (`docs/03` §6).
- `agent/triage.py` — the Claude pass: research summaries (cited, tagged), candidate ranking + thesis
  + confidence, and EOD reflection. **Returns proposals, never orders.**
- Orchestrator allocation: per-strategy budget multipliers clamped by Python; reflection biases
  tomorrow's allocation **within caps only.**
- **Degraded-mode path:** if the Claude layer is unavailable, run a formula-only candidate pass or
  skip the day. Fail safe, never unsafe.

**Ship test:** a paper day where Claude ranks and annotates real candidates and writes an EOD
reflection — and the *same* day re-run with the Claude layer disabled still produces safe,
formula-only behavior. A red-team prompt attempting to make Claude place or upsize an order produces
no order (no such tool exists).

**Exit criteria:**
- [ ] Capability audit: enumerate Claude's tools; confirm none can place an order, change a limit,
      go live, or clear the kill switch.
- [ ] LLM-disabled degraded run completes safely.
- [ ] Allocation multipliers are demonstrably clamped (a Claude "allocate 300%" resolves to the cap).
- [ ] Rate-limit/caching test: the agent loop does not blow the free-tier throttles (`docs/02` §5).

---

## Phase 3 — All three strategies + multi-broker adapter

**Goal:** the full strategy suite and a second real broker behind the same interface, proving the
abstraction and exercising de-confliction — still entirely paper.

**Build:**
- `strategies/` — **Engine A (Volatility-Compression Breakout)**, **Engine B (Premium Harvesting)**,
  **Engine C (Earnings-Vol Plays)**, each behind `strategies/base.py`, every parameter in
  `config/strategies.yaml` (`docs/04`).
- The remaining `core/indicators.py` / `core/options_math.py` math: BBWP, TTM squeeze, VCP, IV rank,
  implied move, straddle pricing — each unit-tested against hand-computed fixtures (`docs/04` §6).
- Orchestrator **de-confliction**: merge candidate lists across A/B/C, resolve two strategies wanting
  opposite sides of one name, enforce correlation-cluster heat (`docs/05` §4).
- `core/brokers/tradier.py` — **Tradier sandbox** adapter (free chains with Greeks/IV via ORATS) as
  a second adapter and the primary options-data source. The same contract tests run against both.
- Per-strategy expectancy tracking in the journal (win rate, avg win/loss, max DD, slippage-vs-model).

**Ship test:** a paper day where all three engines emit candidates, the Orchestrator de-conflicts
them, orders route through **both** Alpaca and Tradier behind the one adapter, and the dashboard
shows per-strategy attribution. `tests/test_adapter_contract.py` passes for both brokers.

**Exit criteria:**
- [ ] Every formula in `docs/04` §0.6/§1.2/§3.2 has a passing unit test against fixtures.
- [ ] De-confliction test: opposing same-name candidates resolve to at most one position.
- [ ] Correlation-cluster heat test: two >0.8-correlated names count as one cluster for heat.
- [ ] Both adapters pass identical contract tests; routing rules are config-driven.

---

## Phase 4 — Out-of-sample paper validation (the graduation gate, no new code path)

**Goal:** decide, honestly, whether *any* engine has earned real capital. This phase is mostly
discipline, not code. Most strategies should fail here — that is the system working.

**Build / instrument:**
- Walk-forward / out-of-sample paper period: **≥ 60 trading days** per engine (`docs/04` §6).
- Cost-aware accounting: model commissions, the bid-ask spread, and slippage; **treat paper P&L as
  an upper bound** (`docs/06` §2.7). Validate against the *same data delay* you'll trade live on
  (`docs/02` §5, `docs/05` §4.2).
- Per-engine **expectancy** report on the dashboard: `avg_win × win_rate − avg_loss × loss_rate`,
  max drawdown, and realized-vs-modeled slippage.

**Ship test:** the dashboard shows each engine's out-of-sample, after-cost expectancy and drawdown
over ≥60 trading days, reproducibly (same inputs → same numbers).

**Exit criteria (a strategy graduates *only* if):**
- [ ] ≥ 60 out-of-sample paper trading days recorded for that engine.
- [ ] **After-cost expectancy is positive.** If not, the engine does **not** advance to live — full stop.
- [ ] Realized slippage is within the modeled band (no nasty surprises hidden by optimistic fills).
- [ ] You have written down, honestly, why you believe the edge is real and not overfit. No mystery edge.

---

## Phase 5 — Guarded, small-size live (the ramp)

**Goal:** route *real* orders for a graduated strategy — at tiny size, behind every gate in
`docs/05` §1, advancing only by clean review periods. The point is not to make money yet; it is to
discover the gap between paper and reality at a cost you can afford.

**Build:**
- `core/brokers/paper_guard.py` live path: enforce **all** of (`docs/05` §1.2), checked in
  deterministic code **at order time**, not at startup:
  1. env flag `TRADING_MODE=live`; 2. separate live keys present *and different from paper*;
  3. dated `~/.trading/LIVE_ENABLED` file written out-of-band by the human;
  4. per-session typed confirmation validated against today's date; 5. size-ramp clamp.
- `cli.py go-live` — the **only** path to live: interactive, guarded, journaled. The LLM has no tool
  for any of the five conditions.
- **Size ramp** (`docs/05` §1.3): Ramp-1 ($250 notional, 2 positions, 10 clean days) → Ramp-2
  ($1,000, 4, 10 days) → Ramp-3 ($5,000, 6, 20 days) → config cap, manual sign-off each step. The
  ramp tier is config the human edits; the system never promotes itself. **Any guardrail breach
  resets the clean-day counter to zero.**
- Order-safety hardening live: marketable-limit only, slippage caps, options-approval-tier check,
  PDT counter (margin <$25k), wash-sale *warning*, separate least-privilege live key with no
  money-movement permission (`docs/05` §5–7).
- The incident runbook wired to one-tap: kill → cancel → flatten-decision → reconcile → preserve
  evidence (`docs/05` §8).

**Ship test:** `go-live` refuses unless **all five** conditions hold (demonstrate each missing
condition blocks). A live Ramp-1 order for a graduated engine places, fills, and reconciles at
$250-notional cap; the kill switch (manual and the −3% daily-loss auto-trip) halts new orders and a
human-only step is required to resume.

**Exit criteria:**
- [ ] Each of the five live-gate conditions, removed individually, blocks the live order.
- [ ] An LLM/red-team attempt to go live or clear the kill switch fails (no tool exists).
- [ ] A simulated guardrail breach resets the clean-day counter and (if a live limit) drops a ramp tier.
- [ ] A full incident runbook dry-run (kill → cancel → reconcile → evidence) completes cleanly.
- [ ] Live size never exceeds the active ramp tier, regardless of requested size.

---

## Beyond Phase 5 (deferred, only if justified by validated edge)

These are explicitly *not* prerequisites and should be added only when paper results justify the
spend or the operational weight (`docs/02` §3.2, `docs/06` §1.2):

- **Pro data** — Polygon real-time, Unusual Whales flow/dark-pool, ORATS IV analytics. Money spent
  on data is a cost your strategy must overcome.
- **IBKR adapter** — deepest universe and routing, but gateway + 2FA-restart + one-session
  constraints make it hostile to a lightweight cron loop. Defer until depth justifies the weight.
- **Supabase backend** — swap the SQLite store for hosted/multi-device access and a remote
  dashboard. Same schema, contained swap.
- **Higher ramp tiers** — only via the manual, clean-day-gated sign-off in `docs/05` §1.3.

---

## The whole roadmap on one screen

| Phase | Adds | Mode | Strategies | Brokers | Ships when |
|---|---|---|---|---|---|
| **0** | Core + gate + adapter + dashboard, manual trigger | Paper | 1 (defined-risk) | Alpaca paper | E2E paper fill + gate/idempotency/partial-fill tests pass |
| **1** | Scheduler + market-hours guard | Paper | 1 | Alpaca paper | 5 unattended paper days, clean reconcile |
| **2** | Claude + MCP (read/rank only) | Paper | 1 | Alpaca paper | Degraded-mode safe; no order-capable tool exists |
| **3** | All 3 engines + 2nd broker | Paper | A, B, C | Alpaca + Tradier | Formulas tested; de-confliction + dual-adapter pass |
| **4** | Out-of-sample validation | Paper | per-engine | — | ≥60 OOS days, **positive after-cost expectancy** |
| **5** | Guarded small-size live ramp | **Live (gated)** | tier 1: validation (pre-graduation permitted); tier ≥2: graduated + prior-tier live evidence | + live | All 7 gate conditions enforced; ramp clamps; kill works |

*Last reviewed: 2026-06-30. A phase ships on its exit criteria, not on "it runs." Default is paper;
most strategies should never reach Phase 5, and that is the system working. Research/operations tool,
not financial advice — verify current broker, options-approval, PDT, wash-sale, and data-provider
rules before risking real capital.*
