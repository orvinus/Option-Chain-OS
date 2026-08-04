# Runbook

## Prerequisites

| Tool | Version | Notes |
|------|---------|-------|
| Python | 3.11.x | 3.12 also works |
| Node.js | 20.x LTS |  |
| Docker Desktop | latest | Runs TimescaleDB (and the full stack via `docker/docker-compose.yml`) |
| XTS Market Data account | — | Symphony/Shrilakshmi XTS with a **Binary Marketdata** `appKey` + `secretKey` |

## Local Windows setup (recommended)

```powershell
# Step 1 - clone (or open the workspace)
cd "D:\trading algo (ayush bhai)"

# Step 2 - configure credentials
copy .env.example .env
notepad .env   # fill XTS_MD_APP_KEY, XTS_MD_SECRET_KEY (and XTS_MD_BASE_URL if not the demo host)

# Step 3 - start TimescaleDB (one-time setup; container persists)
./scripts/start-timescale.ps1

# Step 4 - start backend (in terminal #1)
./scripts/start-backend.ps1

# Step 5 - start frontend (in terminal #2)
./scripts/start-frontend.ps1
```

Open: <http://localhost:5173>

## Linux / Docker Compose deployment

```bash
cd /opt/nifty-oi   # or your clone path
cp .env.example .env
# fill .env (include API_CORS_ORIGINS for your public URL when not using localhost only)
docker compose --env-file .env -f docker/docker-compose.yml up -d --build
docker compose --env-file .env -f docker/docker-compose.yml logs -f backend
```

Visit <http://your-host>.

## Unattended ingestion (production)

To persist OI into TimescaleDB during **09:15–15:30 IST** without opening the dashboard each day:

1. Fill **`XTS_MD_APP_KEY`** + **`XTS_MD_SECRET_KEY`** (and **`XTS_MD_BASE_URL`** for a non-demo host) in `.env`. Startup login is automatic when `RUN_MODE=live`.
2. Keep **one** backend process always running (`restart: unless-stopped` in Compose, or systemd on a VPS). The WebSocket and bucket flusher run inside that process.
3. After a successful login once, the backend can **restore the last token from `auth_sessions`** on the next restart before falling back to a fresh appKey/secretKey login.

**Single replica:** do not run multiple backend replicas on the same XTS appKey.

> **Login is automatic**, not manual — the backend restores or mints a token on every start. If both fail the feed parks at `ws.awaiting_dashboard_login`; run `POST /api/auth/login` to recover. Note a *dashboard* sign-in does not force a fresh token — only that endpoint does.

**Health:** poll **`GET /api/health`** (`authenticated`, `feed_connected`, `last_flush_at`).

Full hosting options, Render notes, and Task Scheduler examples: **[DEPLOY.md](DEPLOY.md)**.

## Reset the database

```powershell
docker stop timescaledb
docker rm timescaledb
docker volume rm timescale_data
./scripts/start-timescale.ps1
```

Then re-run `./scripts/start-backend.ps1` so Alembic recreates the schema.

## Daily operational checks

| Check | How |
|-------|-----|
| Liveness | `curl http://localhost:8000/api/health` should return `status:"ok"` and `authenticated:true` |
| Last flush | The `last_flush_at` field must be < 90s old during market hours |
| Subscribed tokens | `tokens_subscribed` should match expectation from `STRIKE_WINDOW * 2 * len(EXPIRIES)` |
| Spot ticking | `latest_spot` must change every few seconds during market hours |

## Trading hours awareness

NSE F&O hours: **09:15 – 15:30 IST, Mon–Fri**.

Outside trading hours the WebSocket will still connect and the cache will
serve the last known book, but no new ticks will arrive (no live updates,
`last_flush_at` will go stale until the next session).

## Configuration reference

