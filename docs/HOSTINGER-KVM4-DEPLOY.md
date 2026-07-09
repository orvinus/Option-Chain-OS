# Deploying Option-Chain-OS on a Hostinger KVM 4 VPS — the complete guide

This is a beginner-safe, end-to-end walkthrough for running the **whole** stack
(TimescaleDB + FastAPI backend + nginx frontend) on one **Hostinger KVM 4** VPS.
Every command here matches what actually runs in `docker/docker-compose.yml`.

> **Related docs:** [`DEPLOY.md`](DEPLOY.md) (unattended login + health semantics),
> [`VPS-DEPLOY-PLAN.md`](VPS-DEPLOY-PLAN.md) (generic VPS phases),
> [`RUNBOOK.md`](RUNBOOK.md) (local dev). This file is the Hostinger-KVM-4-specific,
> research-backed version and is the one to follow for a fresh VPS.

---

## 0. TL;DR — what you are about to do

1. Buy **KVM 4**, install the **Ubuntu 24.04 + Docker** template.
2. Open a terminal (browser terminal in hPanel, or SSH from your PC).
3. `git clone` the repo, fill in `.env` (broker keys + your server IP).
4. `docker compose ... up -d --build` → the app is live at `http://YOUR_IP`.

**The one hard rule:** the broker (XTS/Lakshmishree) allows **one market-data
session per appKey**. Before you start the feed on the VPS, **stop the stack on
your local PC** — otherwise the two servers keep killing each other's login.

### KVM 4 at a glance

| Resource | KVM 4 | Enough for this app? |
|----------|-------|----------------------|
| vCPU | 4 (AMD EPYC) | Yes — 1–2 cores do ingest+API, spare cores speed up Docker builds |
| RAM | 16 GB | Very comfortable (stack needs ~2–3 GB live) |
| Disk | 200 GB NVMe | Years of tick data (current DB ≈ 20 MB compressed / 6 weeks) |
| Bandwidth | 16 TB/mo | Far more than the feed + dashboard use |
| Included | Weekly backups, real-time snapshots, managed firewall, dedicated IPv4 | Use all of them |

KVM 4 is over-spec for a single-symbol feed (KVM 2 also works); the extra cores
just make image builds and on-server validation runs snappier.

---

## 1. Can I access the terminal? — Yes, three ways

**A) Browser terminal (easiest, nothing to install).**
hPanel → **VPS** → **Manage** → on the VPS Overview page click **Terminal**
(top-right). A new browser tab opens **already logged in as `root`**. This is the
recommended way if you're new — no keys, no SSH client.

**B) SSH from your own PC.** Windows PowerShell already ships OpenSSH:
```powershell
ssh root@YOUR_IP
```
Find `YOUR_IP` and the SSH username (`root`) in hPanel on the VPS Overview page
("VPS details" card). First connect: type `yes` to accept the fingerprint, then
the root password you set. (Later you can switch to SSH-key login — see §12.)

**C) Kodee AI terminal (optional).** hPanel's Browser Terminal includes *Kodee*,
an AI assistant that turns plain-English requests into shell commands. Handy, but
everything in this guide is copy-paste, so you won't need it.

> Everything below is typed into **one** of these terminals. They're
> interchangeable; the browser terminal is fine for the whole setup.

---

## 2. What to get / set inside Hostinger (hPanel checklist)

You do **not** need cPanel, a domain, email, or any add-on. Just:

| # | In hPanel | What to do |
|---|-----------|-----------|
| 1 | **VPS → Buy/Manage** | Order **KVM 4**. |
| 2 | **OS & Panel → Operating System** | Change OS to the **Ubuntu 24.04 with Docker** template (search "Docker"). Installs `docker-ce` + `docker compose` for you (~10 min). *This saves §5.* If you prefer plain Ubuntu 24.04, that's fine too — you'll install Docker manually in §5. |
| 3 | **VPS Overview → details card** | Copy the **public IPv4**. This is `YOUR_IP` everywhere below. |
| 4 | **VPS Overview** | Set a strong **root password** (and add your **SSH public key** if you have one). |
| 5 | **Security → Firewall** | Create a firewall (see §3). Allow **22** and **80** only. |
| 6 | **Snapshots / Backups** | Note where these live — you'll take a snapshot once the app works, and backups are included on KVM 4. |

---

## 3. Firewall — the most important security step (read this)

