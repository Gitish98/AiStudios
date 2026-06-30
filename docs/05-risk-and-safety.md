# 05 — Risk & Safety

> **Scope:** US equities, ETFs, and **options**. Not crypto.
> **Architecture:** Claude + MCP/REST is the *reasoning layer*; a deterministic Python core does the math, the risk checks, and the execution. **Every guardrail in this document lives in the deterministic core, not in the prompt.** A reasoning layer can *propose*; only deterministic code *disposes*.
> **Default mode:** PAPER. Live trading is a separate, explicit, multi-step human action. **An LLM can never flip this switch** (see §1).
> **Honesty clause:** This is a research/edge tool, not a money printer. Most retail algos underperform buy-and-hold after costs, slippage, and taxes. Nothing in this document is financial, legal, or tax advice — consult licensed professionals. The job of everything below is not to make money; it is to **prevent a bad day from becoming an unrecoverable one.**

---

## 0. The one principle this whole document enforces

> **The LLM is an advisor with no hands.** It can rank ideas, draft orders, and explain reasoning. It cannot move money, cannot change its own risk limits, cannot enable live trading, and cannot disable the kill switch. Those are all **deterministic code paths gated on human-controlled state.**

If you remember nothing else: **the dangerous capabilities are not given to the model.** They are given to functions the model can *call*, and those functions refuse unless deterministic preconditions (env flags, config, human confirmation tokens) are met. The model's output is always *input to a validator*, never a command that bypasses one.

Two failure modes we are explicitly designing against:

1. **Prompt-driven catastrophe** — Claude, via hallucination, jailbreak, bad data, or a manipulated tool result, decides to YOLO the account. The guardrails must hold even if the model is fully compromised.
2. **Silent drift** — no single event, but position sizing creeps, correlation stacks up, and one ordinary market gap wipes a quarter of the account. The hard caps must bind *before* that happens.

---

## 1. Paper-trading as default; the gate to go live

### 1.1 Paper is the default, full stop

The system **boots in paper mode** with paper-only credentials. There is no "default to live if a flag is missing" path. Missing/ambiguous config resolves to **paper**, never live. Going live is *opt-in, multi-step, and human-only*.

```python
# trading_mode resolution — deterministic, fail-safe
def resolve_mode() -> Mode:
    # Absence, typos, empty strings, unknown values => PAPER. Never live by accident.
    flag = os.environ.get("TRADING_MODE", "").strip().lower()
    if flag != "live":
        return Mode.PAPER
    # "live" requested — but that ALONE is not enough (see 1.2)
    return Mode.LIVE_REQUESTED   # not yet LIVE — must pass the gate
```

### 1.2 The multi-step gate to enable live

`LIVE_REQUESTED` is **not** `LIVE`. To actually route a live order, *all* of the following must independently be true. They are checked in deterministic code at order time, not at startup, so the gate cannot be satisfied once and forgotten:

| # | Gate condition | How it's enforced | Why |
|---|---|---|---|
| 1 | **Env flag** `TRADING_MODE=live` | Read from environment, never from a tool argument or model output | A model cannot set an env var on the host |
| 2 | **Separate live API keys present** | `ALPACA_LIVE_KEY` / `ALPACA_LIVE_SECRET` exist *and differ from* paper keys | Paper keys physically cannot place live orders |
| 3 | **Live-enable file token** | A file `~/.trading/LIVE_ENABLED` exists, written by the human out-of-band, containing today's date | Forces a deliberate, dated, filesystem action the model has no tool for |
| 4 | **Per-session human confirmation** | A typed confirmation phrase entered by the operator this session (e.g. `CONFIRM LIVE 2026-06-30`), validated against today's date | Live can't ride yesterday's confirmation; no stale "yes" |
| 5 | **Size ramp active** | Live orders are clamped to the current ramp tier (§1.3) regardless of requested size | Even an authorized human can't full-size on day one |

