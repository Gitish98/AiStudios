# 08 — IBKR Gateway Runbook (paper → live)

> **Why this exists.** IBKR's API is not a cloud REST endpoint. Your code talks
> over a socket to a running **IB Gateway** (a small app authenticated to your
> account). So "connect AiStudios to IBKR" really means "have a Gateway running
> somewhere the bot can reach." This doc is the checklist.
>
> Until a Gateway is reachable, the system runs against the **sim broker**
> automatically (the factory falls back and tells you so). Nothing here blocks
> development from a phone — it's only needed to touch your real IBKR paper/live
> account.

---

## 0. The two ways to host the Gateway

| Option | Good for | Trade-off |
|---|---|---|
| **A. Your own computer** (TWS or IB Gateway desktop) | First connection, manual testing | Only works while that machine is on and logged in. Not unattended. |
| **B. A small always-on cloud VM** (headless IB Gateway + IBC) | Real operation: scheduled cycles, run from your phone | ~$5–10/mo; one-time setup. **This is also the host your scheduler wants** — see §5. |

You'll likely start with **A** to prove the connection, then move to **B** when
you want the bot to run without you babysitting a desktop.

---

## 1. Ports & paper/live (memorize this)

| App | Paper port | Live port |
|---|---|---|
| **IB Gateway** | **4002** | 4001 |
| **TWS** | 7497 | 7496 |

`config/config.yaml` defaults to `ibkr_port: 4002` (Gateway paper). The adapter
**refuses** to construct against a live port while `paper: true`, so a fat-finger
can't silently point you at live.

---

## 2. Enable the API (one-time, in TWS/Gateway settings)

In **IB Gateway → Configure → Settings → API → Settings**:
- ✅ *Enable ActiveX and Socket Clients*
- ✅ *Read-Only API* = **OFF** (we place paper orders)
- **Socket port** = `4002` (paper)
- **Trusted IPs**: add `127.0.0.1` (and only that — see §4)
- ✅ *Allow connections from localhost only* (keep it local; reach it via SSH tunnel, not an open port)
- Master API client ID: leave blank; the bot uses `ibkr_client_id: 7`

Log in to the Gateway with your **paper** credentials (IBKR gives every account a
paper login — username often your live user + a suffix, set in Account Management).

---

## 3. Connect from AiStudios

With the Gateway running and logged in:

```bash
pip install ib_async          # the maintained fork (ib_insync also works)

# config/config.yaml already has:
#   brokers.execution: ibkr_paper
#   brokers.ibkr_host: 127.0.0.1
#   brokers.ibkr_port: 4002
#   brokers.ibkr_client_id: 7

python cli.py status          # should now say BROKER: ibkr_paper and show your real paper balances
python cli.py dry-run         # builds real combos, places nothing
python cli.py run-cycle       # places paper orders on IBKR
```

If `status` still says `BROKER: sim`, the Gateway wasn't reachable — check it's
logged in, the port matches, and (for a VM) your SSH tunnel is up (§4).

---

## 4. Cloud VM setup (Option B) — headless & secure

1. **VM**: a small Linux box (1 vCPU / 1–2 GB) near a US/Canada region.
2. **Headless Gateway**: install IB Gateway + **IBC** (IBController) to auto-start
   and auto-restart it, and to handle the daily restart IBKR forces. Paper logins
   generally don't require the mobile 2FA dance that live logins do — one reason to
   validate everything on paper first.
3. **Never expose the API port.** Keep the Gateway bound to `127.0.0.1`. Reach it
   from your laptop/phone tooling over an **SSH tunnel**:
   ```bash
   ssh -N -L 4002:127.0.0.1:4002 user@your-vm    # now localhost:4002 == VM's Gateway
   ```
   No inbound firewall rule for 4002. Ever.
4. **Secrets**: IBKR needs **no API key in `.env`** — the "credential" is the
   logged-in Gateway session. Keep the Gateway login in the VM's IBC config with
   tight file permissions, not in this repo.

---

## 5. The VM is also your scheduler

A phone can't run an unattended cron. The same always-on VM that hosts the Gateway
is where the market-hours-guarded schedule lives:

```bash
# Example: weekday once-a-day cycle, 30 min after the US open (guard is in code too)
# crontab on the VM:
0 14 * * 1-5  cd /opt/AiStudios && /usr/bin/python3 cli.py run-cycle >> logs/cron.log 2>&1
```

The market-calendar guard (`pandas-market-calendars`) means a holiday/half-day run
is a safe no-op even if cron fires. You review from your phone with
`python cli.py status` over SSH, or the dashboard.

---

## 6. Pre-live checklist (do NOT skip)

Before flipping `mode: live` (and the rest of the go-live gate in docs/05 §1):
- [ ] ≥ several weeks of clean **paper** cycles on IBKR with zero reconcile drift.
- [ ] Verified fills, partial-fill accounting, and that credit spreads submit as
      **credits** (negative combo price) — confirm on the IBKR side, not just our logs.
- [ ] Confirmed **options permissions** on your real (non-registered margin) account
      cover defined-risk spreads.
- [ ] Live keys/port (`4001`) configured as a SEPARATE profile; the ramp tier set
      to its smallest size.
- [ ] You've re-read the honest risk picture in `README.md` and `docs/05`.

> Reminder: none of this is financial, legal, or tax advice. Confirm options
> permissions with IBKR and the tax treatment of active trading with a Canadian
> tax professional / the CRA.
