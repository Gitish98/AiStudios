# 09 — Home Setup (15-minute plug-and-play)

> The goal: get from "I'm at my computer" to "the bot is placing IBKR **paper**
> orders" in about 15 minutes, with one command doing most of it.
>
> Nothing here touches real money. Everything is paper. Live is a separate,
> deliberate process later (docs/05 §1).

---

## Before you sit down (already done / nothing to do)
- The repo is on GitHub. Your secrets and live config are gitignored — they were
  never committed, so a fresh clone is clean.
- The system runs against a **sim broker** until a real IB Gateway is reachable,
  so every step below "works" even before you finish the IBKR part.

---

## Step 1 — Install the two pieces of software (~5 min)
1. **Python 3.11+** — https://www.python.org/downloads/ (tick "Add Python to PATH"
   on Windows).
2. **IB Gateway** (lighter than TWS) — from Interactive Brokers' site. You'll log
   in with your **paper** credentials (find/set them in IBKR Client Portal →
   Settings → Paper Trading Account).

## Step 2 — Get the code and bootstrap (~3 min)
```bash
git clone https://github.com/Gitish98/AiStudios.git
cd AiStudios
python bootstrap.py
```
`bootstrap.py` creates your local `config.yaml` / `watchlist.yaml` / `.env` from
the templates (without overwriting anything), installs dependencies, runs the
tests, and prints a status report. At this point `status` will say
**`BROKER: sim`** — expected, because the Gateway isn't up yet.

## Step 3 — Turn on the IBKR API (~5 min)
In **IB Gateway → Configure → Settings → API → Settings**:
- ✅ Enable ActiveX and Socket Clients
- **Socket port** = `4002` (paper)
- **Trusted IPs**: add `127.0.0.1`
- ❌ Read-Only API = OFF
- ✅ Allow connections from localhost only

Log the Gateway in with your **paper** account and leave it running.

## Step 4 — Connect and run (~2 min)
```bash
python cli.py status      # should now say BROKER: ibkr_paper + your paper balances
python cli.py dry-run     # builds real IBKR combos, places NOTHING
python cli.py run-cycle   # places paper orders on IBKR
python cli.py dashboard   # optional: open dashboard/out/dashboard.html on your phone
```
If `status` still says `BROKER: sim`, the Gateway wasn't reachable — re-check it's
logged in and the port is `4002`. (More troubleshooting: docs/08 §3.)

---

## Daily use
- Run a cycle by hand whenever: `python cli.py run-cycle`
- Check anytime from your phone: `python cli.py status`
- Panic button: `python cli.py kill` (the gate then rejects everything until you
  clear it).

## When you want it unattended (later)
A desktop only runs while it's on. To have cycles run on a schedule without you,
move the Gateway + a cron job to an always-on **cloud VM** — full guide in
docs/08-ibkr-gateway-runbook.md §4–5. The same VM becomes the bot's permanent
home, and your laptop/desktop just SSH in.

---

## Moving to a different computer later (e.g., a new laptop)
The repo is portable; see the short "switching machines" note in docs/08, but in
brief:
1. `git clone` the repo on the new machine, `python bootstrap.py`.
2. To keep your trade history, copy the `data/portfolio.db` file over (it's local
   and gitignored — it is NOT carried by git). A fresh machine starts with an
   empty journal otherwise.
3. Run IB Gateway on the new machine (only one Gateway session per login at a
   time — log out the old one first).

> Once you're on the cloud-VM model, this is a non-issue: the VM is the host, and
> which laptop you happen to own doesn't matter at all.