There are **two** firewalls in play. Understanding the difference protects your
database from the public internet.

### 3a. Hostinger **managed firewall** (hPanel) — your PRIMARY defense

- It runs **above the operating system**, at the network edge, and **defaults to
  dropping all inbound traffic** — you must explicitly allow ports.
- Because it sits above the OS, **Docker cannot bypass it.**

In hPanel → **Security → Firewall → Add Firewall**, allow inbound:

| Port | Protocol | Why |
|------|----------|-----|
| 22 | TCP | SSH |
| 80 | TCP | The web dashboard (nginx) |
| 443 | TCP | *Only later*, if you add a domain + HTTPS |

**Do NOT add 5432 or 8000.** Those are the database and the raw backend API;
they must never be public. The managed firewall dropping them is what keeps them
private.

### 3b. Why UFW (the in-Ubuntu firewall) is **not enough** on its own

The compose file publishes `5432` (TimescaleDB) and `8000` (backend) to the
host. **Docker programs iptables directly and bypasses UFW** — a packet for a
published container port is redirected through iptables' `FORWARD` chain *before*
UFW's `INPUT` rules ever see it. So `ufw deny 5432` does **nothing** for a
Docker-published port.

**Conclusion:** rely on the **Hostinger managed firewall (§3a)** to keep
5432/8000 private. Set up UFW too (§4) as defense-in-depth for non-Docker ports,
but never *trust* UFW alone to hide a Docker port. If you ever run **without** the
managed firewall, apply the optional hardening in §11 instead.

---

## 4. Architecture in 30 seconds

```
Browser ──80──> [ nginx (frontend container) ] ──/api,/ws──> [ backend :8000 ] ──> [ TimescaleDB :5432 ]
                         serves the React dashboard          FastAPI + XTS feed        market-data store
```

nginx (`docker/nginx.conf`) reverse-proxies `/api/` and `/ws/` to the backend, so
the browser only ever talks to **port 80**. That's why only 80 is public. The
frontend is built with `VITE_API_BASE` unset, so it calls same-origin `/api` —
no rebuild needed for your server's IP or domain.

---

## 5. Install Docker (SKIP if you used the Docker template in §2)

On plain Ubuntu 24.04:
```bash
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker          # start Docker now + on every reboot
docker --version && docker compose version
```

---

## 6. Base system + secondary firewall (UFW)

```bash
apt update && apt upgrade -y
timedatectl set-timezone Asia/Kolkata          # optional: IST timestamps in logs/cron
apt install -y git ufw

# UFW as defense-in-depth (NOT a substitute for the hPanel firewall — see §3b)
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp
ufw --force enable
```

---

## 7. Get the code

```bash
mkdir -p /opt && cd /opt
git clone https://github.com/orvinus/Option-Chain-OS.git nifty-oi
cd nifty-oi
git checkout ayush-bhai-branch
```
> **Private repo?** When git asks for a password, use a GitHub **fine-grained
> Personal Access Token** (Settings → Developer settings → Personal access
> tokens) as the password, not your account password.

---

## 8. Configure `.env` (the only file you edit)

```bash
cp .env.example .env
nano .env
```

Set these (copy the secret values from the working `.env` on your PC):

| Key | Value | Why |
|-----|-------|-----|
| `XTS_MD_APP_KEY` | *your key* | Broker market-data login |
| `XTS_MD_SECRET_KEY` | *your secret* | Broker market-data login |
| `XTS_MD_BASE_URL` | `https://trades.lakshmishree.com/apimarketdata` | Your broker host (not the demo default) |
| `XTS_LOGIN_AT_STARTUP` | `true` | Log in automatically on every boot/restart |
| `API_CORS_ORIGINS` | `http://YOUR_IP` | So the browser origin is accepted by the API |
| `UNDERLYING_SYMBOL` | `NIFTY` | Symbol the feed starts on |
| `NIFTY_LOT_SIZE` | `65` | Current NIFTY lot |
| `STRIKE_WINDOW` | `11` | Strikes each side of ATM (fits the 50-instrument cap) |
| `EXPIRIES` | `current_weekly` | Which expiry to subscribe |

**Leave these alone:**
- `DB_URL` / `DB_URL_SYNC` — `docker-compose.yml` overrides them to the internal
  `timescaledb` container. (Your local `.env` may point them at Windows-only
  ports like 5434/55432 — those are wrong on the VPS; the compose override wins.)
