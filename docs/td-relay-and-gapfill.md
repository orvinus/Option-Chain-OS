# TrueData relay (one login, many backends) + boot-time gap fill

Added 2026-09-02. Two independent pieces, both driven by the same vendor constraint.

## 1. The constraint

TrueData's realtime push service (port 8084 in production, 8086 on the trial) accepts **one
login per user**. A second connection is rejected with "User Already Connected" and the first
one keeps running. Historically that meant the VPS and a dev PC could never both be live, and any
restart landed in a ~60 s "already connected" window.

The REST/history service is separate and is not limited that way.

## 2. Relay architecture

```
            vendor push socket (the ONE login)
                       │
             ┌─────────▼──────────┐
             │  OWNER backend     │  FEED_VENDOR=truedata
             │  TrueDataFeedClient│  TD_RELAY_ENABLED=true
             │        │ TeeQueue  │  TD_RELAY_KEY=<secret>
             │   ┌────┴────┐      │
             │ local queue  RelayHub ──► /ws/td-relay?key=…
             │ (aggregator) (per-follower bounded queues, drop-oldest)
             └─────────────────────┘
                       │ wss (same TLS terminator as the API)
        ┌──────────────┼──────────────────┐
        ▼              ▼                  ▼
  FOLLOWER A      FOLLOWER B         FOLLOWER N       FEED_VENDOR=td_relay
  TdRelayFeedClient → its own tick queue → its own aggregator/DB/health/engines
```

- **Owner** (`backend/app/ws/td_relay.py`): `TeeQueue` wraps the feed's out-queue so every
  `Tick` is written to the owner's own queue first and then mirrored to `RelayHub`. The hub keeps
  one bounded queue per follower (5 000 frames, drop-oldest) so a slow follower can only lose its
  own frames, never slow the owner. The route accepts, checks `TD_RELAY_KEY` in constant time,
  sends a `hello`, then streams `tick` frames and a `ping` every 15 s.
- **Follower** (`backend/app/ingest/td_relay_feed.py`): same duck-typed surface as the vendor
  clients (`start/stop/nudge_reconnect/swap_subscription`, health properties, `feed_stats`), so
  `feed_factory`, the spot refresher, `/api/health`, the symbol controller and the failover poller
  need no special cases. Frames become `Tick(origin="ws", vendor="truedata")` on the follower's
  queue, so its freshness ladder sees a normal live transport. 40 s without any frame drops
  `feed_connected` and triggers a reconnect with jittered backoff.
- **No steward on followers.** There is no vendor session to rotate, and running the TrueData
  ladder there would try to log the *owner* out. `rt.steward` stays `None` (health tolerates it).
- **Followers are mirrors, not remote controls.** `swap_subscription` only refreshes the
  follower's own `rt.tokens`/`rt.expiries`; the contracts on the wire are the owner's ATM window.
  If two followers wanted different symbols the owner would have to arbitrate inside the ~50
  instrument cap, so v1 does not attempt it.
- **Wire format**: one JSON object per text frame, fields `ts, tok, sym, exp, k, ot, ltp, oi,
  vol, und, vts`. `origin` is deliberately not on the wire.

### Configuration

| Role | Variables |
|---|---|
| Owner (VPS) | keep `FEED_VENDOR=truedata`; add `TD_RELAY_ENABLED=true`, `TD_RELAY_KEY=<long random>` |
| Follower (dev PC, second VPS) | `FEED_VENDOR=td_relay`, `TD_RELAY_URL=wss://oialgo.tech/ws/td-relay`, `TD_RELAY_KEY=<same>` |

Followers keep `TRUEDATA_USER/PASSWORD` for REST (gap fill, failover poller). Caddy on the VPS
already proxies `/ws/` with upgrade headers, so no server change is needed beyond the env vars
and a backend restart. The route is mounted unconditionally but answers with an error frame and
closes unless `TD_RELAY_ENABLED` is set.

### Rollout

1. VPS `.env`: add the two owner variables, `docker compose … up -d --build backend`.
2. Dev PC `.env`: switch `FEED_VENDOR=td_relay`, add URL + key, restart the local backend.
3. Verify on the follower: `/api/health/feed` → `live.vendor == "td_relay"`, `connected: true`,
   `owner.active_symbol`; `/api/health` → `feed_connected: true`, `ws_last_tick_at` advancing.
4. Verify on the owner: `/api/health/feed` unchanged; relay stats are available through
   `get_relay_hub().snapshot()` (clients, published, dropped).

## 3. Boot-time gap fill

`backend/app/ingest/gapfill.py`, supervised task `gapfill`, live mode only.

- `GAPFILL_DELAY_S` (120) after boot: for each symbol in `GAPFILL_SYMBOLS` (NIFTY,SENSEX), list
  the trading days in the last `GAPFILL_LOOKBACK_DAYS` (30) whose `oi_day_stats.usable_minutes`
  is below `GAPFILL_MIN_MINUTES` (300). Weekends and `data/nse_holidays.json` dates are never
  gaps. Today is excluded (the live feed fills it).
