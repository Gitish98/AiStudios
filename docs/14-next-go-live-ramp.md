# 14 — NEXT TASK BRIEF: the guarded go-live ramp

> This is the launchpad for the next session. The goal is the **guarded path to
> LIVE** trading on IBKR — currently `cli.py go-live` is hard-disabled and the
> factory refuses to build a live adapter. This is the most safety-critical build
> in the project. Do it on branch **`aistudios/phase4-ibkr-live`**, review it
> adversarially (Workflow fan-out), and lock every gate with a test.

## The cardinal rule
**An LLM must be able to satisfy NONE of the go-live gates.** Every gate is an
out-of-band human action (a key, a file, a typed phrase, a config flag). Code +
the LLM can *propose*; only the human, through these gates, can *enable live*.
Paper stays the default; the deterministic risk gate still binds in live.

## The five independent gates (ALL required, together)
1. **`mode: live`** in `config/config.yaml` (one condition; does nothing alone).
2. **A separate LIVE credential**, different from paper — for IBKR that's the
   Gateway on the **live port 4001** (paper is 4002) with a live-logged-in
   session; the adapter must verify it is NOT the paper endpoint.
3. **A dated `LIVE_ENABLED` file** written out-of-band by the human (e.g.
   `~/.aistudios/LIVE_ENABLED` containing today's date) — the LLM has no tool to
   write it.
4. **A per-session typed confirmation** — `cli.py go-live` prompts for an exact
   phrase the operator must type; no default, no bypass.
5. **A ramp tier** — `golive.ramp` in config: live starts at the SMALLEST size
   (e.g. 1 contract / a hard `max_live_notional`) and only steps up after a
   documented number of clean, reconciled, net-positive live sessions.

## What to build
- `core/golive.py` — `GoLiveGate` that evaluates all five conditions and returns
  `(allowed: bool, reasons: list)`. Fail-closed; any missing condition → not live.
- `core/brokers/factory.py` — allow a LIVE adapter **only** when `GoLiveGate`
  passes; otherwise keep the current paper-only refusal. The IBKR adapter's
  existing `paper=False` guard gets unlocked *only* through this path.
- `core/risk.py` — when live, enforce the **ramp notional cap** as an additional
  hard limit (smallest tier until graduated).
- `cli.py go-live` — replace the disabled stub with the interactive, journaled
  gate check: show exactly which of the five are satisfied/missing, run the typed
  confirmation, and never flip live unless all five pass. Add `cli.py go-paper`
  to step back down instantly.
- Tie graduation (`core/performance.graduation_status` on **live** trades) to the
  ramp tier advancement.

## Agent plan for the new session
1. **Inline first:** read `core/brokers/factory.py`, `core/brokers/ibkr.py`,
   `cli.py go_live`, `docs/05-risk-and-safety.md §1`, `core/risk.py`. Confirm the
   current live-refusal points.
2. **Build by hand** (this touches order flow + the gate — do NOT delegate the
   integration): `core/golive.py`, the factory unlock, the risk ramp cap, the CLI
   flow. Test each gate in isolation AND that missing-any-one keeps it paper.
3. **Adversarial review (Workflow):** can the LLM satisfy any gate? Can live be
   reached with a gate missing? Does the ramp cap actually bind? Does go-paper
   always work? Fix everything confirmed; add a regression test per fix.
4. **Honest docs:** update `docs/05` and the README go-live section to match the
   implementation, and note which gates are true security boundaries vs. UX.

## Definition of done
All five gates implemented + tested; live is unreachable unless all pass; the
ramp cap binds in live; `go-paper` is one command; adversarial review clean;
README/docs match. Still: **live remains untested until the operator runs it on a
real IBKR live Gateway** — ship it paper-validated, not live-validated.
