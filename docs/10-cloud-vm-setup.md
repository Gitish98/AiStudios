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

> **Honest limitation:** Phase 0 has **no code-level market-calendar guard yet**
> (that's a Phase 2 item). The `1-5` cron avoids weekends but **not holidays /
> half-days**. Until the calendar guard lands, treat unattended runs as
> best-effort and review with `status`. Don't rely on it fully for live.

---

## 8. Operating it day to day (from your phone)
- `ssh trader@<VM_IP>` then `python3 cli.py status` — your primary surface.
- `python3 cli.py kill` — panic button, anywhere, anytime.
- `tail -f ~/AiStudios/logs/cron.log` — see scheduled runs.
- Rebuild the dashboard: `python3 cli.py dashboard`, then `scp` it down or serve
  it briefly — but `status` already tells you everything on a phone.

---

## 8.0 Connecting from a phone SSH app (Termius) — first time

To run anything "on the VM" from your phone you first SSH in. One-time setup:

**1. Install Termius** (free, App Store / Play Store). Blink works too.

**2. You need one of these to authenticate** (whatever you chose when creating the
droplet):
- an **SSH private key** (the match to the public key you pasted into DigitalOcean
  at droplet creation), **or**
- the droplet's **root password** (only if you enabled password login — the
  hardened setup in §2 disables this, so you likely have a key).

**3a. If you have the SSH key** (a file like `id_ed25519` or `id_rsa`, no `.pub`):
- Termius → **Keychain** → **+** → **Import key** → pick the private key file (AirDrop
  or copy it to your phone's Files first, or paste its text).
- Termius → **Hosts** → **+** → set:
  - **Alias:** `aistudios`
  - **Hostname:** `100.85.251.33` (your Tailscale IPv4) or `aistudios.tailbcaed2.ts.net`
  - **Username:** the VM user you created (`root` or `trader`)
  - **Key:** select the key you just imported
- Tap the host → you land at a shell prompt. Done.

**3b. If you only set a password:** same Hosts → + steps, but fill **Password**
instead of Key.

**4. Requires Tailscale ON** on your phone if you use the `100.x` / `.ts.net`
address (that's the private path). A public droplet IP works over normal internet
but only if the firewall allows SSH from your phone — the Tailscale path is safer.

> Lost the SSH key? You can add a new one from the DigitalOcean console
> (Droplet → Access → **Launch Droplet Console** in the browser, then append your
> new public key to `~/.ssh/authorized_keys`). Easier to sort from a desktop.

---

## 8.1 Phone dashboard via Tailscale (bookmarkable, private)

Once Tailscale is installed on both the VM and your phone (both show **Connected**
in the Tailscale app, same tailnet), you can serve the dashboard as a private URL
only your own devices can reach — never the public internet.

**Where do I run these? On the VM, not your phone.** From your phone, open an SSH
app (Termius or Blink), SSH into the VM, and type these there:

```bash
ssh trader@<VM_IP>            # or the Tailscale name, e.g. ssh trader@aistudios
cd ~/AiStudios
python3 cli.py dashboard      # rebuild dashboard/out/dashboard.html from latest data
tailscale serve --bg --https=443 /home/trader/AiStudios/dashboard/out
```

Adjust `/home/trader/...` to your VM's real home path (run `whoami` and `pwd` if
unsure). Then, in your phone's browser, open (using YOUR tailnet's MagicDNS name,
shown in the Tailscale app under the VM device — e.g. `aistudios.tailXXXX.ts.net`):

```
https://<your-vm>.<tailnet>.ts.net/dashboard.html
```

Bookmark it. `--bg` keeps it serving after you close the SSH session.

**Keep it fresh automatically** — have cron rebuild the dashboard after each cycle
(add to the crontab from §7, VM clock in US/Eastern):

```cron
35 10 * * 1-5  cd /home/trader/AiStudios && /usr/bin/python3 cli.py dashboard
```

**To stop serving:** `tailscale serve --https=443 off`.

> This is safe because Tailscale is a private, encrypted mesh — the dashboard is
> reachable only by devices logged into *your* tailnet, and the IBKR API port
> (4002) is never served, only the static HTML in `dashboard/out/`.

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