```python
def assert_live_allowed(confirmation: str) -> None:
    if os.environ["TRADING_MODE"].lower() != "live":
        raise LiveBlocked("env flag not set")
    if not live_keys_present() or live_keys_equal_paper_keys():
        raise LiveBlocked("live keys missing or identical to paper")
    if not live_enable_file_is_today():
        raise LiveBlocked("LIVE_ENABLED file missing or stale")
    if confirmation != f"CONFIRM LIVE {date.today():%Y-%m-%d}":
        raise LiveBlocked("session confirmation missing/expired")
    # only now may a live order proceed, still clamped by the ramp (1.3)
```

> **The LLM has no tool that sets the env var, writes the LIVE_ENABLED file, or supplies the confirmation phrase as a trusted token.** The confirmation is collected from the *operator's* input channel and compared in code; a model echoing "CONFIRM LIVE ..." in its output does not satisfy gate #4, because the validator only trusts the human input stream, not tool/assistant text. Keep these channels separate in the harness.

### 1.3 The small-size ramp

Going live is not a switch from $0 to full size. It is a **staged ramp** with explicit, deterministic tiers. You cannot advance a tier by asking; you advance by editing config after a clean review period.

| Tier | Max $ per position | Max concurrent positions | Min days clean before advancing |
|---|---|---|---|
| **Ramp-0 (paper)** | n/a | per strategy config | — (default) |
| **Ramp-1** | $250 notional | 2 | 10 trading days, no guardrail breach |
| **Ramp-2** | $1,000 notional | 4 | 10 trading days |
| **Ramp-3** | $5,000 notional | 6 | 20 trading days |
| **Ramp-N** | config cap | config cap | manual sign-off each step |

The ramp tier is a config value the **human** edits. The system reads it; it never promotes itself. A single guardrail breach (any KILL event, any reconcile mismatch) **resets the clean-day counter to zero.**

### 1.4 The implementation (`core/golive.py`) — what's wired, and which gates are real boundaries

The five gates above are implemented for **IBKR** in `core.golive.GoLiveGate`, which evaluates all five and is fail-closed (any missing/unreadable/ambiguous condition → not live). The broker factory (`core/brokers/factory.py`) is the **single construction chokepoint**: a live adapter is built *only* when the gate passes; otherwise the system stays paper, loudly, and itemizes what's missing — it **never masquerades a paper adapter as live** (if the gates pass but no live Gateway answers, it stays paper). The risk gate (`core/risk.py`) then enforces the ramp tier's notional/position caps as **hard additional limits**, and fails closed on a live run with no ramp cap.

Concrete mapping (the abstract table in §1.2 was Alpaca-era; this is the shipped IBKR form):

| # | Abstract gate | IBKR implementation | Enforced in |
|---|---|---|---|
| 1 | env/mode flag | `mode: live` in `config/config.yaml` | `GoLiveGate._check_mode` |
| 2 | separate live credential | live IB **Gateway on a live port** (4001/7496), distinct from the paper port; the adapter refuses `paper=False` against a paper port | `GoLiveGate._check_live_endpoint` + `IBKRAdapter.__init__` |
| 3 | dated enable-file | `~/.aistudios/LIVE_ENABLED`, first line = today's date | `GoLiveGate._check_live_enabled_file` |
| 4 | per-session confirmation | `cli.py go-live` reads `CONFIRM LIVE <today>` from the operator's **TTY** (refuses if stdin isn't a terminal); persists the dated phrase so same-day cron runs don't re-prompt, stale at the date rollover | `cli.py go_live` + `GoLiveGate._check_confirmation` |
| 5 | size ramp | `go_live.ramp_tier ≥ 1`; the risk gate clamps notional/positions to that tier | `GoLiveGate._check_ramp_tier` + `RiskGate` |

