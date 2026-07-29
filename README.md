# Ayush Bhai — NIFTY 50 & SENSEX OI-Change Analytics

> **Branch:** `ayush-bhai-branch`
> A focused build of the Open-Interest analytics platform, scoped for the
> **Ayush Bhai** project: only two underlyings (**NIFTY 50** and **SENSEX**) and a
> single, streamlined **OI Change** view. Everything else from the general
> platform (extra symbols, the OI-Absolute and Net-OI-Calculator tabs) is removed.

Production-grade real-time options analytics dashboard that ingests Open-Interest
ticks from the **Shrilakshmi Fintech / Symphony XTS** Binary Market Data
WebSocket stream and visualizes strike-wise call/put **OI change** across
configurable timeframes and custom time windows.

---

## What this branch is (and how it differs from `main`)

| Aspect | `main` | `ayush-bhai-branch` (this) |
|--------|--------|----------------------------|
| Symbols | 136 NSE underlyings | **Only NIFTY 50 (NSE) + SENSEX (BSE)** |
| Exchanges | NSE only | **NSE + BSE** (full SENSEX/BSE integration) |
| Dashboard views | OI Change, OI Absolute, Net OI Calculator | **OI Change only** |
| Everything else | — | unchanged (same engine, feed, DB, launcher) |

`main` is never modified by this branch — all of the above lives here.

---

## Features

- **Two underlyings:** NIFTY 50 (NSE) and SENSEX (BSE), switchable from the header dropdown.
- **OI Change chart:** strike-wise call/put OI change around ATM, live via WebSocket.
- **Preset timeframes:** 1m, 3m, 5m, 10m, 15m, 30m, 1h, 2h, 3h, Full Day.
- **Custom time-range slider:** drag a 9:15 AM → now window to compute OI change over any span; leave the right handle at "now" for a live, left-anchored window.
- **KPIs:** total call/put OI change, PCR, ratio, spot / ATM.
- **ATM ± strike window** control to focus the chart.

> **Note on OI granularity:** NSE/BSE disseminate Open Interest only ~once per
> minute, so OI-change timeframes below 1 minute are intentionally **not** exposed
> here — there is no sub-minute OI to show. (Price/LTP changes faster, but this
> dashboard is OI-focused.)

---

## Stack

- **Backend:** Python 3.11, FastAPI, asyncio, SQLAlchemy 2, Alembic, Symphony XTS Market Data API (login + JSON market-data socket)
- **Database:** PostgreSQL 16 + TimescaleDB (hypertable)
- **Frontend:** React 18, TypeScript, Vite, TailwindCSS, Apache ECharts
- **Transport:** REST + WebSocket fan-out hub

---

## Run on a brand-new PC (portable zip)

Moving the product to a Windows machine with **nothing installed**:

1. **Unzip** the product folder anywhere.
2. **Double-click [`Setup-and-Run.bat`](Setup-and-Run.bat)** and click **Yes** on the UAC prompt.

The bootstrap ([`scripts/bootstrap.ps1`](scripts/bootstrap.ps1)) installs Docker
Desktop + WSL2 via `winget` if missing (one-time restart may be needed — it
resumes automatically after sign-in), starts Docker, uses the bundled `.env`,
brings up the full stack, and opens [http://localhost](http://localhost).

> **Packaging note:** include the filled **`.env`** (your `XTS_MD_*` keys) in the
> zip. Anyone with the zip can use those keys — share only with trusted machines.

## One-Click Start (Docker already installed)

1. **Configure credentials** (first run): copy `.env.example` to `.env` and fill
   `XTS_MD_APP_KEY`, `XTS_MD_SECRET_KEY` (and `XTS_MD_BASE_URL` if not the demo host).
2. **Double-click [`start.bat`](start.bat)** → runs
   [`scripts/start-all.ps1`](scripts/start-all.ps1), which checks Docker, validates
   `.env`, opens the browser when healthy, and runs
   `docker compose --env-file .env -f docker/docker-compose.yml up --build`.

Press **Ctrl+C** to stop. Re-run to relaunch.

## Quick Start (manual, hot-reload dev)

```powershell
./scripts/start-timescale.ps1          # 1. TimescaleDB (Docker)
copy .env.example .env                  # 2. fill XTS_MD_* values
./scripts/start-backend.ps1            # 3. backend
./scripts/start-frontend.ps1           # 4. frontend (second terminal)
```

Then open [http://localhost:5173](http://localhost:5173).

---

## Verify it's running

| Service     | URL / Port            | Notes                     |
|-------------|-----------------------|---------------------------|
| Frontend    | http://localhost (80) | The dashboard — open this |
| Backend API | http://localhost:8000 | FastAPI                   |
| TimescaleDB | localhost:5432        | Postgres 16 + TimescaleDB |

```powershell
curl http://localhost:8000/api/health   # status, authenticated, feed_connected, active_symbol
docker compose -f docker/docker-compose.yml down   # stop the stack
```

---

## Architecture

```
Symphony XTS Market Data WS  ->  ws_client (asyncio queue)
                              ->  aggregator  ->  TimescaleDB hypertable
                                                     |
                                                     v
                                            OI Change Engine (timeframe + custom range)
                                                     |
                                            +--------+---------+
                                            |                  |
                                       FastAPI REST       /ws/oi-stream hub
                                            |                  |
                                            +--------+---------+
                                                     |
                                              React + ECharts
```

The underlying's **exchange** (NSE/BSE) drives segment selection end-to-end: the
instrument master is fetched for both `NSEFO` + `BSEFO`, and the index spot +
option subscriptions are placed on the matching XTS segment (NSECM/NSEFO for
NIFTY, BSECM/BSEFO for SENSEX). See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Authentication

Server-side login to the **Symphony XTS Market Data API** with
`XTS_MD_APP_KEY` / `XTS_MD_SECRET_KEY` (no client code / MPIN / TOTP). Manual by
default (`XTS_LOGIN_AT_STARTUP=false`): on boot the backend restores the last
token from `auth_sessions`; otherwise trigger login via the dashboard button or
`POST /api/auth/login`. Set `XTS_LOGIN_AT_STARTUP=true` for unattended servers.
See [`docs/AUTH.md`](docs/AUTH.md).

---

## ⚠️ SENSEX requires BSE market-data access

SENSEX is a **BSE** index. The pipeline now fully supports BSE (instrument
master, index-token resolution, and feed subscription are all exchange-aware),
**but** streaming SENSEX requires your XTS / Shrilakshmi appKey to be **entitled
to BSE (BSEFO + BSECM) market data**. If it isn't, SENSEX will appear in the
dropdown but won't stream until your broker enables BSE on the appKey — that is a
broker-side entitlement, not a code limitation. NIFTY 50 works regardless.

Tuning for SENSEX (in `.env` / `data/symbols.json`): `strike_step` 100,
`lot_size` ~20 (confirm against the live contract); a wide `STRIKE_WINDOW` can
exceed the appKey's subscription cap — lower it if SENSEX ticks stop arriving.

---

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — system design, OI engine, caching
- [`docs/AUTH.md`](docs/AUTH.md) — XTS authentication
- [`docs/RUNBOOK.md`](docs/RUNBOOK.md) — setup, config reference, troubleshooting
- [`docs/DEPLOY.md`](docs/DEPLOY.md) — unattended ingestion, VPS / hosting