- If any gap exists, run `scripts/truedata_backfill.py pull --symbol X --include-live-days` as a
  subprocess (the image now ships `scripts/` at `/app/scripts`). This is the same puller, the same
  `oi_backfill_progress` checkpoints and the same no-double-count rules the nightly VPS cron
  uses, so the two never fight.
- Then `refresh_data_health()` (oi_day_stats → live_days) so the unified view serves the new days
  at once, and a Telegram note with the before/after gap counts.
- Re-check every `GAPFILL_RECHECK_H` (24 h). Single-flight; a run already in progress is refused.
- Skips cleanly when TrueData REST credentials or the script are absent.

Why REST for this: the archive endpoints are not subject to the single-login limit, so a relay
follower can heal its own history without touching the owner.

## 4. Known limits / next steps

- Followers cannot request symbols; the owner's ATM window is what everyone sees.
- The relay carries option ticks only (index spot rides in `und`), exactly like the local queue.
- Gap fill covers `oi_archive_bars`; `pull-ticks` (last 5 days of tick data) is left to the
  nightly cron because the vendor purges it and it is not needed by the engines.

## Addendum 2026-09-02 — official closes

The gap-fill and `scripts/nightly_topup_vps.sh` now also run
`truedata_backfill.py pull-eod --symbol NIFTY --days N --years 1` after the
chain pull. The NSE F&O bhavcopy it stores (`eod_bars`, source `td_bhavcopy`,
real expiry per contract since today; legacy sentinel rows are repaired on every
run) supplies the UMP engine's daily/weekly closes — TradingView's daily bars
close at the exchange's official close, not the last trade. The puller skips
the bhavcopy pass for non-NSE symbols; SENSEX falls back to the last-30-minute
mean.

## Addendum 2026-09-09 — same-day session catch-up, scheduler, integrity

**Why.** The boot gap-fill had never worked inside the Docker image: the puller
resolved its package path to `/app/backend` (absent; the package is `/app/app`),
every subprocess died with `ModuleNotFoundError: No module named 'app'`, and the
task still logged `gapfill.done status=ok`. Independently, nothing restored the
current session — a boot at 11:28 left 09:15→11:28 missing forever (`missing_days`
capped at yesterday; recheck every 24 h).

**What runs now** (`backend/app/ingest/gapfill.py`):

1. Boot: `sleep(GAPFILL_DELAY_S)` → refresh `oi_day_stats`/`live_days` FIRST →
   **session catch-up** if the session is open → historical scan.
2. **Session catch-up** (`session_catchup`): the session minutes 09:15→now−2 min
   with no live CE/PE row are listed; if ≥ `GAPFILL_CATCHUP_MIN_MISSING` (2) the
   puller runs `pull --symbol S --day YYYY-MM-DD --max-expiries 2` (nearest two
   expiries + index/futures, from 09:15 to the last COMPLETED minute; ledger units
   carry a `:dYYMMDD` suffix and stay `partial` until the close is fetched), then
   the restored minutes are **promoted** into `option_oi_snapshots` with `src = 1`
   wherever the live feed has no row for that token in that minute
   (`ON CONFLICT DO NOTHING`; `oi > 0`). A live minute always wins. Every reader
   of the live table sees the whole session with no query change; the premium
   path (`series._PREMIUM_MINUTES_TEMPLATE`) additionally reads the archive's true
   O/H/L/C for those minutes instead of the promoted o=h=l=c row.
   Triggers: boot, every `GAPFILL_CATCHUP_CHECK_S` (300 s) while the session is
   open, and immediately when the TrueData steward reports a recovery
   (`request_catchup("feed_recovered")`).
3. **Historical pull** (months-long): deferred to `close + GAPFILL_POST_CLOSE_DELAY_MIN`
   on a trading day unless the lookback has NO usable day (fresh install); runs
   once after the close, and on the `GAPFILL_RECHECK_H` fallback. A non-zero
   puller exit code marks the report `error` and pages once; the last 20 output
   lines are kept in the report.
4. Ledger fixes: the full-pull window start is month-aligned (stable
   `{SYM}:IDX:{yymmdd}` keys — the old `now−186d` start re-fetched six months of
   index/futures every run); `eod:td:*` and `status='error'` units are re-opened
   on every run; a failed unit is never marked `done` (`_fetch_unit` returns
   `None` on error and the script exits 2 when any unit errored).
5. After the post-close run: a per-day **integrity report**
   (`services/data_integrity.py`, 10 categories) for today + the previous session,
   persisted to `data_integrity_runs` and `logs/integrity/`.

**Surfaces.** `GET /api/health/feed` → `gapfill.{last_report,last_catchup}` and
`aggregator.late_dropped`; `GET /api/health/data` → coverage + gap-fill + catch-up +
integrity summary; `GET /api/health/data/integrity?symbol=&day=` → the full report;
CLI `truedata_backfill.py integrity --symbol S --days N` (also in the nightly script).

**Related invariants.** The aggregator now drops a tick whose bucket is older than
the last bucket it flushed for that token (`late_dropped`), so a delayed poller
row can no longer overwrite a persisted socket value. The hold-last refresher
follows the date's own close (15:40 since 2026-08-03) and stops on NSE holidays.
