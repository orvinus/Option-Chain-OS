# Single-VPS deployment plan (Docker Compose, lowest cost)

This plan deploys the full stack from [`docker/docker-compose.yml`](../docker/docker-compose.yml) on one Linux VPS: **TimescaleDB**, **FastAPI backend**, and **nginx-served frontend**. It matches the constraints in [`DEPLOY.md`](DEPLOY.md) (always-on process, single ingest replica, Timescale extension).

For **provider comparison and ballpark monthly cost**, see [`VPS-PROVIDER-PRICING.md`](VPS-PROVIDER-PRICING.md).

---

## 1. Target architecture

| Component | How it runs | Ports (public) |
|-----------|-------------|------------------|
| TimescaleDB | `timescale/timescaledb` container | **5432 should stay private** (only localhost / Docker network) |
| Backend | `uvicorn` after `alembic upgrade` | **8000 private** (nginx talks to it) |
| Frontend | nginx static + reverse proxy | **80** (and **443** after TLS) |

The bundled [`docker/nginx.conf`](../docker/nginx.conf) proxies `/api/` and `/ws/` to the backend, so the browser can use **same-origin** URLs in production without rebuilding the frontend for a separate API host.

---

## 2. VPS sizing (minimum sensible)

| Resource | Recommendation | Why |
|----------|-----------------|-----|
| RAM | **≥ 4 GB** | Postgres + continuous aggregates + Python + headroom |
| vCPU | **≥ 1** (2 preferred) | Ingest + API + occasional builds |
| Disk | **≥ 40 GB** NVMe/SSD | Docker images, Timescale volume growth |
| Network | Stable egress | XTS market-data WebSocket + REST |

**One replica only:** do not run two backend containers against the same XTS appKey.

---

## Hostinger KVM 2 — host the whole software here

**KVM 2** (as listed on Hostinger’s KVM line: **2 vCPU, 8 GB RAM, 100 GB NVMe**, multi-TB bandwidth) is **more than enough** for TimescaleDB + backend + nginx in [`docker/docker-compose.yml`](../docker/docker-compose.yml). The procedure is the same as any Ubuntu KVM VPS; below is a **KVM 2–shaped checklist** tied to Hostinger’s flow.

### A. Buy and install the OS

1. In Hostinger **hPanel**, open **VPS** and order or upgrade to **KVM 2**.
2. **OS template:** choose **Ubuntu 24.04 LTS** or **Ubuntu 22.04 LTS** (not “WordPress hosting” — you need full root/KVM).
3. Add your **SSH public key** in the panel if offered; otherwise set a strong root password and switch to key-based SSH after first login.
4. Note the VPS **public IPv4** and SSH port (often **22**).

### B. Panel firewall (if hPanel exposes a VPS firewall)

- Allow **inbound:** SSH (your port), **80/tcp**, and **443/tcp** when you add HTTPS later.
- **Do not** open **5432** (Postgres) or **8000** (FastAPI) to the world; only nginx on **80/443** should be public.

