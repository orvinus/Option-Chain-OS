# Project Context Map (generated 2026-09-02)

Consolidated technical briefing of the whole repo from a full source read. Ground truth for implementing changes; verify `file:line` refs against the tree if code has moved.

## Runtime snapshot (2026-09-02)

- Local stack = compose project `docker` (`docker/docker-compose.yml`): frontend :80, backend 127.0.0.1:8000, TimescaleDB 127.0.0.1:5434. Images built 2026-08-21 match the newest source edits.
- Branch `ayush-bhai-v2`; 30 modified + many untracked files (algo subsystem, migrations 0006–0013) uncommitted. Prod VPS runs `ayush-bhai-branch` at `/root/nifty-oi`, https://oialgo.tech (root@187.127.206.41, Caddy owns 80/443, frontend remapped to 127.0.0.1:8080).
- Local `.env`: `FEED_VENDOR=truedata`, `RUN_MODE=live` (the local `docker-compose.override.yml` pins replay but the running container was started without it), `MAIN_USER`/`ALGO_ADMIN_USER` set, `PERSIST_BUCKET=1s`, `STRIKE_WINDOW=11`, `DB_BIND=127.0.0.1:5434`, bridge `172.31.0.0/16`, `TRUEDATA_WS_PORT=8084`, `TRUEDATA_PROXY` empty.
- DB: `option_oi_snapshots` ≈6.95 M rows, 2026-05-27 → 2026-08-25; alembic head `0013_data_health`.
- Top-level: `backend/` (FastAPI, 172 py files), `frontend/` (React/Vite, 79 files), `docker/`, `scripts/`, `docs/`, `data/` (symbols.json, nse_holidays.json), `logs/`, `packaging/`, `TD_API_Documents/` (vendor docs + live creds, never commit).

---

# PART 1 — Backend data-ingest + runtime (`backend/app/`)

## Shape

```
main.py lifespan ──► feed_factory.ensure_live_ingestion()
                        ├─ MinuteAggregator(rt.tick_queue, on_flush=_make_on_flush())
                        ├─ start_feed() → build_feed_client() ──FEED_VENDOR──┐
                        │        xts → ingest/ws_client.OptionFeedClient      │
                        │        truedata → ingest/truedata_feed.TrueDataFeedClient
                        ├─ spawn_supervised(_spot_refresher, "spot-refresher")
                        └─ (optional) start_shadow_feed() → td_shadow_snapshots
                     ► steward (XTS SessionSteward | TrueDataSteward)
                     ► poller (UniversePoller | TdFailoverPoller | TdSegmentSweeper)
                     ► watches: nse-session-watch, atm-drift-watch, proxy-watch
```

Every producer writes `Tick` objects into `runtime.Runtime.tick_queue` (maxsize 100 000, `runtime.py:18`). The aggregator is the sole consumer and the sole writer of `option_oi_snapshots`.

## Boot sequence (`main.py:51-243`)

1. `configure_logging("INFO")` (`core/logging.py:17`); httpx/httpcore/websockets silenced to WARNING because httpx logged TrueData URLs containing the password.
2. TrueData defensive logout (`main.py:70-86`) when live+truedata: probe proxy, `td_sess.logout_request("boot", force=True)` (25 s cap). Boot assumes the last exit was dirty; skipping guarantees a ~60 s "User Already Connected" lockout.
3. XTS restore/login (`main.py:92-131`) only when `feed_vendor=="xts"`: `try_restore_session_from_db()` (30 s) else `login(force=True, actor="startup")`. Non-fatal; steward takes over.
4. `ensure_live_ingestion(fresh_token_minted=False)` — the ONE construction path (also used by `api/auth.py:40` and steward rebuild).
5. `spawn_supervised` for `nse-session-watch`, `atm-drift-watch`.
6. Steward, hard vendor branch (`main.py:151-163`): TrueData → `get_td_steward()` + `run_proxy_watch`; XTS → `SessionSteward()` + `set_steward()`. Both on `rt.steward`.
7. Poller (`main.py:170-181`) if `effective_poller_mode != "off"`: TrueData → `TdSegmentSweeper` or `TdFailoverPoller`; XTS → `UniversePoller`.
8. `iv-history-snapshot` (60 s warmup), `greeks-history-snapshot` (75 s), `algo-orchestrator` (live only).
9. `algo-stream`, `data-health` run in replay too.

Shutdown order (`main.py:217-242`): cancel tasks → poller.stop → feed.stop (TrueData sends on-socket logout; compose `stop_grace_period: 45s`) → shadow.stop → aggregator.stop → sess.stop.

`core/tasks.py:32` `spawn_supervised(factory, name, *, respawn=True)`: strong refs, Telegram alert on death, respawn after 5 s; factory is a zero-arg callable.

## `Tick` (`ingest/types.py:8-44`)

```python
@dataclass(slots=True)
class Tick:
    ts: datetime          # tz-aware UTC, ARRIVAL time (bucketing key)
    token: str            # XTS: bare ExchangeInstrumentID; TrueData: "td:NIFTY:260828:24500:CE"
    symbol: str; expiry: date; strike: int
    option_type: str      # 'CE'|'PE'|'IDX'|'FUT'
    ltp: float | None     # None = unknown; NEVER 0
    oi: int; volume: int
    underlying: float | None = None   # spot, set by the PRODUCER
    origin: str = "ws"    # "ws" | "poller"
    vendor: str = "xts"   # never persisted
    vendor_ts: datetime | None = None # shadow store only
```

Invariant: `origin` is compared exactly in the aggregator; `ws_rows` is the sole input to `rt.last_ws_flush_at` and thus the whole recovery ladder. Never introduce vendor-tagged origins like `"td_ws"`.

## Aggregator (`ingest/aggregator.py`)

- `MinuteAggregator(in_queue, on_flush=None, table=LIVE_TABLE)`; table allow-listed to `option_oi_snapshots` / `td_shadow_snapshots` (:60-79).
- Bucket from `PERSIST_BUCKET` (`1s|5s|1min`). Key `(token, bucket_start)` → latest Tick. `_absorb` (:159): IDX/FUT dropped (CHAR(2) column); ws-beats-poller precedence in a bucket (:170-172); `MAX_OPEN_BUCKETS=60_000`.
- `_run` (:119) flushes when wall clock crosses a bucket boundary. Graceful `stop()` drains and force-flushes.
- `_flush_closed` (:189): batched `INSERT … ON CONFLICT (ts, token) DO UPDATE` with `ltp=COALESCE(EXCLUDED.ltp,…)`, `volume=GREATEST`, `underlying=COALESCE`. On error buckets are retained and retried.
- Hook `OnFlushHook(cutoff, rows, ws_rows)`.

## Feed factory (`ingest/feed_factory.py`)