- `VITE_API_BASE` — keep it **unset/commented** so the dashboard uses same-origin
  `/api` through nginx.

Save in nano: `Ctrl+O`, Enter, `Ctrl+X`.

---

## 9. STOP the local PC stack first (do not skip)

The broker allows **one** market-data session per appKey. On your **Windows PC**:
```powershell
cd "D:\trading algo (ayush bhai)\trading algo (ayush bhai)"
docker compose --env-file .env -f docker/docker-compose.yml down
```
If you skip this, the VPS login invalidates the PC's token and vice versa, and
both servers' auto-heal loops fight forever.

---

## 10. Launch

From the repo root on the VPS:
```bash
cd /opt/nifty-oi
docker compose --env-file .env -f docker/docker-compose.yml up -d --build
docker compose --env-file .env -f docker/docker-compose.yml logs -f backend
```

First build ≈ 3–6 min on KVM 4. In the backend logs, the **healthy sequence** is:
```
alembic ... running upgrade ...            # DB migrations
app.startup run_mode=live
xts.login.success  /  app.startup.auto_login_ok
scripmaster.universe_resolved  atm=... spot=... token_count=46
ws.subscribe.success  ... rejected=0
aggregator.flushed  rows=...               # repeats every second during market hours
```
`Ctrl+C` leaves the log view without stopping anything (containers keep running).

---

## 11. Verify it works

On the VPS:
```bash
curl -s http://localhost/api/health
```
You want `"authenticated": true`, `"feed_connected": true` (market hours only),
and a recent `last_flush_at`. Then from your PC's browser:

- **`http://YOUR_IP`** → the dashboard, showing live OI-change bars.
- **`http://YOUR_IP/api/health`** → the same JSON.

Off-hours, `feed_connected` may be `false` and charts static — that's normal.

**Now take a snapshot in hPanel** (a "known-good" restore point).

---

## 12. Optional hardening (do if you want extra safety)

**a) SSH keys instead of password.** On your PC: `ssh-keygen -t ed25519`, then
paste the contents of `~/.ssh/id_ed25519.pub` into the VPS's
`/root/.ssh/authorized_keys`. Then in `/etc/ssh/sshd_config` set
`PasswordAuthentication no` and `systemctl restart ssh`.

**b) Belt-and-braces port privacy (only if you're NOT using the hPanel managed
firewall).** Because Docker bypasses UFW (§3b), the truly robust fix is to bind
the sensitive ports to localhost. In `docker/docker-compose.yml` change:
```yaml
  timescaledb:
    ports:
      - "127.0.0.1:5432:5432"   # was "5432:5432"
  backend:
    ports:
      - "127.0.0.1:8000:8000"   # was "8000:8000"
```
Then `docker compose ... up -d`. This makes 5432/8000 reachable only from the
server itself. **With the Hostinger managed firewall active (§3a), this is
optional** — the managed firewall already drops those ports at the network edge.

**c) Change the default DB password.** The compose file ships
`POSTGRES_PASSWORD: postgres`. If you expose the DB in any way, change it in
`docker-compose.yml` **and** in the matching `DB_URL`/`DB_URL_SYNC` there.

---

## 13. Running it day-to-day

Once launched, it's meant to be hands-off:

- **Auto-restart:** all three containers use `restart: unless-stopped`, and Docker
  starts on boot — a reboot brings the whole stack back by itself.
- **Auto-login:** `XTS_LOGIN_AT_STARTUP=true` + a 12h refresh loop keep the token
  fresh; the feed self-heals from "Invalid Token" and the 50-instrument
  subscription-limit wedge (both fixed in this branch).
- **If the feed ever looks dead:** re-mint the token —
  `curl -X POST http://localhost:8000/api/auth/login` on the VPS, or click Login
  in the dashboard.
- **Deploy updates:**
  ```bash
  cd /opt/nifty-oi && git pull
  docker compose --env-file .env -f docker/docker-compose.yml up -d --build
  ```
  (~30s feed gap; the session restores itself.)
- **Overnight:** if you stop the VM, start it **before 09:10 IST** so login +
  scripmaster + subscribe finish before the 09:15 open.
- **Free monitoring:** point **UptimeRobot** (or similar) at
  `http://YOUR_IP/api/health` so you get an email if the box goes down. (An HTTP
  ping only detects a dead host — it does not keep the broker socket alive; the
  always-on container does that.)

