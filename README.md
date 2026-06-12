# NIFTY Open Interest Change Analytics Platform

Production-grade real-time options analytics dashboard for NIFTY, ingesting OI
ticks directly from the **Shrilakshmi Fintech / Symphony XTS** Binary Market
Data WebSocket stream and visualizing strike-wise call/put OI change across
configurable timeframes.

## Stack

- **Backend**: Python 3.11, FastAPI, asyncio, SQLAlchemy 2, Alembic,
  Symphony XTS Market Data API (login + binary/JSON market-data socket)
- **Database**: PostgreSQL 16 + TimescaleDB (hypertable + continuous aggregate)
- **Frontend**: React 18, TypeScript, Vite, TailwindCSS, Apache ECharts
- **Transport**: REST + WebSocket fan-out hub

## Run on a brand-new PC (portable zip)

Moving the whole product to another Windows machine that has **nothing
installed**? Just two steps:

1. **Unzip** this product folder anywhere (Desktop, Documents — any path works).
2. **Double-click [`Setup-and-Run.bat`](Setup-and-Run.bat)** and click **Yes** on
   the Administrator (UAC) prompt.

That single button ([`scripts/bootstrap.ps1`](scripts/bootstrap.ps1)) does
*everything*:

- Installs **Docker Desktop + WSL2** automatically via `winget` if they're
  missing (first run only — a one-time restart may be required; the script
  re-registers itself to **resume automatically** after you sign back in).
- Starts the Docker daemon and waits for it to be ready.
- Uses the **`.env` bundled in the zip**, so no credentials need to be typed.
- Brings up the full stack (database + backend + frontend) and opens
  [http://localhost](http://localhost) once the backend is healthy.

Press **Ctrl+C** in the window to stop everything. Double-click
`Setup-and-Run.bat` again to relaunch.

> **Packaging note:** when you create the zip, make sure the filled **`.env` is
> included** (it carries your `XTS_MD_*` keys). Anyone with the zip can use those
> keys, so share it only with trusted machines. Requirements on the target:
> Windows 10 (1809+) / 11, an internet connection for the first install, and
> admin rights.

## One-Click Start (Windows)

Already have Docker installed? The fastest way to launch the whole stack —
TimescaleDB, FastAPI backend, and frontend — in a single window:

1. **Configure credentials** (first run only): copy `.env.example` to `.env` and
   fill in your XTS market-data values (`XTS_MD_APP_KEY`, `XTS_MD_SECRET_KEY`,
   and `XTS_MD_BASE_URL` if you're not on the Symphony demo host).
2. **Double-click [`start.bat`](start.bat)** (or pin it to the taskbar / make a
   Desktop shortcut).

`start.bat` runs [`scripts/start-all.ps1`](scripts/start-all.ps1), which:

- Checks Docker Desktop and starts it automatically if it isn't running (waits
  up to 60s).
- Verifies `.env` exists with the required `XTS_MD_*` keys filled — if it's
  missing it copies `.env.example` and tells you to fill it in, then exits.
- Spawns a background watcher that opens your browser to
  [http://localhost](http://localhost) once the backend health check passes.
- Brings up the full stack via
  `docker compose --env-file .env -f docker/docker-compose.yml up --build` with
  colored, service-prefixed interleaved logs.

Press **Ctrl+C** in the window to tear everything down. Re-run `start.bat` to
launch again.

## Verify it's running

Once the stack is up, three containers run:

| Service       | URL / Port               | Notes                              |
|---------------|--------------------------|------------------------------------|
| Frontend      | http://localhost (80)    | The dashboard — open this          |
| Backend API   | http://localhost:8000    | FastAPI                            |
| TimescaleDB   | localhost:5432           | Postgres 16 + TimescaleDB          |

Check backend health and feed status:

```powershell
curl http://localhost:8000/api/health
```

A healthy live response looks like:

```json
{
  "status": "ok",
  "authenticated": true,
  "feed_connected": true,
  "run_mode": "live",
  "latest_spot": 23355.85,
  "tokens_subscribed": 46,
  "expiries": ["2026-06-16"],
  "active_symbol": "NIFTY"
}
```

If `authenticated` is `false` or the feed sits at `ws.awaiting_dashboard_login`,
trigger the XTS login from the dashboard button or `POST /api/auth/login`
(see [Authentication](#authentication)).

**Stop the stack** (when started with compose directly):

```powershell
docker compose -f docker/docker-compose.yml down
```

## Quick Start (manual, Windows local)

Prefer to run each service yourself (e.g. for backend/frontend hot-reload
during development):

```powershell
# 1. Start TimescaleDB (one-time, runs in Docker Desktop)
./scripts/start-timescale.ps1

# 2. Configure credentials
copy .env.example .env
# edit .env and fill XTS_MD_* values

# 3. Run the backend
./scripts/start-backend.ps1

# 4. In a second terminal, run the frontend
./scripts/start-frontend.ps1
```

Then open [http://localhost:5173](http://localhost:5173).

**Frontend env:** Vite reads `VITE_*` from the **repository root** `.env` (see [`frontend/vite.config.ts`](frontend/vite.config.ts)). Set `VITE_API_BASE` to your FastAPI origin (for example `http://localhost:8000`). During `npm run dev`, the WebSocket client defaults to **same-origin** (`/ws` → Vite proxy → that API), so you can leave `VITE_WS_BASE` unset; set it (or `VITE_DEV_WS_DIRECT=true`) only if you need a direct WS URL in development.

See [`docs/RUNBOOK.md`](docs/RUNBOOK.md) for setup and troubleshooting. For
**always-on ingestion**, health checks, and hosting (VPS / Render / Task Scheduler),
see [`docs/DEPLOY.md`](docs/DEPLOY.md).

## Architecture

```
Symphony XTS Market Data WS  ->  ws_client (asyncio queue)
                              ->  1m aggregator  ->  TimescaleDB hypertable
                                                     |
                                                     v
                                            OI Change Engine (timeframe-aware)
                                                     |
                                            +--------+---------+
                                            |                  |
                                       FastAPI REST       /ws/oi-stream hub
                                            |                  |
                                            +--------+---------+
                                                     |
                                              React + ECharts
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Authentication

This platform logs in to the **Symphony XTS Market Data API** server-side using
your `XTS_MD_APP_KEY` / `XTS_MD_SECRET_KEY` — market-data access needs only
these two keys (no client code, MPIN, or TOTP).

Auth is **manual by default** (`XTS_LOGIN_AT_STARTUP=false`): on boot the
backend tries to restore the last token from the `auth_sessions` table, and if
that fails you trigger a fresh login via the dashboard button or
`POST /api/auth/login`. Until then the feed sits at
`ws.awaiting_dashboard_login`. Set `XTS_LOGIN_AT_STARTUP=true` for unattended
servers so the backend logs in from `.env` on startup. See
[`docs/AUTH.md`](docs/AUTH.md).


---

 cd "d:\trading algo (ayush bhai)\trading algo (ayush bhai)" && docker compose --env-file .env -f docker/docker-compose.yml up --build