**Which are TRUE security boundaries vs. UX (be honest):**
- **Gate 4 is the strongest boundary.** It is interactive, date-bound, and read from the operator's terminal — *never* a CLI argument or model text — so a compromised/automated LLM cannot supply it. `go-paper` stands live back down instantly.
- **Gates 3 and 2 are strong:** an out-of-band dated filesystem write and a live-authenticated Gateway on a live port are human/broker actions the **runtime advisor LLM has no tool for** (it is read-only, sandboxed — `agent/advisor.py`).
- **Gates 1 and 5 are config the human owns** — intent signals, weaker as *standalone* boundaries. Their value is defense-in-depth: live needs **all five at once**.
- **Honest caveat on the threat model:** "an LLM has no tool for this" is true of the *runtime advisor*. A *developer-grade coding agent* with shell/filesystem access (like the one that built this) could in principle write the file or edit config — which is exactly why gate 4's human-typed, terminal-only confirmation is the linchpin, and why **live remains untested until the operator runs it by hand** on a real live Gateway.

---

## 2. Hard guardrails (deterministic, pre-trade)

These run as a **pre-trade validation pipeline**. Every proposed order — whether it came from Claude, a backtest replay, or a human — passes through *the same* gauntlet. If any check fails, the order is **rejected, logged, and surfaced**, and the model is told why. There is no override flag the model can pass to skip a check.

### 2.1 The guardrail set

| Guardrail | Default limit | Enforced on | Behavior on breach |
|---|---|---|---|
| **Max % per position** | ≤ 5% of equity (notional) | new + add orders | reject order |
| **Max portfolio heat / total risk** | ≤ 6% of equity at risk across all open stops | new orders | reject order |
| **Per-trade risk %** | ≤ 1% of equity (entry → stop distance) | new orders | reject / resize down |
| **Daily loss limit** | −3% of start-of-day equity | continuous, mark-to-market | **KILL SWITCH** (§7) |
| **Max options buying-power usage** | ≤ 30% of options BP | new options orders | reject order |
| **Defined-risk-only** | naked short options **forbidden** | options orders | reject unless high-approval flag |
| **Max concurrent positions** | ≤ 6 (lower in ramp) | new orders | reject order |
| **Per-strategy capital cap** | per-strategy $ / % cap | new orders | reject order |
| **Max contracts / order** | per-symbol fat-finger cap | every order | reject (§5) |
| **Max daily new positions** | e.g. ≤ 5 | new orders | reject (anti-runaway-loop) |

All numbers above are **defaults living in a config file the human owns**, not constants in model-visible context. The model may *read* current limits (so it sizes sensibly) but cannot *write* them.

### 2.2 Portfolio heat — the one people skip

"Heat" (a.k.a. total open risk) is the sum, across all open positions, of `(entry − stop) × size` — i.e. *what you lose if every stop hits at once.* Per-trade risk caps each position; **heat caps the whole book.** Six positions each risking 1% is 6% of equity gone if the market gaps through all your stops on the same morning — which correlated positions do (see §4). Heat is checked **before** accepting a new position, using the *new* position's risk added to existing open risk.

```python
def portfolio_heat(positions) -> float:
    return sum(max(0.0, (p.entry - p.stop)) * p.qty * p.multiplier for p in positions)

def admits_new(new_risk, equity, positions, max_heat_pct=0.06) -> bool:
    return (portfolio_heat(positions) + new_risk) <= max_heat_pct * equity
```

### 2.3 Defined-risk-only for options

Default: **no naked/undefined-risk short options.** Cash-secured puts and covered calls are defined-risk and allowed. Spreads, condors, defined-risk verticals — allowed. **Naked short calls (theoretically unlimited loss) and uncovered short puts are rejected** unless an explicit, separately-flagged high-approval mode is on (`ALLOW_UNDEFINED_RISK=1` *plus* a per-order human acknowledgment). The model cannot enable this; it is a human env flag with the same trust properties as the live gate. Default-deny.

```python
def is_defined_risk(order) -> bool:
    # max loss is computable and bounded for every leg combination
    return compute_max_loss(order) < float("inf")
```

> **Why this is non-negotiable:** a single naked short call into a takeover-bid gap can exceed the entire account. There is no stop-loss that reliably saves you across an overnight gap. Defined-risk is structural protection that doesn't depend on a fill.