- `build_feed_client()` (:193) is the vendor seam. Rollback = flip `FEED_VENDOR` + restart.
- `_resubscribe_provider`/`_td_resubscribe_provider` → `(tokens, spot)`; never publish an empty universe (:110-119). `_initial_spot` → XTS quote → `db_last_underlying` → 24000.0 only for NIFTY.
- `resolve_index_ref()` (:126) resolves token AND segment together.
- `_make_on_flush()` (:323): sets `rt.last_flush_at`; `rt.last_ws_flush_at` only when `ws_rows>0`; hub publishing decoupled into a coalescing worker task.
- `start_shadow_feed()` (:219): separate queue/aggregator/table, touches no freshness fields.
- `rebuild_feed_client(reason)` (:278), `_spot_refresher()` (:302) reads `rt.feed_client` dynamically each 1 s.
- `ensure_live_ingestion(fresh_token_minted)` (:370) idempotent; `True` triggers `nudge_reconnect()`.

## XTS feed (`ingest/ws_client.py`)

Socket.IO; LTP/volume on 1501, OI on 1510, merged per instrument (`_handle_touchline` :767, `_handle_oi` :829, `_emit` :851). Subscription via REST POST `/instruments/subscription`. `nudge_reconnect()` (:207); `_connect_once` bounded 25 s; `_subscribe_all` 60 s budget; `_classify_400` (:913) distinguishes "already subscribed" from the 50/50 cap breach (`_has_cap_breach` :950); re-arm rung (:608-629) asks the steward for a fresh login (never logs in itself, `_request_auth_recovery` :698). `_last_oi` survives reconnects; `_last_ltp/_last_volume` day-scoped. 30 s tick silence clears `_connected`.

## TrueData feed (`ingest/truedata_feed.py`)

One frame carries LTP+volume+OI. Trade-frame indices `_T_*` (:104-118) stable for 15- and 19-field layouts; touchline fields ≥11 not trusted for OI. Credentials go RAW in the WS query (:320-349) — `%40` reads as wrong password. `proxy` passed explicitly (:350-353). Login verdict keys on `login["success"] is False` (:381). Drain-only `_reader` → bounded `_reader_q` (50 000) → `_parser`. Any decoded frame refreshes heartbeat (:510). `_hold_last` (:729) re-emits untraded contracts each ~0.9×bucket, gated on heartbeat AND `_in_session_window()`. `_teardown` (:421) resets budget ACKs, `SymbolIdMap`, `_last_seq`. `swap_subscription` (:801) is a set diff; `removesymbol` needs the vendor string. Counters on `/api/health/feed`.

## Budget & identity

`ingest/subscription_budget.py`: `Priority` REFERENCE(0, never evicted)/ACTIVE/ELASTIC; `set_capacity(maxsymbols)` minus `TRUEDATA_BUDGET_SAFETY_MARGIN`; `maxsymbols<=0` = UNKNOWN → floor 50 + error log; symmetric truncation by moneyness rank.
`market/td_identity.py`: `option_token()` → `td:NIFTY:260828:24500:CE` must stay byte-identical to `scripts/truedata_backfill.py` or `oi_snapshots_unified` splits every series. Vendor strings echoed, never constructed. `SymbolIdMap` rebuilt per reconnect.

## Stewards

**XTS `session_steward.py`**: premise = a login kills the previous session. 30 s loop; watch window weekday open−45 min → close; health = `rt.last_ws_flush_at` age ≤180 s (not `feed_connected`, not token count). Ladder (`_escalate` :356, min 90 s between rungs): 0 kick → 1 rotate → 2 rebuild universe → 3 rebuild client → wraps to 1. Rotation verified by a newer WS flush (in session) or `is_feed_connected` (off session). Daily pre-open rotation at `DAILY_TOKEN_REFRESH_IST` 08:35 + 22 h TTL backstop; NO interval refresh loop. Second-client detector parks rotations. `_should_exit` (:519): >600 s unhealthy AND rebuild attempted AND circuit closed AND in-window → `os._exit(1)`. Exposes `ws_feed_healthy` / `ws_feed_stable` (≥ `POLLER_FAILOVER_LINGER_S`).

**TrueData `truedata_steward.py`**: premise inverted, danger is logging OUT (~75 s cooldown). 15 s loop, `STALE_AFTER_S=150`, `SUICIDE_AFTER_S=900`; heartbeat age >25 s ⇒ unhealthy; parked while in COOLDOWN or proxy down. Ladder: 0 nudge → 1 wedge recovery (`logout_request`, only on wedge signature) → 2 rebuild client → wraps to 1. `_do_exit` logs out first then exits.
Gotcha: `TrueDataSteward` is never registered via `set_steward()`, so `get_steward()` is `None` under TrueData; `UniversePoller._ws_feed_delivering()` fails safe, `TdFailoverPoller` reads `rt.steward`.

## Sessions

`auth/market_session.py` (XTS): only place `/auth/login` happens. `login(force, manual, actor)` (:203) enforces hard floor `LOGIN_FLOOR_S` (force does not bypass; manual does; seeded from `MAX(auth_sessions.issued_at)`), burst budget → `LoginRateLimited`, storm breaker (4 rotations w/o healthy), circuit → `LoginCircuitOpen` except one probe per 300 s. Boot-storm heuristic: ≥4 rows in 30 min opens circuit. `authenticated` = TTL-valid AND not broker-rejected; `has_usable_token` is the poller gate. `try_restore_session_from_db` validates with the broker. `stop()` does NOT log out.

`auth/td_session.py`: `disconnected → connecting → live → wedged → cooldown → disconnected`; `LOGOUT_COOLDOWN_S=75`, `LOGOUT_MIN_INTERVAL_S=90`, `REJECTIONS_TO_WEDGED=2`; `connect_guard()` single-flight; `_absorb_entitlements` on every connect; `snapshot()` is the health payload.

## Symbol switching / ATM drift / session watch

`symbol_controller.switch_active_symbol(symbol)` (:194) under `_switch_lock`: resolve spot token (MCX near-month future; indices exact `_INDEX_XTS_NAME` match; stocks search) → spot → universe (vendor-branched :237-245) → `feed.swap_subscription`. Spot precedence: live quote → `db_last_underlying` → `rt.latest_spot` only if same symbol → `1.0` placeholder; otherwise `rt.latest_spot=None`.
`atm_drift_watch.py`: every 60 s resubscribe when |spot − last_sub_spot| ≥ window × registry step × 0.4, or when the configured window changed (Algo Daily Config edit).
`market_session_watch.py`: on session open transition → `nudge_reconnect()`.

## Pollers

`universe_poller.py` (XTS): never `login()`; 401/"invalid token" → `TokenStale` aborts sweep; always sets `underlying`; skips `oi<=0`; skips active symbol while WS delivering; paced chunks; LTP carry ≤5 min else `ltp=None` but row still written.
`td_failover_poller.py`: `TdFailoverPoller` polls active symbol only while `not ws_feed_stable`, via `getlastnbars` (~46 req ≈12 s at 4 rps), rows stamped with bar time; `TdSegmentSweeper` uses `getAllBars` per minute per segment (paid add-on), longest-prefix underlying match. Both emit `origin="poller"`.

## Universe resolution