---

## 14. Backups

**On the VPS (nightly logical dump).** `mkdir -p /root/backups`, then `crontab -e`
and add (16:30 IST, weekdays):
```cron
30 16 * * 1-5 cd /opt/nifty-oi && docker compose --env-file .env -f docker/docker-compose.yml exec -T timescaledb sh -c 'pg_dump -U postgres -Fc oi > /tmp/oi.dump' && docker compose --env-file .env -f docker/docker-compose.yml cp timescaledb:/tmp/oi.dump /root/backups/oi_$(date +\%F).dump
```

**Pull copies down to your PC** with the committed script (already tested):
```powershell
.\scripts\backup-oi.ps1 -Mode vps -VpsHost root@YOUR_IP
```
(Needs SSH-key auth set up — §12a.)

**Restore** a dump into a TimescaleDB container (note the pre/post hooks — a plain
`pg_restore` fails on hypertables):
```bash
docker cp oi_YYYY-MM-DD.dump docker-timescaledb-1:/tmp/r.dump
docker exec docker-timescaledb-1 psql -U postgres -c "CREATE DATABASE oi_restore;"
docker exec docker-timescaledb-1 psql -U postgres -d oi_restore -c "SELECT timescaledb_pre_restore();"
docker exec docker-timescaledb-1 pg_restore -U postgres -d oi_restore --no-owner /tmp/r.dump
docker exec docker-timescaledb-1 psql -U postgres -d oi_restore -c "SELECT timescaledb_post_restore();"
```
Also keep hPanel **snapshots** for whole-disk rollback before risky changes.

---

## 15. Troubleshooting

| Symptom | Cause → Fix |
|---------|-------------|
| `http://YOUR_IP` won't load | Port 80 not allowed → add it in the **hPanel managed firewall** (and `ufw allow 80/tcp`). |
| `xts.login` fails only from the VPS | Broker may **IP-whitelist** API access → ask Lakshmishree to allow your VPS IP. |
| `ws.subscribe.all_failed ... 'Invalid Token'` | Stale token → self-heals; manual: `POST /api/auth/login`. |
| Feed half-dead after a symbol switch; logs show *"Exceeded Instrument Subscription Limit of 50"* | Gateway slots not freed → self-heals via fresh login (fixed in this branch); manual re-login also clears it. |
| `authenticated: false` after reboot | Check `.env` keys, then `docker compose ... logs backend` for the login error. |
| Dashboard empty during market hours | Check `/api/health` first — usually login failed or the **local PC stack** is stealing the single session (see §9). |
| Two servers keep logging each other out | Only one machine may run the feed per appKey → stop the other (`docker compose ... down`). |
| Health JSON reachable but charts static off-hours | Normal — market closed. |

---

## 16. Command cheat-sheet

```bash
# status of the three containers
docker compose --env-file .env -f docker/docker-compose.yml ps
# follow backend logs
docker compose --env-file .env -f docker/docker-compose.yml logs -f backend
# restart just the backend
docker compose --env-file .env -f docker/docker-compose.yml restart backend
# stop everything (frees the broker session)
docker compose --env-file .env -f docker/docker-compose.yml down
# start again
docker compose --env-file .env -f docker/docker-compose.yml up -d
# health
curl -s http://localhost/api/health
```

---

## Sources (Hostinger docs / research, July 2026)

- Browser terminal: <https://www.hostinger.com/support/7978544-how-to-use-the-browser-terminal-in-hostinger/>
- Connect via SSH: <https://www.hostinger.com/support/5723772-how-to-connect-to-your-vps-via-ssh-at-hostinger/>
- Ubuntu + Docker VPS template: <https://www.hostinger.com/support/8306612-how-to-use-the-docker-vps-template-at-hostinger/>
- Install Docker on Ubuntu (manual + template): <https://www.hostinger.com/tutorials/how-to-install-docker-on-ubuntu>
- Managed VPS firewall: <https://support.hostinger.com/en/articles/8172641-how-to-use-a-managed-vps-firewall>
- UFW on Ubuntu (2026): <https://www.hostinger.com/tutorials/how-to-configure-firewall-on-ubuntu-using-ufw>
- Docker + UFW bypass (background): <https://github.com/chaifeng/ufw-docker>
- KVM 4 specs: <https://www.vpsbenchmarks.com/hosters/hostinger/plans/kvm-4>
