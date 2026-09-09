# 10 — Cloud VM Setup (the bot's permanent home)

> Goal: a small, locked-down, always-on Linux box that runs **IB Gateway**
> (headless) + **AiStudios** on a schedule, reachable from your phone over SSH.
> Once this exists, which laptop/desktop you own no longer matters — they're just
> terminals.
>
> Everything here stays **paper**. Budget ~30–45 min the first time. Still not
> financial/legal/tax advice.

---

## 0. What you'll end up with

```
        your phone / laptop  ──SSH──►  Cloud VM (always on)
                                        ├─ IB Gateway (headless, via IBC + Xvfb)
                                        │     listening ONLY on 127.0.0.1:4002
                                        ├─ AiStudios repo + Python
                                        └─ cron → python cli.py run-cycle (weekdays)
                                        ▲
                                        └─ IBKR paper account
```
The API port is **never** exposed to the internet. You reach it through an SSH
tunnel.

---

## 1. Pick a provider and size (~$5–7/mo)

Any Ubuntu 22.04/24.04 VPS works. IB Gateway + its Java runtime want headroom.

| Provider | Plan | ~Cost |
|---|---|---|
| **Hetzner** | CX22 (2 vCPU / 4 GB) | ~€4/mo |
| **DigitalOcean** | Basic 2 GB | ~$12/mo |
| **AWS Lightsail** | 2 GB | ~$10/mo |