`market/scripmaster.py` (XTS): NSEFO+BSEFO+MCXFO master parsed by column position; MCX lot from `Multiplier` col 13; cache needs 20 h TTL AND same IST date; zero rows raises. `resolve_option_universe(spot, symbol, today, force_refresh, window, policies)` → `(tokens, expiries)`; policies `current_weekly|next_weekly|current_monthly|next_monthly`; step = registry → modal gap → `settings.strike_step`; window = atm ± window×step.
`market/scripmaster_td.py`: imports selection helpers from `scripmaster`; discovery via `getSymbolExpiryList` + `getSymbolOptionChain`; sticky-expiry guard (:173-182); `_configured_data_window()` (:252) reads today's `data_strike_window` from Algo Config, fallback `STRIKE_WINDOW`; disk cache stamps UTC-naive.
`market/symbols.py`: `SymbolEntry(symbol, display, kind, sector, exchange, spot_token, fno_eligible, lot_size, strike_step, poll_tier)` from `data/symbols.json`.

## Market-data clients

`xts_client.py`: 1501/1510, segments NSECM=1 NSEFO=2 NSECD=3 BSECM=11 BSEFO=12 MCXFO=51; subscribe POST, unsubscribe PUT; `validate_token` → True/False/None.
`truedata_rest.py`: CSV by header name with aliases; error strings in 200 bodies pre-screened; bearer refresh 300 s early; `_RateGovernor`; `get_bars` splits at the ~3 800-row cap; `logout_request` raw creds; `_OPT_TAIL_RE` consumes 6-digit expiry.
`yahoo_hv.py`: HV10/20/30, 6 h caches, `YAHOO_HV_ENABLED`.

## WS fan-out

`ws/hub.py`: `Subscriber(ws, timeframe, expiry, symbol, queue(8))`; `publish_flush` skips other-symbol subscribers, drops oldest on full.
`ws/oi_stream.py`: `/ws/oi-stream?timeframe&expiry&symbol`; accept before close; 1008 bad params, 1013 not_fno/warming_up; immediate snapshot, 15 s ping; `set:tf=…,exp=…,sym=…`.

## Health

`GET /api/health` always 200: `status, authenticated, latest_spot, tokens_subscribed, last_flush_at, expiries, run_mode, now_ist, nse_session_open, session_open_ist, session_close_ist, feed_connected, active_symbol, poller_enabled, poller_last_sweep_at, poller_last_ticks` + `last_ws_flush_at, ws_last_tick_at, live_subscriptions, supervisor_alive, watchdog_last_check_at, last_login_at, logins_last_hour, circuit_state, db_ok, poller_mode`. `authenticated` is the XTS session → permanently false under TrueData.
`GET /api/health/strict`: 503 reasons `db_down|recovery_circuit_open|session_wedged|proxy_down|stale_data`; `restart_recommended = db_ok and not circuit_open and not wedged and not proxy_down`; holiday → 200 `note:"nse_holiday"`; fresh REST data → 200 `degraded:"rest_failover"`. Body shape frozen (sentinel greps it).
`GET /api/health/feed`: vendor internals (budget, heartbeat, seq gaps, drops, td_session snapshot, shadow, proxy, steward).

## Settings (`core/config.py`), defaults

XTS: `XTS_MD_BASE_URL`, `XTS_MD_APP_KEY`, `XTS_MD_SECRET_KEY`, `XTS_MD_SOURCE=WebAPI`, `XTS_MD_BROADCAST_MODE=Full`, `XTS_MD_PUBLISH_FORMAT=JSON`.
TrueData: `TRUEDATA_USER/PASSWORD`, `TRUEDATA_OI_SCALE_NSE/BSE/MCX=1`, `TRUEDATA_RATE_LIMIT_RPS=4`, `TRUEDATA_PROXY=""`, `TRUEDATA_WS_HOST=push.truedata.in`, `TRUEDATA_WS_PORT=8086` (prod 8084), `TRUEDATA_MAXSYMBOLS_OVERRIDE=0`, `TRUEDATA_BUDGET_SAFETY_MARGIN=2`, `TRUEDATA_SUBSCRIBE_BATCH=100`, `TRUEDATA_HOLD_LAST_ENABLED=True`.
Vendor: `FEED_VENDOR=xts|truedata` (default xts), `TRUEDATA_SHADOW_ENABLED=False`.
DB: `DB_URL`, `DB_URL_SYNC`.
Scope: `UNDERLYING_SYMBOL=NIFTY`, `NIFTY_INDEX_TOKEN=26000`, `STRIKE_WINDOW=11`, `STRIKE_STEP=50`, `EXPIRIES=current_weekly`, `NIFTY_LOT_SIZE=75` (.env says 65).
Cadence: `PERSIST_BUCKET=1min` (local 1s).
Poller: `POLLER_MODE=off|failover|full|segment_sweep` (failover), `POLLER_FAILOVER_INTERVAL_S=15`, `POLLER_FAILOVER_LINGER_S=120`, `POLLER_ENABLED=False`, `POLLER_STRIKE_WINDOW=7`, `POLLER_EXPIRIES`, `POLLER_QUOTE_CHUNK=25`, `POLLER_MAX_CONCURRENCY=6`, `POLLER_PACE_MS=50`, `POLLER_MAX_429_RETRIES=5`, `POLLER_SKIP_ACTIVE_SYMBOL=True`, `POLLER_TIER_FAST/MID/SLOW_S=20/60/180`.
API: `API_HOST`, `API_PORT`, `API_CORS_ORIGINS`. Session: `MARKET_OPEN_IST=09:15`, `MARKET_CLOSE_IST=15:40`.
Behavior: `RUN_MODE=live|replay`, `DEBUG_TICKS`, `XTS_LOGIN_TIMEOUT_S=45`, `DB_PERSIST_TIMEOUT_S=20`.
Gates: `MAIN_USER/PASSWORD`, `ALGO_ADMIN_USER/PASSWORD` (""→algo 503), `ALGO_SESSION_SECRET` (""→ephemeral), `ALGO_SESSION_TTL_H=12`.
Orders: `LAKSHMISHREE_INTERACTIVE_URL/_APP_KEY/_SECRET_KEY/_SOURCE=WEBAPI`.
Algo stream: `ALGO_STREAM_ENABLED=True`, `ALGO_STREAM_TICK_MS=1000`, `ALGO_STREAM_OI_REFRESH_S=30`, `ALGO_STREAM_SLOW_REFRESH_S=20`, `ALGO_STREAM_MAX_SUBS=8`.
Alerting: `TELEGRAM_BOT_TOKEN/CHAT_ID` (""→no-op), `ALERT_MIN_INTERVAL_S=300`, `LOG_FORMAT=pretty|json`.
Rotation: `LOGIN_FLOOR_S=90`, `LOGIN_BURST_MAX=5`, `LOGIN_BURST_WINDOW_S=600`, `DAILY_TOKEN_REFRESH_IST=08:35`, `STRICT_STALE_AFTER_S=300`.
IV: `IV_SCANNER_MAX_SYMBOLS=50`, `YAHOO_HV_ENABLED=True`, `IV_HISTORY_SNAPSHOT_INTERVAL_S=900`.