| Env var          | Default                | Description |
|------------------|------------------------|-------------|
| `STRIKE_WINDOW`  | `50`                   | Strikes either side of ATM (so 50 means 100 strikes total) |
| `STRIKE_STEP`    | `50`                   | NIFTY weekly grid is 50 (use 100 for far months) |
| `EXPIRIES`       | `current_weekly`       | Comma-separated: `current_weekly,next_weekly,current_monthly,next_monthly` |
| `PERSIST_BUCKET` | `1s`                   | `1s`, `5s`, or `1min`. **Use `1s`** for the sub-minute timeframes (`1s/15s/30s/45s`) — see note below. |
| `RUN_MODE`       | `live`                 | `live` connects to the XTS feed; `replay` skips ingestion (REST only) |
| `DEBUG_TICKS`    | `false`                | Verbose tick logging |
| `XTS_MD_APP_KEY` / `XTS_MD_SECRET_KEY` | (empty) | Binary Marketdata credentials; required for any login |
| `NIFTY_INDEX_TOKEN` | `26000`             | XTS NSECM instrument id for the NIFTY 50 spot |

> **Sub-minute timeframes need `PERSIST_BUCKET=1s`.** OI Change is computed as
> `OI(now) − OI(now − window)`, so a stored snapshot must exist *between* `now`
> and `now − window`. With `1min` buckets the `1s/15s/30s/45s` deltas have no
> intra-minute baseline and always read 0. `1s` fixes them; `5s` covers
> everything ≥ 5s. Restart the backend after changing this.

### Vite and `VITE_*` variables

The dev server loads `.env` from the **repository root** (parent of `frontend/`), not only from `frontend/.env`. Set **`VITE_API_BASE`** to your FastAPI HTTP origin (for example `http://localhost:8000`). The Vite config proxies **`/api`** and **`/ws`** to that target; the WebSocket proxy target must be **http(s)**, not `ws://`. In dev, the browser WebSocket defaults to **same-origin** (`ws://localhost:5173/ws/...`) so it always goes through the proxy—leave **`VITE_WS_BASE` unset** unless you intentionally want a direct socket (then set `VITE_DEV_WS_DIRECT=true` and `VITE_WS_BASE`).

## Troubleshooting

### Missing XTS credentials at startup
Open `.env` and fill `XTS_MD_APP_KEY` + `XTS_MD_SECRET_KEY` (and `XTS_MD_BASE_URL`
if not on the demo host). Restart the backend.

### Feed stuck at `ws.awaiting_dashboard_login`
The startup login failed (bad credentials, or the broker was unreachable). Run
`curl -X POST http://localhost:8000/api/auth/login` — the only path that forces a
fresh token.

### `401 / Invalid appKey or secretKey`
Wrong or blank `XTS_MD_APP_KEY` / `XTS_MD_SECRET_KEY`, or `XTS_MD_BASE_URL`
pointing at the wrong host (demo vs. live broker). Fix `.env` and re-login.

### Frontend stays on "Loading…" forever
Open the browser console. If you see CORS errors, check that
`API_CORS_ORIGINS` in `.env` includes `http://localhost:5173`.

### `feed_connected` flaps every ~80 seconds
Normal. The XTS gateway drops and reconnects the market-data socket roughly
every 83s. Ingestion is hardened to carry OI forward across reconnects, so
stored data stays clean.

### `STRIKE_WINDOW` chosen too wide
Each instrument costs subscription slots on **two** message codes (touchline
`1501` + OI `1510`). XTS enforces an appKey-specific subscription cap
(sometimes as low as 50). Lower `STRIKE_WINDOW` or raise the cap on the broker
dashboard if ticks stop arriving.

### Replay endpoint times out
Reduce the window or increase the step:
```
GET /api/replay?start=...&end=...&step=15m&expiry=...
```

## Daily ScripMaster refresh (recommended)

Schedule once-a-day refresh via Windows Task Scheduler:

```powershell
# Action -> Start a program
# Program: powershell.exe
# Arguments: -NoProfile -ExecutionPolicy Bypass -File "D:\trading algo (ayush bhai)\scripts\refresh-scripmaster.ps1"
```

…or just call:
```powershell
& "D:\trading algo (ayush bhai)\backend\.venv\Scripts\python.exe" `
  "D:\trading algo (ayush bhai)\scripts\seed_scripmaster.py"
```

The backend also auto-refreshes on startup if the cache is older than 20h.