- **RAM: 2 GB minimum, 4 GB comfortable** (Gateway + JRE ≈ 1 GB).
- **Region:** US East is a fine default (close to IBKR's US infra).
- OS image: **Ubuntu 24.04 LTS**.
- Add your **SSH public key** during creation (no password logins).

> Don't have an SSH key? On your laptop: `ssh-keygen -t ed25519`, then paste
> `~/.ssh/id_ed25519.pub` into the provider's "SSH keys" box.

---

## 2. First login + harden (~10 min)

SSH in as root (provider gives you the IP), then:

```bash
# --- create a non-root user ---
adduser trader
usermod -aG sudo trader
rsync --archive --chown=trader:trader ~/.ssh /home/trader   # copy your key over

# --- firewall: allow ONLY ssh ---
ufw allow OpenSSH
ufw --force enable

# --- lock down sshd: keys only, no root ---
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart ssh

# --- updates + basics ---
apt update && apt -y upgrade
apt -y install python3 python3-pip python3-venv git xvfb unzip curl fail2ban
```

Reconnect as `trader` from now on: `ssh trader@<VM_IP>`.

> The API port (4002) is **not** in the firewall on purpose. It stays bound to
> localhost; you tunnel to it (Step 6). Never `ufw allow 4002`.

---

## 3. Install IB Gateway (headless) (~5 min)

```bash
cd ~
# Grab the current STABLE standalone Linux installer from IBKR's site
# (link: interactivebrokers — "IB Gateway" → Linux 64-bit standalone .sh).
curl -L -o ibgw.sh "https://download2.interactivebrokers.com/installers/ibgateway/stable-standalone/ibgateway-stable-standalone-linux-x64.sh"
chmod +x ibgw.sh
./ibgw.sh -q -dir ~/Jts/ibgateway      # silent install; bundles its own Java (JRE)
```
The standalone installer includes its own JRE, so you don't need a separate Java.

---

## 4. Install IBC (auto-login + auto-restart) (~10 min)

IB Gateway is a GUI app that wants a human to click "Login" and survives only one
session. **IBC** automates the login, dismisses dialogs, and handles IBKR's daily
restart. We run it under **Xvfb** (a virtual display) so no desktop is needed.

```bash
cd ~
# Get the latest IBC release for Linux (IbcAlpha/IBC on GitHub → IBCLinux-x.y.z.zip)
curl -L -o ibc.zip "https://github.com/IbcAlpha/IBC/releases/latest/download/IBCLinux-3.20.0.zip"
mkdir -p ~/ibc && unzip -o ibc.zip -d ~/ibc
chmod +x ~/ibc/*.sh ~/ibc/scripts/*.sh
```

Edit **`~/ibc/config.ini`** — set these keys (leave the rest default):

```ini
IbLoginId=your_PAPER_username
IbPassword=your_PAPER_password
TradingMode=paper
IbDir=/home/trader/Jts
; keep the API reachable only from this machine:
OverrideTwsApiPort=4002
; let IBKR's nightly restart happen and have IBC log back in automatically:
IbAutoClosedown=no
ClosedownAt=
ReloginAfterSecondFactorAuthenticationTimeout=yes
```

> Paper logins generally don't require the mobile 2FA dance that live logins do —
> a key reason to validate everything on paper here first. Keep `config.ini`
> readable only by you: `chmod 600 ~/ibc/config.ini`.

In **`~/Jts/ibgateway/.../jts.ini`** (created after first run) the API "Trusted
IP" should be `127.0.0.1` and "Allow localhost only" on — IBC/Gateway default to
this; verify after first start.

---

## 5. Run it as a service (survives reboots + restarts) (~5 min)

Create `/etc/systemd/system/ibgateway.service` (use `sudo`):

```ini
[Unit]
Description=IB Gateway via IBC (headless)
After=network-online.target
Wants=network-online.target

[Service]
User=trader
Environment=DISPLAY=:99
# Xvfb gives the GUI app a virtual screen; IBC drives the login.
ExecStartPre=/usr/bin/Xvfb :99 -screen 0 1024x768x24 -nolisten tcp &
ExecStart=/home/trader/ibc/scripts/ibcstart.sh "1030" --gateway \
  "--mode=paper" "--user=" "--pw=" "--ibc-ini=/home/trader/ibc/config.ini" \
  "--ibc-path=/home/trader/ibc" "--tws-path=/home/trader/Jts"
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ibgateway
sudo journalctl -u ibgateway -f      # watch it log in; Ctrl-C to stop watching
```

When the logs show the API server is listening on `127.0.0.1:4002`, you're up.

> The exact `ibcstart.sh` arguments and IBC version string change between
> releases — if a flag is rejected, check the IBC `userguide` in `~/ibc/` for the
> current invocation. The shape (Xvfb + ibcstart, mode=paper) is stable.

---

## 6. Install AiStudios on the VM + connect (~5 min)

```bash
cd ~
git clone https://github.com/Gitish98/AiStudios.git
cd AiStudios
python3 bootstrap.py        # creates config, installs deps, runs tests, prints status
python3 cli.py status       # should now say BROKER: ibkr_paper with your paper balances
python3 cli.py dry-run
python3 cli.py run-cycle
```
`config/config.yaml` already targets `127.0.0.1:4002`, so no edits needed.

**Reach it from your phone/laptop** without exposing the port — open an SSH tunnel
and the VM's Gateway appears as your own localhost:

```bash
ssh -N -L 4002:127.0.0.1:4002 trader@<VM_IP>
```
Or simply SSH in and run `python3 cli.py status` directly on the VM (simplest from
a phone SSH app like Termius/Blink).

---

## 7. Schedule it (unattended weekday cycles)

A small wrapper keeps cron tidy — `scripts/run_cycle.sh` is in the repo. Install a
crontab on the VM:

```bash
crontab -e
```
Add (runs ~30 min after the US open, Mon–Fri; times are the VM's clock — set the
VM to US/Eastern or adjust):

```cron
TZ=America/New_York
0 10 * * 1-5  /home/trader/AiStudios/scripts/run_cycle.sh >> /home/trader/AiStudios/logs/cron.log 2>&1
```

> **Market-calendar guard (now in code):** `cli.py run-cycle` checks
> `core/market_calendar.py` (NYSE via `pandas-market-calendars`, with a stdlib
> fallback) and **skips weekends AND holidays/half-days on its own** — so the
> `1-5` cron is just a coarse outer filter; the code makes the real call and a
> holiday run exits cleanly with a "market closed" note. Use `--force` only for a
> deliberate off-day run. Still glance at `status` periodically — don't rely on any
> automation blindly, and never for live.

---

## 8. Operating it day to day (from your phone)
- `ssh trader@<VM_IP>` then `python3 cli.py status` — your primary surface.
- `python3 cli.py kill` — panic button, anywhere, anytime.
- `tail -f ~/AiStudios/logs/cron.log` — see scheduled runs.
- Rebuild the dashboard: `python3 cli.py dashboard`, then `scp` it down or serve
  it briefly — but `status` already tells you everything on a phone.

---

## 9. Before you ever go live (do NOT skip)
- Weeks of clean **paper** cycles on the VM, zero reconcile drift.
- Confirm credit spreads submit as **credits** on the IBKR side.
- Confirm your real non-registered **margin** account's options permissions cover
  defined-risk spreads.
- Live = a SEPARATE Gateway profile on port **4001**, the full go-live gate
  (docs/05 §1), and the smallest ramp tier.
- Rotate the VM's IBKR password handling into a secrets store; never commit it.

> Reminder: confirm options permissions with IBKR and the tax treatment of active
> trading with a Canadian tax professional / the CRA before any live capital.

## 10. Going live from this VM — the tier-1 validation (target: November 2026)

This section exists because the one-session rule (§8, Error 10197) and the go-live
gates were documented in different places and nobody had written down how they
interact. Read it before you buy anything.

### 10.1 What tier 1 is, and is not

Tier 1 is **$250 max notional per position, 2 positions**, every order clamped by the
risk gate. It is a *plumbing validation*: its job is to prove the untested live path —
a real order places, fills, reconciles, and its slippage gets **measured** — at a size
where a total loss is a rounding error. It is permitted before the paper record has
graduated (gate 7 passes at tier 1). It is **not** "deploying the money"; tiers above
1 are refused until the paper record graduates *and* tier 1 has produced its own
evidence (`python cli.py ramp` shows exactly where that stands).

### 10.2 The wall: one market-data session

IBKR serves market data to **one login at a time**. Your paper trading user's data
permissions are *bound to the live user's* — you can share the live subscriptions
with paper, but only while the live user is **not logged in elsewhere**. This VM's
paper Gateway *is* a login. Two Gateways (paper + live) on this box, or a live
Gateway here plus a live TWS on your desk, produce Error 10197 / "Existing session
detected" tug-of-war, and the data stops for whichever loses.

Pick one, deliberately:

| Option | What happens to the paper record | Cost |
|---|---|---|
| **A. Swap** — stop the paper Gateway service, run the live Gateway in its place for the validation window | Paper cycles pause; IV bank pauses. Resume paper after. | $0 extra |
| **B. Second host** — a second small VM runs the live Gateway; this one keeps running paper | Both continue | ~$6–12/mo + a second IBC/systemd setup (§3–5 again) |
| **C. Time-share** — live Gateway logs in only during the validation cycles, paper the rest of the day | Fragile; two logins racing the nightly restart | $0, not recommended |

A is the honest default for a *validation*: it is a few days, and the paper record
resuming afterwards is just more sessions. Choose B only if you intend to run live and
paper side by side for months.

### 10.3 Entitlements (the live login, not the paper one)

Real-time subscriptions can only be bought on the **funded live account**. Gate 6
requires `brokers.ibkr_live_market_data_type: 1`; under type 1 without the
entitlement IB refuses the request outright (it does not silently serve delayed —
that only happens under type 3/4), so a missing subscription shows up as *no quote*,
which the cycle treats as "hold", never as a price. You need, on the live login:

- US equities top-of-book for the ETFs' home exchanges (the "US Securities Snapshot
  and Futures Value Bundle" covers NYSE/ARCA/NASDAQ for retail accounts);
- **OPRA** (US options) — without it there are no live option quotes at all.

Check `python cli.py ramp` after purchase: the `IB notices:` line in the cycle summary
must read `none` under type 1. If it reports 10089/10091 under type 1 the label will
say **UNEXPECTED — NOT delivered**; that means an entitlement is missing.

### 10.4 The checklist (operator actions — none can be done by a model)

1. Fund the live account. Decide the tier-1 cap is money you are content to lose.
2. Buy the entitlements in §10.3 on the live login.
3. `config/config.yaml`: `mode: live`, `brokers.execution: ibkr_live`,
   `brokers.ibkr_live_port: 4001`, `brokers.ibkr_live_market_data_type: 1`,
   `go_live.ramp_tier: 1`. Leave `ibkr_market_data_type: 3` — it is paper's.
4. Choose §10.2 option A or B and set the live Gateway up accordingly (IBC config
   points at the live login; `TradingMode=live`).
5. `python cli.py ramp` — six of seven gates should read ✅; the seventh
   (`session_confirmation`) is typed at arming time. Fix any ❌ before proceeding.
6. Passphrase the SSH key; confirm the API bind is localhost-only (§9).
7. On the day: write `~/.aistudios/LIVE_ENABLED` with today's date; run
   `python cli.py go-live` **in a real terminal** and type the phrase it shows.
8. Watch the first cycle. `python cli.py status` should show `MODE: LIVE`; the
   heartbeat body should show `IB notices: none`. The first fill's slippage is
   the number this whole project has been waiting for.
9. Stand down any time with `python cli.py go-paper`. A KILL, a freeze, a crashed
   cycle, or persistent drift resets the clean-day clock (§05 §1.3) — that is
   the system working, not a reason to skip it.