### 2.4 Buying-power & margin

Options BP usage is capped well below 100% so an adverse move doesn't trigger a margin call / forced liquidation at the broker's discretion (which happens at the *worst* prices). Equities on margin: prefer cash or modest leverage; remember Reg-T and PDT (§6). The pre-trade check uses **broker-reported** buying power (reconciled, §5.4), never an internally estimated number that can drift.

---

## 3. Position sizing methods

Sizing decides *how much*, and it is where most blowups are seeded. All three methods below are implemented in the deterministic core; Claude may *suggest* which to use and feed conviction, but the **number is computed in code** and then clamped by §2.

### 3.1 Fixed-fractional (the default, and usually the right answer)

Risk a fixed fraction `f` of equity per trade (default `f = 0.01`, i.e. 1%). Size so that `(entry − stop) × qty = f × equity`.

```python
def fixed_fractional_qty(equity, entry, stop, f=0.01, multiplier=1) -> int:
    risk_per_unit = abs(entry - stop) * multiplier
    if risk_per_unit <= 0:
        return 0          # no stop distance => no trade, never "infinite size"
    return int((f * equity) / risk_per_unit)
```

Simple, robust, hard to misuse. **Start here.** Everything else must beat this on paper before it earns capital.

### 3.2 Volatility-targeting

Scale position size *inversely* to the asset's recent volatility so each position contributes a similar risk budget. Use ATR or realized vol: a high-ATR name gets fewer shares, a quiet name gets more, so a "1% risk" trade is actually ~1% regardless of how jumpy the symbol is.

```python
def vol_target_qty(equity, price, atr, target_risk=0.01, atr_mult=2.0, multiplier=1) -> int:
    stop_distance = atr_mult * atr
    if stop_distance <= 0:
        return 0
    return int((target_risk * equity) / (stop_distance * multiplier))
```

This is just fixed-fractional with the stop defined by volatility — which is cleaner, because the stop is set by the *market's* noise, not a round number.

### 3.3 Kelly-lite — and why you fraction Kelly down hard

Full Kelly maximizes long-run growth *if your edge estimates are exactly right.* They are never exactly right. Kelly's downside is brutal: it produces gut-wrenching drawdowns (a full-Kelly bettor routinely sees 50%+ drawdowns), and **overestimating your edge makes Kelly bet too big, which compounds losses fast.** Because retail edge estimates are noisy and usually optimistic, full Kelly is closer to a blowup recipe than a growth formula.

So we use **Kelly-lite**: compute the Kelly fraction, then apply a hard fraction (¼ Kelly or less) **and** cap it at the §2 per-trade limit.

```python
def kelly_lite_fraction(win_rate, win_loss_ratio, kelly_fraction=0.25, cap=0.01) -> float:
    # f* = W - (1-W)/R   (classic Kelly)
    p, r = win_rate, win_loss_ratio
    if r <= 0:
        return 0.0
    f_star = p - (1 - p) / r
    f_star = max(0.0, f_star)            # never short your own edge / never negative-bet
    return min(f_star * kelly_fraction, cap)   # fraction it down, then hard-cap
```

> **Rules for Kelly-lite:** (1) never exceed ¼ Kelly; (2) clamp by the per-trade risk cap regardless; (3) recompute `win_rate`/`win_loss_ratio` from a *meaningful* sample (dozens of trades, not 5) — with thin samples, default to fixed-fractional. Kelly with a fabricated edge estimate is more dangerous than no Kelly at all.

---

## 4. Correlation, concentration & circuit breakers

### 4.1 Correlation / concentration limits

Position-level caps (§2) are blind to the fact that NVDA, AMD, SMCI, and a semis ETF are *the same bet.* Concentration limits stop you from holding "six positions" that are really one leveraged position:

