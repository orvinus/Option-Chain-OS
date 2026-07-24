# Local development — the single-session rule (READ THIS FIRST)

Production (`oialgo.tech`) and your local PC **cannot both run a live broker feed on the
same appKey**. The XTS/Lakshmishree market-data API allows **exactly one live session
per appKey**. If two instances log in with the same key they **ping-pong-steal** the
session from each other every ~60–80 s, and **both** end up with gap-ridden, wrong data.

This is the root cause of the "data goes wrong every 2–3 days" bug: opening `localhost`
auto-logged the local backend into the broker and knocked production offline.

## The rule

| Instance | `RUN_MODE` | Broker login? | appKey |
|----------|-----------|---------------|--------|
| **Production** (VPS) | `live` | yes | the production key |
| **Local** (your PC) — default | `replay` | **never** | none needed |
| **Local** — live testing (rare) | `live` | yes | a **separate, dedicated** key |

## Default: local runs in REPLAY (safe, full dev sandbox)

Your local `.env` ships with `RUN_MODE=replay`. In replay the backend serves the whole
historical database over the same REST/WS APIs, so **every UI and analytics feature works**
(OI-Change, Charts, Multi-TF, Ratio, Replay, greeks, export, symbol switching, …) — you
just don't get a live tick feed. This is all you need for normal feature work.

Two independent guards make it **impossible** for a replay instance to touch the broker:

1. **Backend** (`backend/app/api/auth.py`): `POST /api/auth/login` and `/api/auth/hidden-login`
   refuse with **409** when `RUN_MODE != live`; the ingestion bootstrap no-ops in replay.
2. **Frontend** (`frontend/src/hooks/useMarketContext.ts`): the auto-connect effect is
   skipped when health reports `run_mode: "replay"` — the dashboard never even asks.

Extra belt-and-suspenders on this PC: the local `.env` leaves `XTS_MD_APP_KEY` /
`XTS_MD_SECRET_KEY` **blank**, so even a stale/older image cannot form a session
(`xts_client.login()` raises before any network call when a key is blank).

```powershell
# Start the local replay stack (safe any time — cannot compete with production)
docker compose --env-file .env -f docker/docker-compose.yml up -d --build
# Open http://localhost   → header shows "API run mode: replay"
```

## Rare: testing the LIVE feed locally

Only needed when you are changing the ingestion/feed code itself (ws_client, subscribe,
self-heal, aggregator). Everyday work does **not** need this.

> **⚠️ NEVER reuse the production appKey for a local live run.** That is exactly what
> causes the outage. Use a **second, dedicated** broker appKey (a separate Lakshmishree
> API subscription). If you have only one key, the only alternative is to **stop
> production first** — but that takes the public site offline, so a second key is
> strongly preferred.

```dotenv
# local .env — LIVE TESTING ONLY, with a DEDICATED key
RUN_MODE=live
XTS_MD_APP_KEY=<your SECOND appKey — not production's>
XTS_MD_SECRET_KEY=<your SECOND secretKey>
```

Then rebuild local. The backend gate allows login in live mode; because the key is
different, there is zero competition and both instances stay live.

## Verifying correctness — compare production to the market, not to local

Because local (replay) and production (live) have **separate databases** and local no
longer captures live data, their numbers will **not** match — that's expected. Judge
correctness by comparing **production vs Sensibull / NSE**, on the VPS:

```bash
docker compose --env-file .env -f docker/docker-compose.yml \
  exec backend python -m validation.run_all --symbol NIFTY --round 1
```

Layer B is an exact independent recompute of the OI-change math; Layers D/E cross-check
Sensibull and NSE within the ATM window.

See also: [`RUNBOOK.md`](RUNBOOK.md) (local setup), [`UPDATE-PRODUCTION.md`](UPDATE-PRODUCTION.md)
(pull + rebuild on the VPS), [`DEPLOY.md`](DEPLOY.md) (login/health semantics).