## Support

`core/db.py`: async engine pool 10+20 `pool_pre_ping`, `AsyncSessionLocal(expire_on_commit=False)`, `session_scope()`, sync engine 5+10, `get_session()`.
`core/time_utils.py`: `IST`, `MARKET_OPEN/CLOSE`, **`ist_naive_to_utc` always** (pytz `.replace(tzinfo=IST)` = +05:53 LMT, 23-min shift), `session_floor_for(ts)` (rolls back a day between 00:00 and 09:15), `TIMEFRAME_TO_DELTA`, `parse_timeframe`.
`core/holidays.py`: mtime-cached `data/nse_holidays.json`. `core/notify.py`: `notify(key, msg)` never raises, rate-limited per key. `core/proxy_health.py`: TCP connect to SOCKS port, 5 s cache.

---

# PART 2 — Analytics API + services + DB schema

## Router wiring

`api/__init__.py:27` `api_router = APIRouter(prefix="/api")` with 21 sub-routers; mounted in `main.py:336` with `ws_router` and `algo_ws_router`; SPA static mount at `:342`.

## REST routes (non-algo)

| Method | Path | Params | Response |
|---|---|---|---|
| POST | `/api/auth/login` | `{force_new_token}` | `LoginResponse{status,message,authenticated}` |
| POST | `/api/auth/main-login` | `{username,password}` | `LoginResponse` |
| GET | `/api/health` | — | HealthResponse |
| GET | `/api/health/strict` | — | `{status, reasons[], restart_recommended?, degraded?, note?}` 200/503 |
| GET | `/api/health/feed` | — | vendor internals |
| GET | `/api/spot` | `symbol?` | `{symbol, spot, asof}` |
| GET | `/api/expiries` | `symbol?` | `{expiries[]}` |
| GET | `/api/oi-change` | `timeframe=5m, expiry?, symbol?, as_of?, from_ts?, to_ts?` | `{timeframe,expiry,spot,asof,computed_at,total_call_oi_change,total_put_oi_change,rows[]}` |
| GET | `/api/oi-timeseries` | `symbol?, expiry?, strike_min*, strike_max*, bucket=1m, from_ts?, to_ts?` | `{points[{ts,total_call_oi,total_put_oi,ratio,pcr}]}` |
| GET | `/api/ratio-timeseries` | `symbol?, expiry?, bucket, strike_min?, strike_max?, from_ts?, to_ts?` | `{points[{ts,ratio,pcr,…}]}` |
| GET | `/api/multi-timeframe` | `symbol?, expiry?, timeframes?, as_of?, atm_window?` | `{spot,atm_strike,totals,ratio,pcr,rows[{timeframe,call_oi_change,put_oi_change,oi_change_ratio}]}` |
| GET | `/api/history-dates` | `symbol?, expiry?` | `{dates[]}` newest first |
| GET | `/api/option-chain` | `expiry?, symbol?` | `{expiry,spot,asof,rows[]}` |
| GET | `/api/option-chain-full` | `timeframe, expiry?, symbol?` | + `lot_size, synthetic_future, atm_iv, ivp`, 28-field rows w/ greeks |
| GET | `/api/iv-scanner` | `symbols*, expiry=near|next|far, mode, timeframe` | scanner rows |
| GET | `/api/replay` | `start*, end*, step=5m, expiry?, symbol?, summary, with_greeks` | `{frames[{ts,spot,atm,totals,ratio,pcr,rows[]}]}` ≤5000 frames |
| GET | `/api/interpretation` | `timeframe, expiry?, symbol?` | `{rows[{strike,call,put}]}` |
| GET | `/api/symbols` | — | `{active_symbol, groups[]}` |
| POST | `/api/active-symbol` | `{symbol}` | `{symbol,display,fno_eligible,spot,expiries}` |
| GET | `/api/verify/ping`, `/api/verify/nifty-cross-check`, `/api/verify/option-chain` | | cross-checks vs NSE/Yahoo |
| WS | `/ws/oi-stream` | `timeframe, expiry?, symbol?` | `{type:"oi_change"|"option_chain_full", data}` |

Helpers: `_symbol_utils.resolve_symbol` (404 unknown) / `resolve_fno_symbol` (409 `not_fno_eligible`); `_expiry_utils.resolve_expiry` — explicit → runtime head → DB fallback using per-table aggregates, never bare `MIN(expiry)`, never the unified view (2026-08-11 pool exhaustion). `/api/expiries` = runtime ∪ live ∪ archive, upcoming-asc then past-desc; self-heals via `switch_active_symbol` if empty.

## OI-change engine (`services/oi_change.py`)

- Timeframe path `_compute` (:513): `anchor = as_of or MAX(ts) or now`; `full_day` baseline = first snapshot at/after `session_floor_for(anchor)`; else `cutoff = anchor − delta` with baseline floored to session open (:538-543 explains the "1 Min shows −2Cr" bug).
- Per-strike baseline clamp (:567-581): strikes without a floored `then` use their earliest-today row.
- Range path `_compute_range` (:402): re-anchors windows after the last stored session to `market_open_today`; open-ended past-day windows clamp to close; bounded baseline; no-baseline → zero change + warning.
- Sub-minute hold-last-change (`SUBMINUTE_TIMEFRAMES={1s,15s,30s,45s}`, horizon 5 min, `_merge_hold_last` :767), session-floored both arms; live only.
- TTL cache 30 s keyed `(timeframe, expiry, symbol, ref_key)` / `("range",…)` / `("multi",…)`; live spot grafted onto cache hits.
- `get_multi` (:594): shared now snapshot + N then queries; spot from freshest row (`max(key=ts)`); `atm_window` filters ± N×step; `ratio=CE/PE`, `pcr=PE/CE`, None on zero.

## option_chain_full + IV/greeks

`_years_to_expiry` (:39) → T to `IST(expiry, MARKET_CLOSE)`; None after settlement → null IV/greeks. `MIN_T_YEARS` = 60 s. `r` = 0.065 equity/index, 0.0 commodity. `iv_calculator.calc_iv`: bisection σ∈[1e-6,5]; rejects below intrinsic. `greeks.calc_greeks`: θ per calendar day, vega per 1 pp. Trend codes LB/SC/SB/LU. Row filter: keep strikes with change or volume, else ATM ±20 with two-sided OI ≥ lot. `interpretation.py`: long/short buildup etc, `OI_EPSILON=1`, `LTP_EPSILON=0.05`.

## Replay (`services/replay.py`)

Single `time_bucket_gapfill + locf` over `oi_snapshots_unified`; spot from freshest leg; frame `ts = bucket + step` (label by end); baseline = each strike's first tick at/after `session_floor_for(start)`; `atm` via `_atm_strike` fallback `_min_straddle_atm`; `with_greeks` joins `greeks_snapshots`.

## Timeseries