Then still configure **UFW on the server** as in [Phase D](#6-phase-d--firewall) so rules stay even if you change panels.

### C. Put the project on the server (no spaces in path)

Use a Linux path such as **`/opt/nifty-oi`** (avoid Windows-style paths with spaces).

- **Git:** `sudo mkdir -p /opt && sudo chown $USER:$USER /opt && cd /opt && git clone <your-repo-url> nifty-oi`
- **Private repo / no git on server:** zip the repo on your PC (excluding `node_modules`, `.venv`, large caches) and `scp` it up, then unzip under `/opt/nifty-oi`.

### D. Environment file

1. `cd /opt/nifty-oi && cp .env.example .env && nano .env` (or `vim`).
2. Set **XTS** credentials (`XTS_MD_APP_KEY`, `XTS_MD_SECRET_KEY`, `XTS_MD_BASE_URL`); startup login is automatic in live mode, so ingestion resumes after a reboot (details in [`DEPLOY.md`](DEPLOY.md)).
3. **Database:** [`docker-compose.yml`](../docker/docker-compose.yml) sets `DB_URL` / `DB_URL_SYNC` to the **`timescaledb`** service for the backend container, so you do **not** need to edit `.env` for DB host on the VPS (unless you change the Postgres password in compose and match it in those URLs).
4. **`API_CORS_ORIGINS`:** in `.env`, set origins that match how you open the UI, e.g. `http://YOUR_PUBLIC_IP` (comma-separated list). Compose passes this into the backend via `${API_CORS_ORIGINS:-…}`; add `https://your-domain` when you use TLS.

### E. Install Docker and start the stack

Follow [Phase C](#5-phase-c--docker-engine--compose) (Docker Engine + Compose plugin), then:

```bash
cd /opt/nifty-oi
docker compose --env-file .env -f docker/docker-compose.yml up -d --build
docker compose --env-file .env -f docker/docker-compose.yml logs -f backend
```

First boot runs **Alembic** then **uvicorn**. When logs look stable, open **`http://YOUR_PUBLIC_IP`** in a browser (same machine nginx proxies `/api/` and `/ws/`).

### F. Smoke checks

```bash
curl -sS "http://127.0.0.1/api/health"   # from the VPS after UFW allows localhost, or use public IP from laptop
```

During NSE hours, confirm `authenticated` / `feed_connected` per [`DEPLOY.md`](DEPLOY.md).

### G. Hostinger snapshots vs your DB backups

Use **hPanel snapshots** before risky upgrades. For the database, still schedule **`pg_dump`** (see [Phase G](#9-phase-g--backups-you-operate-these)) so you can restore logic to another host; do not assume panel backup alone covers Docker volumes until Hostinger’s docs say it does for your plan.

---

## 3. Phase A — Provision the server

1. Create a **KVM VPS** (not shared “web hosting” without root) with Ubuntu **22.04 or 24.04 LTS**.
2. Add your **SSH public key**; disable password SSH after first login (optional but recommended).
3. Note the **public IPv4** (or IPv6-only if your broker and clients support it end-to-end).
4. (Optional) Point a **DNS A record** at the VPS for HTTPS later.

---

## 4. Phase B — Base hardening

On the server (as a sudo user):

1. `sudo apt update && sudo apt upgrade -y`
2. Install essentials: `git`, `curl`, `ufw`, `fail2ban` (optional), `unattended-upgrades` (optional).
3. **Time sync:** ensure `systemd-timesyncd` or `chrony` is correct; market windows in docs assume sensible IST wall-clock on *your* monitoring side (the server should use UTC internally—normal).

---

## 5. Phase C — Docker Engine + Compose

1. Install [Docker Engine](https://docs.docker.com/engine/install/ubuntu/) and the **Compose plugin** (`docker compose`).
2. Add your deploy user to the `docker` group, re-login, verify `docker run hello-world`.
3. Prefer **Docker volumes** for `timescale_data` (already defined in compose) so upgrades do not wipe DB data.

---

## 6. Phase D — Firewall

Use **UFW** (or cloud security groups) so only required ports are open:

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp   # after you terminate TLS (see phase H)
sudo ufw enable
```

**Do not** publish PostgreSQL `5432` to the public internet.

---

## 7. Phase E — Application secrets and env

1. Clone this repository to a path **without spaces** on Linux (e.g. `/opt/nifty-oi`), or copy only `docker/`, `backend/`, `frontend/`, and needed files.
2. Copy `.env.example` to `.env` on the server (not committed).
3. Set at least:
   - **XTS:** `XTS_MD_APP_KEY`, `XTS_MD_SECRET_KEY`, `XTS_MD_BASE_URL` — unattended boot needs no extra flag (see [`DEPLOY.md`](DEPLOY.md)).
   - **DB:** The backend container uses **`timescaledb`** URLs from [`docker-compose.yml`](../docker/docker-compose.yml); change those lines only if you use external Postgres/Timescale or a non-default password.
   - **CORS:** Set `API_CORS_ORIGINS` in `.env` to your **public origin** (e.g. `http://YOUR_IP` or `https://oi.example.com`). Use **`docker compose --env-file .env -f docker/docker-compose.yml …`** from the repo root so this value is applied (see phase F).

Compose passes `../.env` into the backend via `env_file`. For **`API_CORS_ORIGINS`**, [`docker-compose.yml`](../docker/docker-compose.yml) uses `${API_CORS_ORIGINS:-…}` so your repo-root `.env` value is applied when you invoke Compose with **`--env-file .env`** from the repo root (see phases F and Hostinger section E).

---

## 8. Phase F — First boot and migrations

From the **repository root** (so paths like `build.context: ..` resolve correctly and `.env` is picked up for CORS substitution):

```bash
cd /opt/nifty-oi   # example
docker compose --env-file .env -f docker/docker-compose.yml up -d --build
docker compose --env-file .env -f docker/docker-compose.yml logs -f backend
```

Health check (from your laptop or UptimeRobot):

```text
GET http://YOUR_SERVER_IP/api/health
```

During market hours you want `authenticated` and `feed_connected` healthy per [`DEPLOY.md`](DEPLOY.md).

---

## 9. Phase G — Backups (you operate these)

**Provider “weekly backup”** (e.g. some Hostinger plans) is **not** a substitute for a tested **logical** DB backup unless you confirm it snapshots the whole disk including Docker volumes the way you expect.

Recommended pattern:

1. **Nightly `pg_dump`** from a small script running on the host (or a `cron` container) using the **postgres** superuser over the Docker network, **not** over the public internet.
2. Encrypt and upload dumps to **object storage** (S3-compatible, Backblaze B2, etc.) or download with `rsync`/`scp` to another machine.
3. Monthly **restore drill** on a throwaway VM to prove the backup is usable.

Example dump (run from **repository root**; adjust backup path):

```bash
cd /opt/nifty-oi
docker compose --env-file .env -f docker/docker-compose.yml exec -T timescaledb pg_dump -U postgres -Fc oi > "/backup/oi_$(date +%F).dump"
```

Retention: keep 7 daily + 4 weekly or match your risk tolerance.

---

## 10. Phase H — TLS (recommended before production)

1. Install **Caddy** or **Certbot + nginx** on the host **in front of** the compose frontend, **or** terminate TLS on a separate reverse proxy.
2. Obtain a certificate for your domain; force HTTPS; update `API_CORS_ORIGINS` to `https://your-domain`.

Avoid exposing raw port 8000 publicly once TLS is in place.

---

## 11. Phase I — Monitoring and ops

1. HTTP monitor on `GET /api/health` during **09:15–15:30 IST** (per [`DEPLOY.md`](DEPLOY.md)).
2. If you stop the VM overnight, start it **before 09:10 IST** so login and subscribe complete before the open.
3. `docker compose --env-file .env -f docker/docker-compose.yml pull && docker compose --env-file .env -f docker/docker-compose.yml up -d --build` for image updates; always check migrations.

---

## 12. Cost stack reminder

- **VPS:** see [`VPS-PROVIDER-PRICING.md`](VPS-PROVIDER-PRICING.md).
- **Domain:** roughly **USD 10–15/year** at common registrars.
- **Object storage for backups:** often **under USD 5/mo** at small data sizes.
- **Monitoring:** many tools have a free tier for a single HTTP check.

---

## 13. Related repository docs

- [`DEPLOY.md`](DEPLOY.md) — unattended login, health semantics, Render/Railway caveats.
- [`RUNBOOK.md`](RUNBOOK.md) — local setup and troubleshooting.