| Limit | Default | Notes |
|---|---|---|
| **Max exposure per sector** | ≤ 25% of equity | GICS sector tags |
| **Max summed \|beta\| exposure** | ≤ 1.5× equity beta-weighted | beta-weighted to SPY; long and short net |
| **Max pairwise correlation in book** | flag if any two positions > 0.8 (90d) | warn + count as one cluster for heat |
| **Max single-underlying exposure** | ≤ 8% (stock + its options combined) | options and shares on one name aggregate |
| **Max % in one ETF's holdings overlap** | watch sector ETF + its top names | don't double-count via the ETF |

Correlated positions are treated as **a single risk cluster** for heat (§2.2): if two names correlate > 0.8, their risk is added, not diversified-away. This is the single most common way a "diversified" book turns out to be one trade.

### 4.2 Circuit breakers (halt-on-anomaly)

Before *any* order and on a continuous monitor, deterministic checks halt trading when the data itself is untrustworthy. **Bad data is more dangerous than no data**, because it produces confident, wrong actions.

| Breaker | Trips when | Action |
|---|---|---|
| **Stale quote** | last quote/trade timestamp older than N seconds (e.g. 5s intraday) | block orders on that symbol; do not act on stale price |
| **Crossed/locked market** | bid ≥ ask, or zero/None bid/ask | block; quote is broken |
| **Anomalous price jump** | mid moved > X σ vs recent bars with no volume/news | quarantine symbol, require human review |
| **Wide spread** | bid-ask spread > Y% of mid | block market/marketable orders; only resting limits |
| **Gap risk at open** | overnight gap > Z% vs prior close | suppress auto-entries in first N minutes; re-evaluate stops |
| **Data-source disagreement** | two providers differ > threshold on the same quote | trust neither; halt symbol |
| **Feed outage / rate-limit storm** | provider 4xx/5xx or throttling | pause the strategy, alert, do not blind-trade |
| **Market-wide halt (LULD/SSR)** | exchange halt or short-sale restriction flag | respect it; never try to route into a halt |

```python
def quote_is_tradeable(q, now, max_age_s=5, max_spread_pct=0.01) -> bool:
    if q.bid is None or q.ask is None: return False
    if q.bid <= 0 or q.ask <= 0 or q.bid >= q.ask: return False     # crossed/locked
    if (now - q.ts).total_seconds() > max_age_s: return False        # stale
    mid = (q.bid + q.ask) / 2
    if (q.ask - q.bid) / mid > max_spread_pct: return False          # too wide
    return True
```

> Free data tiers are *delayed and throttled* (see `02-data-sources.md`). A "stale quote" breaker is not optional when your feed is 15-minute-delayed — paper-trade against the *same* delay you'll trade live on, or your backtest is fiction.

---

## 5. Order safety

Even a correctly-sized, risk-approved order can lose money to **execution** mistakes. The execution layer (`04-execution.md`) implements these; this section specifies the safety contract it must meet.

### 5.1 Order types — limit / marketable-limit, never blind market

- **Default to limit orders.** For an aggressive fill, use a **marketable limit** (limit priced through the touch by a bounded amount), never a bare market order. A market order in a thin name or a fast tape fills wherever — sometimes catastrophically.
- **Options: always limit.** Options spreads are wide; a market order on options is how you give away 10%+ instantly.
- Use `extended_hours=False` unless a strategy explicitly opts in (and then with tighter limits — overnight/pre-market liquidity is thin).

### 5.2 Slippage caps

Every order carries a **max acceptable slippage** vs the reference (decision) price. If the marketable-limit price would exceed that cap, the order is **not sent** — we'd rather miss the trade than chase. After fill, realized slippage is recorded and monitored; persistent high slippage trips a review.

```python
def price_within_slippage(ref_price, limit_price, side, max_slip_pct=0.005) -> bool:
    if side == "buy":
        return limit_price <= ref_price * (1 + max_slip_pct)
    return limit_price >= ref_price * (1 - max_slip_pct)
```

### 5.3 Fat-finger checks