`oi_timeseries.py`: locf per (bucket,strike,type) then SUM; `first_seen` CTE keeps the strike set constant; window end = last real tick +1 µs; `_expiry_lifetime_window_utc` bounds scans to `[expiry−120d, expiry+1d]`. `ratio_timeseries` = projection of same.

## Other services

`data_health.py`: `refresh_data_health()` refreshes `oi_day_stats` then `live_days` on an AUTOCOMMIT connection (CONCURRENTLY); `coverage()` reads `oi_day_stats`; loop two slots/day (≥06:00, ≥16:00). `iv_history.py`: `iv_daily` upsert, IVR/IVP over 252 d, min 20; refuses to write unless chain `asof` is today. `greeks_history.py`: same guard, upsert. `iv_scanner.py`: TTL 45 s, gather per symbol, max 50. `spot_fallback.db_last_underlying` only for strike-window centring. `nse_option_chain.py`: NSE v3, OI in contracts (×lot to compare). `nifty_public_quote.py`: NSE allIndices → Yahoo ^NSEI, 30-pt tolerance.

## Migrations

- 0001: `option_oi_snapshots(ts, symbol, expiry, strike, option_type CHAR(2), token, oi, ltp NUMERIC(14,2), volume, underlying, PK(ts,token))` hypertable 1-day chunks; `ix_oi_expiry_strike_ts (expiry,strike,option_type,ts DESC)`, `ix_oi_symbol_ts`, `ix_oi_token_ts`; cagg `oi_1h`; `auth_sessions`, `websocket_logs`, `api_logs`.
- 0002: compression (segmentby symbol) 7 d, retention 180 d.
- 0003: `iv_daily(symbol, trade_date, atm_iv, spot, expiry, created_at)`.
- 0004: `greeks_snapshots(ts,symbol,expiry,strike,option_type,iv,delta,gamma,theta,vega)` hypertable; drops `oi_1h`.
- 0005: `oi_archive_bars` (TrueData 1-min, permanent), `oi_archive_ticks` (last 5 trading days), `eod_bars`, `fii_dii_flows`, `news_items`, `oi_backfill_progress`; view `oi_snapshots_unified` = live ∪ archive (CE/PE only).
- 0006: `td_shadow_snapshots` (+`vendor_ts`, `recv_lag_ms`), `contract_key()` fn, matview `live_days` + unique index, unified view where live wins whole days.
- 0007: `oi_archive_ticks.source` default `td_getticks`.
- 0008: algo foundation (`algo_config_versions`, `algo_users`, `algo_audit`, `algo_trades`…). 0009: `algo_day_notes`. 0010: `algo_backtest_runs/trades/days/signals`. 0011: `algo_backtest_sandbox`, `algo_broker_snapshot`. 0012: `algo_trades.broker_order_id` (entry_order_id). 0013: `oi_day_stats` matview (`winner='live'` iff `live_minutes >= LEAST(arch_minutes,300)`), `live_days` derived, `data_health_refresh`.

## Validation harness (`backend/validation/`)

Read-only, never calls login. `python -m validation.<mod>` from `backend/`; `run_all.py`. Layers: A broker (XTS REST vs DB vs API), B internal (independent SQL recompute), C frontend (Python ports of KPIBar/oiStrikeWindow/oiCandles), D Sensibull, E NSE (`/api/verify/*`), F `oi_delta` (change vs Sensibull deltas), FX cross-vendor, `oi_units` (0.98–1.02 band), `m6_*`, `backtest_golden`, `backtest_parity`. Output `logs/validation/<date>/`.

## Cross-cutting invariants

1. Never bare `MIN(expiry)`; never aggregate over `oi_snapshots_unified` on a polled path.
2. Spot from the freshest row, never the first in strike order.
3. Every baseline session-floored (`session_floor_for`).
4. Never report `now − 0`; clamp to first-seen, else zero.
5. T from `MARKET_CLOSE`, never a literal; settled → null IV/greeks.
6. `ist_naive_to_utc`, never `.replace(tzinfo=IST)`.
7. Broker login hard-gated off in `RUN_MODE=replay`.
8. Frame labels are bucket ends; gapfill finish bounds exclusive (+1 µs).
9. `ltp NULL ≠ 0`.

---

# PART 3 — Algo trading subsystem (`backend/app/algo/`)

## Topology

Filter layer (3 pure engines over OI series) decides direction; execution layer (Ultra Master Pro, Pine v24 port over option-premium bars) decides entry/exit. `ZoneOrchestrator` (`orchestrator.py:219`) is the only meeting point; all effects via `OrchestratorDeps` (`:84`), which is what makes backtest a faithful replay. Tasks spawned `main.py:195` (`algo-orchestrator`, live only) and `:204` (`algo-stream`, always).

## Config models (`config_models.py`)

One JSONB row per version, live = MAX(version). Root `AlgoConfig` (:407): `schema_rev=1`, `global_` (alias `"global"`), `days: dict[Weekday, DayConfig]`; `zone(day, id)`, `last_zone_id(day)` = max End time.
- `GlobalConfig` (:384): `master_kill=False`, `overnight_carry=True`, `demat_balance=30000`, `paper`, `fees`, `holidays`, `config_lock`.
- `PaperConfig` (:354): `paper_mode=True`, `virtual_balance=30000`, `session_started`, `slippage_pct=0.5`, `latency_ms=250`, `fill_source=ltp|bid_ask_mid`, `shadow_mode=False`.
- `FeeConfig` (:341): `brokerage_per_order=17`, `stt_sell_premium_pct=0.1`, `exchange_txn_pct=0.03503`, `sebi_turnover_pct=0.0001`, `ipft_pct=0.0005`, `gst_pct=18`, `stamp_duty_buy_pct=0.003`.
- `ConfigLock` (:371): `lock_market_hours=False`, `require_save_confirm=True`. `HolidayEntry(date, occasion)`.
- `DayConfig` (:319): `index_symbol=NIFTY`, `day_kill`, `data_strike_window=11 (1..60)`, `all_in=False`, `allocation_pct=50`, `max_loss_pct=20`, `max_profit_lock_pct=30`, `max_consec_losses=3`, `max_consec_loss_pct=25`, `end_exit_enabled=True`, `alerts` (`DayAlertToggles`: only telegram wired), `zones`.
- `ZoneConfig` (:272): `start=09:20`, `end=10:30`, `premium_min=75`, `premium_max=125`, `strike_scan_count=1 (1..10; seed ships 3)`, `max_trades=2`, `zone_kill`, `strategy_active=True`, `reeval_cadence=every_candle|zone_start`, `direction_hold_min=0`, `enabled_indicators=[oi_change,multi_tf,ratio]`, + `oi_structure`, `mtf_ratio`, `mqae`, `ump`.
- `OIStructureParams` (:85): `left_bars=3`, `right_bars=3`, `min_swing_move_cr=0.03`, `eq_tolerance_cr=0.015`, `breakout_buffer_cr=0.02`, weights swing 15/breakout 30/sweep 10/red_candle 20/agreement 10, `confidence_threshold=55`, `engine_1m_on`, `engine_5m_on`, `strikes_atm_window=10`, `matrix` (6 events × Call/Put → Call|Put|Ignore).
- `MtfRatioParams` (:158): `timeframes=[1m…full_day]`, `strikes_atm_window=-1`, `rules[MtfRule{name,on,out,conditions[MtfCondition{timeframe,side,operator,threshold,call_sign,put_sign}]}]`.
- `MqaeParams` (:179): `risk_mode=aggressive`, `master_threshold=3`, velocity (`period=8`, `surge=0.012`), order blocks (`impulse=0.02`, `width=0.01`), SMC (`swing_bars=3`, `min_move=0.025`), trend rider (`trail_period=15`, `buffer=0.04`), `crossover_on`.
- `UmpParams` (:260): laws (`expansion_law_pct=20`, `bridge_law_pct=50`, `median_law_pct=50`, `price_floor_inr=50`), detection (`body_match_pts=2.2`, `scan_depth_bars=250`), visual (`show_*` rendering only, `zone_width_pct=3`), entry (`enable_long`, `enable_retest`, `max_sl_pct=10`, `trigger_timeout_bars=6`), dashboard.
- `default_config()` (:513) = Appendix-A seed; `zone_completeness()` (:617) runtime gate; `validate_document()` (:684) → `(errors, warnings)`.

