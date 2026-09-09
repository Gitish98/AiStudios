# 00 — START HERE

> **You are holding a research tool, not a money printer.** Read the next two
> paragraphs before anything else.

**What this is.** AiStudios is an agentic system for researching and (eventually)
trading US equities, ETFs, and **options**. Claude does the *judgment* (reads
news, ranks ideas, writes a thesis). Plain, tested Python does the *math, the
risk checks, and the trading*. There is a hard line between them: **Claude
proposes; deterministic code disposes.** Claude has no tool to place an order,
change a risk limit, go live, or clear the kill switch.

**The honest expectation.** Most retail algorithms **underperform** simply buying
and holding a broad index, once you count fees, the bid-ask spread, slippage,
borrow costs, taxes, and your own behavior under real losses. Backtests and paper
curves are optimistic by construction. The real value of this system is **hard
limits that make a wrong idea cost a small, bounded amount instead of your
account.** The default mode is **paper**. If you are not prepared to lose the
capital you would allocate, **do not go live.** Nothing here is financial, legal,
or tax advice.

---

## You are operating this from an iPhone. Here is the whole job.

The ergonomic is **one command in, one summary out.** You review by *reading*, not
by operating a dashboard.

### 1. One-time setup (do this once)

```bash
# In your Claude Code session, from the repo:
cp .env.example .env                       # then fill in PAPER keys only
cp config/config.example.yaml config/config.yaml
cp config/watchlist.example.yaml config/watchlist.yaml
pip install -r requirements.txt
```

- Get **free** paper keys: Alpaca paper account (no funding needed) and a Tradier
  sandbox token. That is enough to run everything in paper.
- **Never commit `.env`.** It is gitignored. Your keys never go into source
  control, logs, or Claude's context.

### 2. Every-day loop (the only commands you need)

| Command | What it does | Why you'd run it |
|---|---|---|
| `python cli.py status` | **Plain-text summary in this chat.** Mode banner (should say PAPER), account, open positions, open orders, any reconcile drift. | Your primary phone review surface. No file transport, no browser. Start here. |
| `python cli.py run-cycle` | One paper cycle: fetch data → indicators → risk gate → (optional Claude triage) → route → fill → reconcile → journal. | Run a trading cycle by hand. |
| `python cli.py dashboard` | Rebuild the optional single-file HTML dashboard. | Only if you want the visual; `status` already tells you everything on a phone. |
| `python cli.py reconcile` | Force a broker-truth diff right now. | If `status` shows drift or after a crash. |
| `python cli.py kill` | **Panic button.** The gate rejects every order until a human clears it. | The instant something looks wrong. Tripping is easy; clearing is slow and manual — on purpose. |
| `python cli.py go-live` | The *only* path to live. Interactive, guarded, journaled. | Only after months of honest paper validation. See below. |

> **Phone tip.** `python cli.py status` prints its summary straight into the
> Claude Code conversation — that is the most reliable thing to read on a phone,
> with zero file transport. The HTML dashboard is a nice-to-have, not the path.

### 3. Reading a cycle

After `run-cycle`, look for, in `status`:
- A green **PAPER** banner. If it ever says **LIVE** and you didn't mean it, run
  `python cli.py kill`.
- Today's signals and, for each, the **risk-gate decision** — including any
  **VETO** and the reason. Vetoes are the system working.
- Positions, P&L, recent fills (including partial fills), and reconcile drift
  (there should be none).

---

## The five things the system will NOT do (and why that protects you)

1. It will **not** let Claude size a position or approve an order. Ever.
2. It will **not** trade naked/undefined-risk options. (A single naked short call
   into a takeover gap can exceed your whole account.) This is an
   **un-overridable** reject in the starter — there is no flag to turn it off.
3. It will **not** run when the market calendar says the market is closed.
4. It will **not** go live without **five** independent human-only conditions
   (env flag, separate live keys, a dated out-of-band file, a per-session
   confirmation, and a tiny size ramp). Miss any one → you stay paper.
5. It will **not** pretend a good paper curve is proof of edge.

---

## Honest limits you must internalize

- **The −3%/day kill switch does NOT protect you overnight.** It is an *intraday*
  control that watches your P&L while the market is open. The biggest real risk —
  a stock **gapping** past your stops overnight (earnings, halt, takeover, macro
  shock) — happens while that switch is asleep. Overnight risk is capped
  *separately* (see `config/config.yaml` → `overnight:` and `docs/05` §2). Treat
  an overnight options position as risking its **full defined max loss**, not an
  intraday stop the market can leap over.
- **Free data is delayed and throttled.** Paper-trade against the *same* delay
  you'll trade live on, or the paper result is fiction.
- **Paper fills are optimistic.** Paper P&L is an *upper bound* — real fills,
  slippage, and your behavior under real losses are not in the sim.
- **A strategy graduates to real money only** after ≥60 out-of-sample paper days
  **and** ≥40 closed trades **and** positive after-cost expectancy that survives a
  worst-case slippage haircut. Low-frequency strategies may need *far* more than
  60 calendar days to reach 40 trades. Most strategies should never graduate —
  that is the system working. **One deliberate exception:** live ramp **tier 1**
  ($250/position cap) is a *plumbing validation* step and is permitted before
  graduation — the untested live order path is the biggest honest gap, and a
  capped trade is how it gets tested. Tiers above 1 are gated on graduation
  **plus** the previous tier's live evidence (docs/05 §1.4, gate 7).

---

## Where to read next

1. [`docs/01-overview.md`](01-overview.md) — the one-page system overview.
2. [`ARCHITECTURE.md`](../ARCHITECTURE.md) — the master narrative
   (data → signals → decision → risk → execution → reflection).
3. [`docs/05-risk-and-safety.md`](05-risk-and-safety.md) — the guardrails. Read
   this before you ever consider going live.
4. [`docs/07-roadmap.md`](07-roadmap.md) — the staged build plan (Phase 0 → live).

---

*Last reviewed: 2026-06-30. Research/operations tool, not financial advice. The
default is paper — keep it that way until an edge is proven and you can afford to
be wrong.*
