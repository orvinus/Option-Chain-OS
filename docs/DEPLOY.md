# Production deployment and unattended ingestion

This document complements [`RUNBOOK.md`](RUNBOOK.md) with hosting choices, health checks, and how to keep **TimescaleDB** fed during NSE hours **09:15–15:30 IST** without manual dashboard login.

## How ingestion stays on

1. **One long-running backend** process (`uvicorn` or the `backend` Docker service) with `RUN_MODE=live`.
2. **Authenticated XTS market-data session** before the WebSocket can subscribe:
   - **Preferred:** restore the token from DB after a previous login, or **`XTS_LOGIN_AT_STARTUP=true`** with `XTS_MD_APP_KEY` / `XTS_MD_SECRET_KEY` in `.env` (see below).
3. **TimescaleDB reachable** via `DB_URL` / `DB_URL_SYNC`.
4. **Single replica** for the ingest worker: do not run multiple instances against the same XTS appKey.

Free-tier hosts that **scale to zero** or sleep (e.g. idle Render free tier) are a poor fit: the XTS market-data client needs a **persistent** process. Use a **paid always-on** web service, a small VPS, or a home/office PC that stays awake.

## Environment: unattended login

| Variable | Purpose |
|----------|---------|
| `XTS_LOGIN_AT_STARTUP` | Set to **`true`** so the backend logs in to the XTS market-data API on startup (no dashboard click). |
| `XTS_MD_APP_KEY`, `XTS_MD_SECRET_KEY` | Required for startup login. |
| `XTS_MD_BASE_URL` | Your broker's market-data host (demo host by default). |

On boot, the app **first** attempts **`try_restore_session_from_db`**: if a valid token row exists in `auth_sessions`, it reuses it and startup login is skipped. If restore fails and **`XTS_LOGIN_AT_STARTUP=true`**, it logs in fresh from `.env`. If the flag is `false` and restore fails, the feed waits at `ws.awaiting_dashboard_login` until you trigger `POST /api/auth/login`.

## Health and monitoring

Poll **`GET /api/health`** (see [`backend/app/api/health.py`](../backend/app/api/health.py)) during market hours:

- `authenticated: true`
- `feed_connected: true` (when the broker socket is up)
- `last_flush_at` recent (aggregator wrote rows in the last minute or so when ticks flow)

Use UptimeRobot, Grafana Cloud, or similar. HTTP pings alone do **not** replace a running WebSocket; they only detect a dead or sleeping host.

## Database with Timescale

The app expects **Timescale** (hypertable `option_oi_snapshots`). Options:

- Run **`timescale/timescaledb`** from [`docker/docker-compose.yml`](../docker/docker-compose.yml) on the same VM as the backend, **or**
- Use **Timescale Cloud** / another managed Timescale-compatible URL in `DB_URL`.

Generic managed Postgres without the Timescale extension may fail migrations or hypertable creation.

## Scheduling the host (optional)

If you **stop** Docker or the VM overnight, start it **before 09:10 IST** so login, scripmaster, and subscribe finish before the open.

**Windows Task Scheduler**

- Program: `powershell.exe`
- Arguments (adjust paths):

```text
-NoProfile -ExecutionPolicy Bypass -File "D:\trading algo (ayush bhai)\scripts\docker-compose-market-up.ps1"
```

- Trigger: daily at **09:05 IST** (or weekly Mon–Fri if your OS supports it).

**Linux cron** (user that runs Docker):

```cron
5 3 * * 1-5 cd /opt/nifty-oi && /usr/bin/docker compose --env-file .env -f docker/docker-compose.yml up -d
```

The example uses **03:05 UTC** as a rough pre-open for IST; adjust for your timezone and DST.

## Render / Railway / Fly.io notes

- **Render:** Prefer a **paid Web Service** with **no** scale-to-zero for the backend. Attach **Timescale-compatible** Postgres or external Timescale URL.
- **Railway / Fly.io:** Pin **one** instance; set health checks on `/api/health`.
- **“Ping every 5 minutes”** on a sleeping free app only wakes HTTP; it does **not** keep a broker WebSocket subscribed. Treat pings as **monitoring**, not ingestion.

## Related scripts

| Script | Use |
|--------|-----|
| [`scripts/docker-compose-market-up.ps1`](../scripts/docker-compose-market-up.ps1) | Bring up `docker compose` before the session (Task Scheduler). |