## Filter engines (slot map)

| slot | module | series |
|---|---|---|
| `oi_change` | `engines/oi_structure.py` | cumulative CE/PE OI Δ (Cr) per closed minute |
| `multi_tf` | `engines/mtf_ratio.py` | same → `rows_from_cumulative_series` |
| `ratio` | `engines/mqae.py` | Green=PCR, Yellow=Ratio |

Naming trap: slot `ratio` = MQAE, slot `multi_tf` = MTF Ratio. Wiring `orchestrator.py:1376-1406` and `backtest/deps.py:270-302` are twins.
- OI Structure (:269): fractal swings → EQH/EQL first → zone → buffered breakout → 5m red candle on `candles[-2]`; matrix scoring, sweep tolerance 0.05 Cr, ties favour CALL.
- MTF Ratio (:135): first fully-matching rule wins; zero-condition rule never matches; `Above`=`>=`, `Below`=`<`; zero Δ fails both sign filters.
- MQAE (:246): 5 models + crossover; aggressive weights velocity×3/OB×3/SMC×1/cross×1 thr 3; conservative SMC×3/cross×3 thr 6; `extract_swings` left-side only.

## Ultra Master Pro (`engines/ump/`)

`levels.build_levels` (:98) 7 stages: 1H structural reversals → 39-value daily bridge pool (pivot matrix) → weekly discovery staircase → downside bridges → gap scavengers → Expansion spacing → recursive medians. Types 1=1H-STRUCT 2=DISCOVERY 3=BRIDGE 4=MEDIAN.
`engine.process_minute` (:392) folds 1m → exchange-aligned 5m; `new5m` only on closing minute and when close differs. Tick order: level rebuild (frozen in trade) → `levels_ready` → Step 1 trigger on confirmed green close via `_find_zone` (SC1/2/3) → Step 2 entries S1A/B/C, S2A/B, S3A/B/C within `trigger_timeout_bars×5` → Retest R1/R2 (never on MEDIAN) → Step 3 management. Trail ladder Base→Q1→Q2(+Q3 retest), one rung per 5m; System B (non-retest); exit priority MAX_SL → TRAIL_EXIT → SB_EXIT → TARGET → BASE_SL. `entry_guard` (:711) = premium-band hook. Warmup `warmup_ump_engine_full_life` (`orchestrator.py:1336`) replays the contract's whole life with day rollover.

## Orchestrator