Deterministic sanity bounds that catch the "extra zero" class of error — these are the errors that actually empty accounts:

- **Quantity bound:** reject orders above a per-symbol max qty / max contracts.
- **Notional bound:** reject if order notional > a hard ceiling or > the §2 per-position cap.
- **Price sanity:** reject limit prices that deviate > N% from the current mid (an obviously wrong price).
- **Side/quantity sanity:** can't sell more than held (no accidental naked short), can't buy a position you intended to close.
- **Duplicate/replay guard:** reject an identical order within a short window (loop protection — see §5.5).

### 5.4 Reconcile fills vs intent

After every order, **reconcile**: did we get what we asked for? Compare *intent* (symbol, side, qty, price band) against *actual* broker fills.

- Pull fills from the broker (source of truth), not from our optimistic local state.
- On **any** mismatch (partial fill we didn't expect, wrong symbol, price outside band, phantom/duplicate, position quantity disagreement) → **flag, halt new orders, alert the human.**
- Reconcile **positions and buying power against the broker** at startup, after every fill, and on a schedule. The broker is always the source of truth; if our model of the account disagrees with the broker's, we **stop and trust the broker.**

### 5.5 Runaway-loop protection

Because the order originator is an agent in a loop, add anti-runaway guards independent of correctness: **max orders per minute**, **max orders per symbol per day**, **idempotency keys** so a retried tool call doesn't double-submit, and a **max daily new positions** cap (§2.1). An agent stuck in a retry loop must not be able to send 500 orders.

---

## 6. Secrets management

> Detailed security architecture lives in the security doc; this section is the **risk-officer's mandatory baseline.** Nothing here is optional.

- **`.env`, never committed.** All keys in environment / a secrets manager, loaded at runtime. `.env`, `*.key`, `~/.trading/` are in `.gitignore`. **No key, token, or secret ever enters source control, logs, prompts, tool results, or model context.** Scrub secrets from any error surfaced to the model.
- **Separate paper vs live keys.** Paper keys and live keys are *different credentials* in *different env vars* (`ALPACA_PAPER_*` vs `ALPACA_LIVE_*`). The live gate (§1.2) checks they differ. Paper keys cannot reach a live endpoint; the base URL is bound to the key set.
- **Least-privilege API keys.** Scope keys to exactly what's needed. If the broker supports it, use **trade-disabled / read-only keys** for the analytics path and a separate, narrowly-scoped key for the execution path. No "withdraw funds" or money-movement permission on any trading key — ever.
- **Key rotation & revocation.** Rotate on any suspicion of exposure; document how to revoke at the broker in minutes (it's in the runbook, §8.4). Treat a leaked live key as an active incident.
- **Don't hand keys to unaudited MCP servers.** Per `02-data-sources.md`: only the **official broker MCP** or **our own audited code** ever holds broker credentials. Never a random community server.
- **Secret scanning in CI.** Pre-commit hook + CI secret scan to catch an accidental commit before it reaches the remote. Assume any key that touches a public repo is already compromised — rotate it.

---

## 7. Compliance & realism

> **This is not financial, legal, or tax advice. None of it.** The rules below are summarized for engineering awareness and change over time. **Consult a licensed broker, attorney, and tax professional** before trading real money. Verify current rules with FINRA/SEC/IRS and your broker — do not rely on this document.

### 7.1 PDT — Pattern Day Trader (margin accounts under $25k)

- FINRA's Pattern Day Trader rule flags an account that makes **4+ day trades within 5 business days** in a **margin** account. A flagged account must maintain **≥ $25,000** equity or it's restricted from further day trading.
- **Engineering implication:** if operating a margin account under $25k, the system must **count round-trip day trades in a rolling 5-business-day window** and **block** an order that would be the 4th. This is a deterministic pre-trade check, not a suggestion.
- Cash accounts avoid PDT but introduce **settlement (T+1) / good-faith-violation** constraints — you can't immediately reuse unsettled proceeds. Track settled vs unsettled cash.

```python
def would_violate_pdt(day_trades_last_5d, equity, is_margin) -> bool:
    if not is_margin or equity >= 25_000:
        return False
    return day_trades_last_5d >= 3   # the 4th in 5 business days trips PDT
```

### 7.2 Options approval levels

Brokers assign **options approval tiers** (roughly: covered calls / cash-secured puts → long options → spreads → naked/uncovered). **The system must know the account's tier and refuse any strategy above it** — both because the broker will reject it and because exceeding your approval is exactly the undefined-risk territory §2.3 forbids. Encode the account's tier in config; validate every options order against it pre-submit.

### 7.3 Wash-sale rule (tax)

- The IRS **wash-sale rule** disallows a loss deduction if you buy a "substantially identical" security within **30 days before or after** realizing that loss (and it has options implications — replacing stock with calls can trigger it). The disallowed loss adjusts the new position's cost basis.
- **Engineering implication:** the system should **flag** when a new buy would wash-sale a recent realized loss (same/related symbol within ±30 days) so the operator can decide consciously. We **track and warn**; we do not pretend to compute final tax — that's the professional's job.

### 7.4 Tax & record-keeping

- **Log everything, immutably:** every intent, order, fill, cancel, reconcile result, and guardrail decision, timestamped. This is your audit trail, your tax record, and your post-mortem evidence.
- Keep enough to reconstruct **cost basis, holding period (short vs long term), and realized P&L** per lot. Export in a form an accountant / tax software can consume. Your broker's 1099-B is authoritative — your logs exist to *check* it and to explain *why* each trade happened.

### 7.5 An honest expectations statement

> **Most retail algorithmic strategies underperform simply buying and holding a broad index**, once you account for commissions, the bid-ask spread, slippage, financing/borrow costs, taxes (especially short-term rates), data fees, and your own time. Backtests are optimistic by construction (overfitting, survivorship bias, look-ahead bias, ignoring real fills). A strategy that "works" on paper frequently does not survive live frictions.
>
> **This system is an edge-research and risk-control tool, not a money printer.** Its honest purpose is to let you *test hypotheses cheaply and safely*, with hard limits that ensure a wrong hypothesis costs you a small, bounded amount instead of your account. If you are not prepared to lose the capital you allocate, **do not go live.** The default — paper — is where this tool delivers most of its value.

---

## 8. Kill switch & incident runbook

### 8.1 What the kill switch is

A single deterministic control that, when tripped, **immediately stops new risk**: it blocks all new orders, optionally cancels working orders, and optionally flattens positions, then alerts the operator and requires an explicit human action to resume. It is enforced in code at the lowest level — the order-submission function itself checks the kill flag and refuses. **The LLM cannot clear it.**

```python
def submit_order(order):
    if kill_switch.is_tripped():
        raise Halted(f"KILL active: {kill_switch.reason}")  # no order leaves the building
    run_pretrade_guardrails(order)   # §2
    ...
```

### 8.2 What trips it

Automatic trips (deterministic monitors):

- **Daily loss limit** breached (−3% of start-of-day equity) — the primary financial trip.
- **Reconcile mismatch** (§5.4) — our state disagrees with the broker.
- **Heat / concentration breach detected post-fill** (e.g. a fill made the book hotter than allowed).
- **Data circuit breaker** persistently tripped (§4.2) — we can't trust prices.
- **Runaway order rate** exceeded (§5.5).
- **Broker / connectivity error storm** — auth failures, repeated rejects, feed down.
- **Unexpected position appears** — a position we never intended (possible compromise / bug).

Manual trip: the operator can trip it **from the iPhone** with one command/word at any time. Make the panic action trivially easy to invoke and impossible to invoke by accident in normal flow.

### 8.3 Halt → flatten/cancel → assess (the runbook)

When the kill switch trips (or you trip it manually):

1. **HALT.** New orders are already blocked by the flag. Confirm: no new risk can enter.
2. **CANCEL.** Cancel all working/open orders at the broker. Verify cancellation against the broker — don't trust local state.
3. **FLATTEN (decision).** Decide whether to close open positions:
   - **Defined-risk options & hedged positions:** often safe to *hold* (max loss is already bounded) — flattening into a panic can lock in slippage.
   - **Undefined or directional exposure / data is untrusted:** flatten with **marketable limits** (not market orders — §5.1), scaling out if size is large, accepting that a forced exit costs slippage.
   - Flatten is a *choice with a default per strategy*, written down in advance — not improvised mid-incident.
4. **RECONCILE.** Pull final positions, orders, and balances from the broker. Make local state match reality exactly.
5. **PRESERVE EVIDENCE.** Snapshot logs, the tripping condition, market data at the time, and the decision trail. This is how you learn — and how the accountant/lawyer reconstructs events if needed.

### 8.4 Secrets-incident sub-runbook

If a *key* (not a price) is the problem:

1. **Revoke the affected key at the broker immediately** (you documented how — §6).
2. Trip the kill switch; assume orders may have been placed without your intent.
3. Reconcile against the broker; flatten anything you didn't authorize.
4. Rotate all keys; scan history for the leak; close the leak before re-issuing keys.

### 8.5 How to resume

Resuming is **deliberate and human**, mirroring the live gate:

1. **Root cause identified** and written down. No resume on a mystery.
2. **Fix applied** (config, code, or a data-source swap) and, if code changed, tested in **paper** first.
3. **Reconciled clean** — local state matches the broker exactly.
4. **Human clears the kill switch** explicitly (the model cannot clear it). If a live limit was breached, consider **dropping a ramp tier** (§1.3) before re-enabling.
5. **Watch the first trades** after resume more closely than usual; a fresh-but-wrong fix is common.

> **The asymmetry is the point.** Tripping the kill switch is instant and can be automatic or one-tap. Clearing it is slow, manual, and requires a named root cause. We make stopping easy and restarting hard — on purpose. When in doubt, stay halted.

---

## 9. Defaults summary (one screen)

| Control | Default | Trips/Action |
|---|---|---|
| Mode | **PAPER** | live requires §1.2 gate + ramp |
| Per-position max | 5% equity | reject |
| Portfolio heat max | 6% equity | reject new |
| Per-trade risk | 1% equity | reject/resize |
| Daily loss limit | −3% start-of-day | **KILL** |
| Options BP max | 30% | reject |
| Naked short options | **forbidden** | reject (default-deny) |
| Max concurrent positions | 6 (lower in ramp) | reject |
| Slippage cap | 0.5% vs reference | don't send |
| Stale-quote breaker | > 5s old | block symbol |
| Sector concentration | 25% | reject |
| Kill switch (clear) | human-only | model cannot clear |

All values are **config the human owns.** The model reads them to behave; it never writes them.

---

## 10. Open decisions (for `00-overview.md` / project owner)

- [ ] Confirm the numeric defaults in §2/§9 against the actual account size — percentages are starting points, not gospel.
- [ ] Decide flatten-on-kill default per strategy (hold defined-risk vs flatten directional) — §8.3.
- [ ] Decide whether `ALLOW_UNDEFINED_RISK` is ever enabled, and under what written conditions — default is **never** (§2.3).
- [ ] Confirm account type (cash vs margin) and wire the correct constraint set: PDT (margin <$25k) vs settlement/GFV (cash) — §7.1.
- [ ] Encode the broker options-approval tier in config and the validator — §7.2.
- [ ] Define the live-ramp schedule and the "clean day" definition for tier promotion — §1.3.
- [ ] Confirm the panic/kill command surface from the iPhone (one word, hard to fat-finger by accident) — §8.2.

---

*Last reviewed: 2026-06-30. This document is an engineering risk specification, not financial, legal, or tax advice. Rules (PDT, wash-sale, options approval, margin) change — verify current FINRA/SEC/IRS/broker rules before trading real capital. The default is paper; keep it that way until an edge is proven and you can afford to be wrong.*
