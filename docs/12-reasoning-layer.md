# 12 — Reasoning Layer (the Advisor)

> Goal: explain the **Claude advisor** — what it is, the "advisor with no
> hands" / veto-only design, the hard clamps that make it safe, prompt
> caching, and the one rule it can never break. Still paper. Not
> financial/legal/tax advice.

---

## What the advisor is

The advisor (`agent/advisor.py`) is an **isolated, read-only reasoning layer**
that sits **after** the deterministic risk gate. By the time a candidate trade
reaches the advisor, it has **already been approved**. The advisor's only job
is to look at each approved candidate plus a research brief and give an opinion:
keep it, trim its size, or drop it.

That's it. It annotates. It does not act.

---

## "Advisor with no hands"

The advisor has **no hands** — no way to touch the world:

- It **never imports** `core/execution`, `core/risk`, `core/store`,
  `core/brokers`, or `cli`.
- It has **no broker, no account, no store, no order path**.
- It can only **annotate** candidates that were handed to it.

So the worst thing a confused or adversarial model can do is be *too cautious*.
It cannot place an order, cannot move money, cannot reach a broker.

---

## Veto-only design

For each candidate the model returns three fields:

- `keep` — `true` (allow) or `false` (drop). **Defaults to `true`.** The model
  may only flip it to `false`.
- `allocation_mult` — a multiplier applied to the **already-approved** size.
- `thesis` — one short sentence of reasoning.

The model can **subtract** (trim or drop) but never **add**. It cannot create a
new trade, cannot reference a candidate that wasn't passed in, and cannot raise
size.

---

## The hard clamps

Every value the model returns is re-checked in **deterministic Python after the
call** — the model is never trusted directly:

1. **`allocation_mult` clamped to `[0.0, 1.0]`.** Garbage, `NaN`, or a value
   above `1.0` all collapse to `1.0`. The model can only ever **lower** size,
   never raise it.
2. **Keep-only-removes.** `keep` starts `true`; the model may only flip it to
   `false`. It can never turn a dropped/absent candidate back on, and it can
   never add one.
3. **Fail-safe pass-through.** If the client is missing, the call raises, or the
   response is malformed, **every candidate is returned unchanged**
   (`keep=true`, `mult=1.0`, `thesis=""`). Any candidate the model omits or
   garbles is also kept unchanged.

Because the risk gate already approved these candidates and the advisor only
ever reduces exposure, **"no opinion" == pass-through == safe.**

---

## Prompt caching

The system prompt is **static and byte-stable** — no timestamps, no per-request
IDs, no varying interpolation — and is sent in a cached system block
(`cache_control: ephemeral`). This lets Anthropic **prompt caching** reuse the
heavy static instructions across calls, cutting cost and latency. Only the
small per-cycle user message (candidates + research brief) changes.

The model replies through a **structured-output tool** (`annotate_candidates`),
so we parse JSON we asked for rather than regexing prose.

---

## The one rule

> **The advisor can NEVER place an order or raise size.**

It has no broker and no order path, `allocation_mult` is clamped to `[0, 1]`,
`keep` can only go from `true` to `false`, and any failure becomes full
pass-through. By construction, the most it can do is make the system trade
**less**.