Cadence (:1558): 1 s poll, fires once per minute when `second>=3`, 09:15–15:45 weekdays; `latest_minute` = bar for `now−1min` (closed-minute convention). Pass order (:246): status reset → config → date rollover → weekend → holiday (config ∪ file) → kills → ledger risk counters → open-position management first (:355) → staleness gate (`_ENTRY_STALE_AFTER_S=180`) → zone by clock → gates (zone_kill, completeness, max_trades) → readings → `_unanimous` → `strategy_active` → direction lifecycle → strike selection (`series.select_strikes_in_band` :795, sort by |ltp−mid|, top `strike_scan_count`) → hunts (first engine reporting in_trade wins; winner's `entry_guard=lambda: False`). States: `idle, weekend, killed, gated, stale_data, no_trade, hunting, hunting_hold, no_strike_in_band, no_engine_data, strategy_inactive, in_trade`.
Sizing `lots = allocated // (entry × lot_size)`; `lot_size==0` refuses loudly. Paper vs live: `ledger = paper|live`; `broker/execution.py` routes; latency simulated only for paper. Shadow mode mirrors live into paper.
Exits: engine decision latched (`pending_exit`), expiry force-close `date>=expiry and time>=15:25` unconditional, End-Exit inert when `overnight_carry` ON; exit `None` = retry next minute. `_risk_gate` (:1075): max loss → day kill; profit lock; consecutive losses → paused until `POST /api/algo/resume`. `adopt_open_trade` (:1142): newest open row (live before paper), full-life replay armed then disarmed, fallback `_AdoptedShellEngine`. Alerts via `deps.notify` → Telegram, `_alert_once` per day; `_audit_once` runtime rows.

## Broker (`broker/`)

`xts_interactive.py`: `login()` POST `/interactive/user/session`, one session/process under a lock; `_authed` one re-login; HTTP-200-with-"Invalid Token" body = auth rejection; `place_market_order` `productType=NRML` (load-bearing for carry), NSEFO/BSEFO; recovers AppOrderID from order book; `await_fill` 6×0.5 s; `XtsTransportError` = unknown state. `execution.py`: paper → simulator; live → refuse if unconfigured; instrument id via scripmaster, fallback Kite dump; entry transport error → pause + page; exit failure → None retry; `reconcile_live_position` every 5th minute.

## Backtest (`backtest/`)

`preflight.py:190` day plan (archive firsts, tz-suspect, `live_days`, `DEFAULT_EXCLUDED_DAYS`, month-sharded coverage, skip reasons). `data.py` `DayFrame` per day (cursor-dependent totals, `real_end` clamp, `preopen_spot`, `pick_strikes_in_band` historical twin). `deps.py:261` in-memory deps; `build_engine` two-level cache (full-life feed per contract + day-open engine per `(strike, ot, id(zone_cfg))` deep-copied per hunt); real broker fns safe because pinned `paper_mode=True, shadow_mode=False`. `runner.py:136`: frozen-config guard `overnight_carry = raw JSON key or False` → preflight → upsert plan (resume skips done) → delete first pending day's outputs → per day: two-deep prefetch, fresh orchestrator, `adopt_open_trade`, minute loop 09:15–15:40 with `asyncio.sleep(0)`, EOD carry-or-force-close, flush trades/signals/notifications/timeline/UMP captures. Single-flight `active_run_id`. Speed ≈1.2 s/day.

## Auth / config store / RBAC

`auth.py`: PBKDF2-SHA256 200k; token `user_id.expires.hmac`; admin seeded from env; `require_admin` (503 unconfigured/401), `require_editor` rank≥1, `require_admin_role` rank≥2. `api/algo_config.py:83` `_enforce_kill_authority`: editors cannot change master_kill, paper_mode, day/zone kills, strategy_active, all_in, allocation_pct, end_exit_enabled.
`config_store.py`: 30 s cache; save = validate → Config Lock (market hours: only `config_lock.*` edits) → INSERT → `config_change` + ≤200 diff rows → Telegram if toggled; `restore()` re-saves as new version.

## Algo REST (`/api`, admin-gated except login)

auth: `POST /algo/auth/login|logout`, `GET /algo/auth/me`.
config: `GET|POST /algo/config`, `GET /algo/config/versions`, `GET /algo/config/versions/{v}`, `POST /algo/config/restore`, `GET /algo/config/defaults`, `GET|POST /algo/config/validation`, `GET /algo/audit`.
engines (`day, zone, date?, expiry?, symbol?, at?, config_version?, config_run?, config_sandbox?`; ump adds `strike?, option_type`): `GET /algo/engines/ump|mqae|mtf-ratio|oi-structure`.
trades: `GET /algo/trades`, `GET /algo/pnl/summary|calendar`, `GET /algo/notes`, `PUT /algo/notes/{date}`, `GET /algo/paper/session`, `POST /algo/paper/reset`, `GET /algo/status`, `GET /algo/broker/status|account|reconcile`, `POST /algo/broker/test-login`, `POST /algo/resume`.
backtest: `GET|PUT /algo/backtest/sandbox`, `POST /algo/backtest/sandbox/copy`, `GET /algo/backtest/coverage`, `POST /algo/backtest/preflight`, `POST|GET /algo/backtest/runs`, `GET /algo/backtest/runs/{id}`, `POST …/{id}/cancel|resume|delete`, `GET …/{id}/days|trades|pnl/summary|pnl/calendar|equity`, `GET …/{id}/day/{date}`.

## WS `/ws/algo-stream` (`ws/algo_stream.py:79`)

Query `day, zone` required; `symbol, expiry, strike, option_type` optional; accept then cookie auth. Frames: `{type:"algo",…}`, `{type:"error"}`, `{type:"ping"}` 15 s. Client: `pong`, `set:day=…,zone=…,sym=…,exp=…,strike=…,ot=…`. `algo` fields: `seq, ts, scope, live, source∈{replay,session_closed,other_symbol,no_data,stale,live}, option_row_age_s, run_mode, nse_session_open, config_version, strike, strike_source∈{manual,band,band@last,none}, frozen_at, price.ltp, candle{m1,b5,…}, readings{intrabar,closed,closed_as_of,diverged,tail}, status{…,hunts[],position{…}}`. `algo_hub.py` fan-out by frozen `AlgoScope`, queues maxsize 4 drop-oldest; OI readings refresh detached.

## Gotchas

- `overnight_carry=True` overrides `end_exit_enabled` everywhere; only auto-close is 15:25 expiry force-close; product must stay NRML.
- P&L/risk counters attribute to exit day; `max_trades` to entry day; timestamps IST-naive under UTC label (`AT TIME ZONE 'UTC'`).
- `include_forming=True` only for the preview stream. `premium_life_from_minutes` cuts at 15:29.
- `master_kill` blocks entries but an open trade keeps being managed.
- Migration 0013 quality rule makes archive days backtestable; live arm ungated.
- Side rule: dominant Side = SMALLER signed OI Δ (don't revert).

---

# PART 4 — Frontend (`frontend/`)

## Toolchain

React 18.3, `echarts` 5.5 + `echarts-for-react`, `lightweight-charts` 4.2, `write-excel-file`; vite 5.4, tailwind 3.4, TS 5.6 strict, eslint `--max-warnings 0`. Scripts `dev`, `build` (`tsc -b && vite build`), `preview`, `lint`. `vite.config.ts`: `envDir` = repo root (reads root `.env`), port 5173, proxies `/api` and `/ws` to `VITE_API_BASE` with `localhost→127.0.0.1`. Tailwind tokens: `bg #0b0f17`, `panel #111827`, `border #1f2937`, `ce #ef4444`, `pe #22c55e`, `accent #3b82f6`. `index.css` classes `.panel .pill .pill-active .kpi .range-input`. Env: `VITE_API_BASE`, `VITE_WS_BASE`, `VITE_DEV_WS_DIRECT`.

## Entry / routing / gates

`main.tsx` → `<LoginGate><App/></LoginGate>`. No router: `App.tsx:14-36` `useState<Page>` over tabs `oi-change, charts, mtf, ratio, replay, algo-config, backtesting`; one shared `useMarketContext()` passed as `mc`; tabs render when `mc.dataReady`; Guide link `/platform-guide.html`; `SymbolChangeConfirm` mounted once.
`LoginGate`: in-memory, `POST /api/auth/main-login` (MAIN_USER/PASSWORD), not enforced on `/api`. `AlgoAdminGate`: real gate, `GET /api/algo/auth/me` restores httpOnly `algo_session`; 401 → form, 503 → not configured; listens `algo:unauthorized`; render-prop `children(identity, signOut)`.

## API clients

`api/rest.ts` `api.*`: `health, spot, niftyCrossCheck, expiries, oiChange, oiChangeRange, oiTimeseries, optionChainFull, historyDates, ratioTimeseries, multiTimeframe, replayFrames, symbols, setActiveSymbol, authStart, login, gateLogin`.
`api/algoRest.ts`: `AlgoApiError{status, errors[]}`; auth, config (`getConfig, saveConfig, versions, version, restore, validation, validateDocument, configDefaults`), engine evals (`oiStructureEval, mtfRatioEval, mqaeEval, umpEval` with `{day,zone,date,symbol,expiry,at,configVersion,configRun,configSandbox}`), sandbox, backtest (`coverage, preflight, create, runs, run, cancel, resume, delete, days, trades, pnlSummary, pnlCalendar, equity, dayBundle`), `brokerAccount`, notes, `trades, pnlSummary, pnlCalendar, paperSession, paperReset, orchestratorStatus, resumeEngine, audit`.
`api/ws.ts` `OIStream`: frames `oi_change|option_chain_full|ping|error`; pong reply; backoff 500 ms→30 s; 55 s idle close; `set:` re-scope on same socket. `api/algoWs.ts` `AlgoStream`: same contract, `AlgoFrame`, `setScope()`.

## Hooks

`useMarketContext` (health 3 s, expiries 30 s; `authenticated`, `dataReady`, `feedLive`, `atmWindow` default 5, `liveSpot`; symbol pinned NIFTY, confirm gate on leaving; no auto-login). `useOIStream`, `useOIChange`, `useOIChangeRange` (3 s poll when live edge), `useOITimeseries` (20 s), `useRatioTimeseries`/`useMultiTimeframe` (20/15 s), `useReplayFrames`, `useAvailableDates`, `useAlgoStream` (+`AlgoStreamContext`), `useConfigDraft(load, save)` (draft/diff/confirm cycle shared by live config and sandbox).

## Pages

Dashboard (SpotHeader, SymbolSelect, TimeframeBar, DatePicker, ExpirySelect, AtmWindowSelect, ExportButton, TimeRangeSlider, KPIBar, OIChangeChart, FeedOfflineBanner; preset/custom/historical paths reconciled :158-175). ChartsPage (client-side ATM window → `strike_min/max`, two OICandleChart panes). MultiTimeframePage. RatioChartPage (`full_day` client-side cumulative). ReplayPage (781 lines, fetch once then client-side scrub; throttled chart panes; steps 1m/5m/15m, speeds 1–15×). AlgoConfigPage (sub-tabs Daily Config · 4 engines · P&L · Paper · Holidays · Integrations · Security · Validation; LiveStrip; dirty/Save/Discard). BacktestingPage (same UI bound to sandbox; Runs & Results `BacktestPanel`; `RunPicker`; `WorkspaceSecurity`).

## Components / utils / types

Charts: `OIChangeChart`, `OICandleChart`, `charts/TimeSeriesChart` (lightweight-charts, created once), `TimeRangeSlider`, `ReplayController`, `KPIBar` (PCR = Put/Call), `ChartDrawingOverlay` (only localStorage user, prefix `oi.drawings.v1.`). Algo: `DailyConfigPanel, EnginesPanel, OiStructurePanel, MtfRatioPanel, MqaePanel, UmpPanel, UmpTvChart, BacktestPanel, BacktestDayReplay, PnlPanel, SecurityPanel, ValidationPanel, MiscPanels, LiveStrip, ConfirmSaveModal, EngineActions, controls.tsx, algoDraft.ts, backtest/overrides.ts`.
Utils: `formatMarket`, `sessionTime` (555/940/385 min), `oiCandles` (anchored 09:15), `oiStrikeWindow` (`atmRound`, `effectiveAtmWindow`: N shows ATM±(N−1), 0=ATM only, <0=all), `netOiMath`, `ratio` (`callPutRatio`, Side = smaller signed Δ), `exportData`, `num`, `ui`, `charts/chartTime` (+5:30 baked), `chartTheme`.
Types: `types.ts` (`Timeframe, OIChangeRow/Response, HealthResponse, SymbolEntry, OptionChainFullRow…`), `types/algo.ts` (848 lines: `Weekday, ZoneId Z1|Z2|Z3, IndicatorKey, AlgoConfigDoc, *EvalResponse, TradeRow, OrchestratorStatus, Backtest*`).
State pattern: lifted hook + per-page state, one context, server-owned config with draft/diff/confirm, near-zero client persistence.

---

# PART 5 — Deploy / ops / docs

## Docker

`docker-compose.yml`: network `nifty-oi` pinned subnet `${DOCKER_BRIDGE_SUBNET:-172.28.0.0/16}`; logs 50 MB×5. `timescaledb` (pg16, `POSTGRES_PASSWORD` required, `${DB_BIND:-127.0.0.1:5432}`), `backend` (`env_file ../.env`, `DB_URL` to `timescaledb:5432/oi`, `extra_hosts warp-proxy`, `stop_grace_period 45s`, `${API_BIND:-127.0.0.1:8000}`, command `alembic upgrade head && uvicorn`), `frontend` (`${FRONTEND_BIND:-0.0.0.0:80}`). `docker-compose.override.yml` local-only `RUN_MODE: replay`. `Dockerfile.backend` python:3.11-slim; `Dockerfile.frontend` node:20 build → nginx:1.27 (`nginx.conf` proxies `/api/`, `/ws/` to `backend:8000`, SPA fallback).

## Scripts

`deploy.sh` (forces `--env-file .env -f docker/docker-compose.yml`, log snapshot, deploy marker, network-split guard, health gate; `ps|logs|restart|logs-save`). `install-sentinel.sh` (installs `oi-sentinel`, `oi-doctor`, `oi-backup`, `oi-topup`, systemd units, `warp-socks-bridge`). `oi-sentinel.sh` (per-minute dead-man: daemon/container/strict probe, K=5 fails + uptime>10 min → restart backend, max 2/day, never touches broker; Telegram grepped from `.env`). `nightly_topup_vps.sh` (`oi-topup`: WARP probe, ledger reset, `truedata_backfill.py pull/pull-ticks` NIFTY+SENSEX, refresh `oi_day_stats` then `live_days`, validate + freshness assert). `truedata_backfill.py` (`probe|pull|pull-ticks|pull-eod|pull-flows|pull-news|validate`). `start-all.ps1` (via `start.bat`, layers override when present). `bootstrap.ps1` (via `Setup-and-Run.bat`). Others: `backup-db.sh`, `backup-oi.ps1`, `oi-doctor.sh`, `harden-vps.sh`, `init_db.sql`, `build_symbols*.py`, `seed_scripmaster.py`, `probe_*.py`, `repair_archive_tz.py`, `verify_option_chain.py`, `stack_scenario_sweep.py`, `lakshmishree_live_order_test.py` (REAL order), `build-windows-exe.ps1`, `run_*.cmd` repair wrappers.
systemd: `oi-sentinel.timer` (every minute), `oi-backup.timer` (02:30 IST), `oi-topup.timer` (Mon–Fri 17:35 & 21:00, Tue–Sat 06:30 IST), `oi-alert@.service`, `warp-socks-bridge.service` (socat 172.28.0.1:40000 → 127.0.0.1:40000).

## Prod update (UPDATE-PRODUCTION.md)

`ssh root@187.127.206.41`, `cd /root/nifty-oi`, `git pull origin ayush-bhai-branch`, `docker compose --env-file .env -f docker/docker-compose.yml up -d --build` (or `bash scripts/deploy.sh`). Migrations automatic; `.env` untouched; ~30 s feed blip; rollback = `git reset --hard <hash>` + rebuild. Never commit/push/touch VPS without explicit ask.

## Docs index

`ARCHITECTURE.md`, `AUTH.md`, `DEPLOY.md`, `LOCAL-DEV.md` (single-session rule), `MONITORING.md` (steward + sentinel + Telegram), `RUNBOOK.md`, `UPDATE-PRODUCTION.md`, `VPS-DEPLOY-PLAN.md`, `HOSTINGER-KVM4-DEPLOY.md`, `ump-pine-conformance.md`, `ump-pine-comparison-2026-08-18.md`, `spec-conformance-audit-2026-08-18.md`, `hidden-standalone-rebuild.md`, `DATA-VERIFICATION-REPORT.md`, `VPS-PROVIDER-PRICING.md`, `platform-guide.html`, `data-consistency-audit.html`, `reference/nifty_ultra_master_pro_v20.pine`.
