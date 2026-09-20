# Project Full Inventory — 2026-09-06

Complete collection of everything known about this project: all 24 memory files, every contact and external reference, every document, the code structure, and the result of running and verifying the whole stack on 2026-09-06 (Sunday, market closed).

Repo: `D:\trading algo (ayush bhai)\trading algo (ayush bhai)` · branch `ayush-bhai-v2` (HEAD `faceeaf`) · production runs `ayush-bhai-branch` on `oialgo.tech`.
Companion (deep technical map): `docs/PROJECT-CONTEXT-MAP.md`. No secret values appear in this file (masked as `xxx***`).

---

## Part A — Run and verification results (2026-09-06)

### A.1 What was run

| Step | Result |
|---|---|
| Docker Desktop | Was stopped; started. Compose project `docker` auto-started (restart: unless-stopped). |
| Containers | `docker-timescaledb-1` healthy (127.0.0.1:5434), `docker-backend-1` healthy (127.0.0.1:8000), `docker-frontend-1` up (0.0.0.0:80). Unrelated containers on this PC: `market-tools-*` (8001/8080), stopped `broker_*_wbl`, `qa-backfill2`. |
| Backend env (container) | `RUN_MODE=live`, `FEED_VENDOR=truedata`, `TRUEDATA_WS_PORT=8084`, `TD_RELAY_ENABLED=true`, `MARKET_CLOSE_IST=15:40`, `PERSIST_BUCKET=1s`, `STRIKE_WINDOW=11`. Override file (replay pin) is NOT layered. |
| Image freshness | backend image 2026-09-03 00:55 (only `tests/test_ump_engine.py` + `scripts/start-all.ps1` newer); frontend image 2026-08-21 (no `frontend/` source newer). No rebuild needed. |
| Boot sequence | alembic head `0013_data_health`; TD defensive logout → 75 s cool-down → `td_session.live maxsymbols=50 subscription=tick validity=2026-09-20` → 47 contracts subscribed (46 live) → aggregator flushing → td_poller sweeping → algo orchestrator + algo-stream started. `truedata.auth.ok` (REST bearer). |
| DB | 28 tables. Live `option_oi_snapshots`: NIFTY 7.44 M rows 2026-05-27→2026-09-03 15:40 IST; archive `oi_archive_bars`: NIFTY 14.9 M + SENSEX 11.1 M rows 2026-02-05→2026-09-02; `eod_bars` 148 k; algo config live v68 → (see A.3) v81; 0 algo trades; 15 backtest runs; 430 audit rows. |
| Public API | `/api/health` 200, `/api/health/strict` 503 `session_wedged` during TD cool-down then healthy, `/api/symbols` (230), `/api/expiries` (35), `/api/history-dates`, `/api/oi-change`, `/api/option-chain(-full)`, `/api/multi-timeframe`, `/api/oi-timeseries`, `/api/ratio-timeseries` (11.5 s — slow, known), `/api/iv-scanner`, `/api/interpretation` all 200 with data as of 2026-09-04 15:40 IST. Bad main-login → 401; anonymous `/api/algo/*` → 401. |
| Admin API | login OK (`ayush`/admin); `/api/algo/config` v68 "restore of v62"; validation 0 errors (overnight-carry warnings); status idle; broker "configured — awaiting burn-in"; pnl summary; paper session ₹30,000; backtest runs list. |
| WebSockets | `/ws/oi-stream` delivers frames (needs an allowed Origin, e.g. `http://localhost`); `/ws/algo-stream` 403 without admin cookie (expected). |
| Frontend | `http://localhost/` serves the SPA ("NIFTY OI Analytics"). `tsc --noEmit` clean; `vite build` clean (8.9 s). |
| Backend unit tests | **362 passed** (throwaway container from the backend image + `pip install pytest pytest-asyncio`, repo mounted at `/backend` and `/scripts` — `test_shadow_isolation.py` reads source at that fixed path). |
| Backtest parity | `validation/backtest_parity.py` → **633/633** (days 2026-08-31, 09-01, 09-02). |
| Scenario sweep | `scripts/stack_scenario_sweep.py` → **144/144** after fixing a stale hardcoded date in the S3 expiry check (script patched: uses today's date). Ran in-container with the frontend URL rewritten to `http://frontend/`. |

### A.2 How to repeat

```powershell
# stack (from repo root)
docker compose --env-file .env -f docker/docker-compose.yml up -d
# unit tests
docker run --rm -v "${PWD}\backend:/backend" -v "${PWD}\scripts:/scripts" -w /backend docker-backend:latest sh -c "pip install -q pytest pytest-asyncio; python -m pytest tests -q"
# parity (in container)
docker exec docker-backend-1 sh -c "cd /app && PYTHONPATH=. python validation/backtest_parity.py"
# sweep (in container; needs ALGO_SESSION/ALGO_SESSION2 cookies minted via /api/algo/auth/login)
```

### A.3 Side effects of this session

- Config versions 69–80 were created by the sweep's documented benign writes (no-change save, lock ON/OFF, paper-session reset ×3). The only content difference vs v68 was `paper.session_started` (2026-08-16 → 2026-09-06; ledger had 0 trades). **Restored v68 content as v81** via `/api/algo/config/restore`.
- `scripts/stack_scenario_sweep.py` patched (S3 expiry threshold now `date.today()`).
- Nothing committed or pushed. VPS untouched.

### A.4 Findings worth acting on

1. **Password in source comments**: `backend/app/market_data/truedata_rest.py:352` (tracked, uncommitted diff) and `backend/app/ingest/truedata_feed.py:328-329` (untracked) contain the real TrueData password in comments. Remove before any commit.
2. **Stale VPS IP** `187.127.206.41` in `docs/UPDATE-PRODUCTION.md` and `docs/PROJECT-CONTEXT-MAP.md`; current is `200.97.169.102`.
3. `NIFTY_LOT_SIZE` default 75 in `config.py:173` (real 65; `.env` has 65).
4. `.env.example` lacks 9 settings that `config.py` reads (`ALERT_MIN_INTERVAL_S`, `ALGO_STREAM_*` ×5, `POLLER_FAILOVER_*` ×2, `POLLER_MAX_429_RETRIES`).
5. Telegram not configured locally → all alerts silent. `OI_BACKUP_REMOTE` unset on VPS (off-box backup = #1 SPOF).
6. QA 2026-09-02 open defects H1–H4, M1–M4, L1–L7 (`docs/qa-algo-backtest-2026-09-02.md`).
7. `/api/ratio-timeseries` takes ~11.5 s (Engine B gapfill over full strike range) — known, deferred.
8. Huge uncommitted working tree: 31 modified + 91 untracked files (entire algo engine, TrueData migration, migrations 0006–0013, 23 test files). Commit only on explicit ask (deploy-consent rule).
9. Packaging spec + `start-backend.ps1` still reference Angel One leftovers.
10. Local stack is `RUN_MODE=live` on the paid TrueData login (single realtime session) — the VPS is still XTS, so no collision today; when the VPS becomes the TD owner, this PC must switch to `FEED_VENDOR=td_relay`.

---

## Part B — Memories (all 24 files, digested)

Location: `C:\Users\DHARMIK\.claude\projects\d--trading-algo--ayush-bhai--trading-algo--ayush-bhai-\memory\` (index `MEMORY.md`, 145 KB total).

| Memory | Type | Essence |
|---|---|---|
| **deploy-consent** | feedback | Build/test locally only. Never commit, push (`orvinus/Option-Chain-OS`) or touch the VPS (`oialgo.tech`, `~/nifty-oi`) unless that message asks. Even read-only VPS commands need an ask. Flow: `ayush-bhai-v2` → PR → `ayush-bhai-branch` → VPS. |
| **xts-broker-gotchas** | project | Angel One → XTS (Lakshmishree, `trades.lakshmishree.com`). XTS md is SINGLE-SESSION per appKey (every login kills prior token; tokens daily). Socket drops every ~83 s. 50-instrument cap → `STRIKE_WINDOW=11` (47 subs). Local stack stealing prod session was the recurring breakage → local XTS keys blanked, backend 409-gates login unless live. Native Windows postgres owns host 5432 (container on 5434; harness via socat 55432). Prod IP moved 187.127.206.41 → 200.97.169.102 (Mumbai). `https://oialgo.tech/api/health` public. Wedge recovery: `deploy.sh restart backend` then `POST /api/auth/login`. |
| **market-data-cross-check-sources** | reference | NSE v3 API (`option-chain-v3`, OI in LOTS, cookie warmup, no brotli); Sensibull oxide (`live_derivative_prices/256265`, now auth-gated → browser capture); Kite public instruments (`exchange_token` == XTS id); lots NIFTY 65 / SENSEX 20; validation harness `backend/validation/` run in-container; layer F endpoint-delta OI-change check (gap-immune). 2026-08-17 TD validation: all PASS, ΔCE exact 0. `/api/option-chain` freshest-spot bug fixed. |
| **multi-symbol-poller-mcx** | project | 230 F&O symbols (210 NSE + 9 BSE + 11 MCX) via universe poller (REST quotes, tiers 20/60/180 s, needs the ACTIVE session token — stale → 400 "Invalid Token"). MCXFO=51, MCX lot = Multiplier col 13. Token ranges disjoint across exchanges. Index alias map. `build_symbols_from_master.py`. Migration 0002 compression/retention. |
| **v2-analytics-upgrade** | project | Milestone 1 (2026-07-22): B1 baseline-floor fix, `/api/history-dates`, `/ratio-timeseries`, `/multi-timeframe`, batched replay + greeks (migration 0004), Multi-TF/Ratio/Replay tabs, lightweight-charts, export, drawing tools. Shipped live 2026-07-24 (PR #1). VPS repo `~/nifty-oi`; frontend on 127.0.0.1:8080 behind Caddy. |
| **market-data-integrity** | project | Invariants: `ltp` NULL ≠ 0; T from real settlement instant (old 1 h floor inflated expiry IV 2.5×); spot from freshest row; constant strike set per bucket; per-strike first-seen baselines; history loops gated on chain date; `session_floor_for()`. Deferred: fractional strikes, holiday calendar, two query engines. Local `restart: unless-stopped` hazard. |
| **side-rule-and-consistency** | project | Dominant Side = SMALLER *signed* OI Δ (Ayush's spec; don't revert). F1–F5 consolidation (window, PCR, ATM round, formatters, Δ colour). `as_of` on oi-change. Audit at `docs/data-consistency-audit.html`. |
| **local-docker-runtime** | project | Live local stack = compose project `docker` (:80/:8000/DB 5434). `/hidden` dashboard REMOVED 2026-08-18 (commit 3265cb8; blueprint `docs/hidden-standalone-rebuild.md`). `/` gate = MAIN_USER/MAIN_PASSWORD (display-only). Stale frontend image can look like a login gate. |
| **reliability-overhaul** | project | Outage #2 (2026-08-06) login-storm livelock. Fix invariants: ONE rotation authority (SessionSteward; 90 s floor, burst budget, circuit), no interval refresh (08:35 IST daily), demoted actors, WS-origin freshness split, `POLLER_MODE=failover`, ladder → `os._exit(1)`, `/api/health/strict` contract, oi-sentinel (systemd 1/min, cap 2 restarts/day, Telegram), deploy.sh log snapshots. |
| **truedata-migration-report** | project | Rev 2 feasibility report in `TD_API_Documents/` (conditional proceed; 4 probes). Steward NOT portable (TD rejects 2nd login → wedge). Backfill pipeline (migration 0005, `truedata_backfill.py` probe/pull/validate/pull-ticks/pull-eod/pull-flows/pull-news). Collection executed 2026-08-10 (18.6 M bars). Deployed to VPS 2026-08-10 via COPY transfer (TD blocks VPS IP). Outage #3 (2026-08-11): gateway cycling, steward ignoring requests, UNION-view scans exhausting the pool → hot paths back on base table. TD live WS probe: 0 disconnects, OI ~1/min. Data-quality campaign resolved 9 issues; 39 spot-less sessions are vendor gaps. `TD_API_Documents/` holds live creds — never commit. |
| **truedata-realtime-auth** | project | Paid account (validity 2026-09-20, maxsymbols 50, `tick`, segments fo/ind/bseind/bsefo). Password must go RAW in the WS query (percent-encoded `@` = "Invalid User Credentials"). Port 8084 only. Trust `success` flag. httpx leaked password to logs → loggers pinned to WARNING. |
| **vps-truedata-warp** | project | TrueData/NSE drop the VPS's hosting ASN. Fix: Cloudflare WARP proxy mode `socks5://127.0.0.1:40000` + `TRUEDATA_PROXY`. Nightly top-up VPS-resident (`nightly_topup_vps.sh`, 17:35 IST); Windows task disabled. |
| **td-feed-latency-findings** | project | TD leads XTS ~0–1 s; VPS clock +0.9 s. Heartbeat-only watchdog false-flap bug (every ~26 s) fixed: any data frame = liveness. Shadow mode needed for tick-level lag. |
| **xts-interactive-live-contract** | project | Dead token = HTTP 200 + "Invalid Token" body (never 401) → relogin fix; one session per appKey kicks; ~70 ms calls; balance shape; test-login now probes balance. Stage cookies via `/sync/session.txt`. |
| **live-trading-audit-2026-08-17** | project | 6 money-path fixes (Kite instrument fallback, exit routes by `entry_order_id`, dup-order guard, restart adoption + migration 0012, staleness gates, pnl basis by ledger). Report-only: Telegram unset locally, no margin pre-check, partial fills, kills don't force-exit, End-Exit ≠ 15:40, LTP-carry. |
| **algo-config-build** | project | The trading-engine arc (2026-08-13/14): 12 source docs (Pine v24 = authority), locked decisions (strike-in-band, engine→slot map OI Structure→"OI Change", MTF→"Multi-TF", MQAE→"Ratio"; paper-first; single admin). Engine facts (ties, rounding, Pine `var` rollback semantics). M0–M7 all built: migrations 0008/0009, engines + golden fixtures, orchestrator (clock-driven, §6 suppression, risk counters), fees (₹17/order), broker adapter (Lakshmishree Interactive; 1-lot test rejected "Margin Exceeds" = unfunded account; qty in UNITS), reconcile, shadow mode, §15 gates. Exhaustive sweeps (40/40, 22/22, 116/116). Session-secret bug fixed. |
| **backtest-engine** | project | Backtest = live orchestrator with swapped deps; closed-minute/no-lookahead conventions; migration 0010 tables + `/api/algo/backtest/*`; day-replay bundle; `+1µs` gapfill live fix; parity 633/633 + golden byte-identical (run on closed windows only); FastAPI 0.141 `app.routes` gotcha; speed arc 15 s → ~1.2 s/day; sandbox workspace (migration 0011); scenario presets, run queue, entry funnel; benchmark run #9: 5 trades, −₹2,799. |
| **spec-conformance-and-multistrike** | project | 2026-08-18 audit: engines clean; 17 fixes (strategy_active dead switch, blended P&L by date, SENSEX→BSEFO, RBAC, alerts wired, holiday union, real paper latency, audit rows, band-strike default, lot fail-loud). Top-N multi-strike `strike_scan_count` (model default 1, seed 3, live 3). Round 2: P0 engine crash-loop (`from ..runtime import rt`), order-state safety, ingest hardening (24/7 hold-last bloat), perf floors (7 d spot, 120 d windows). |
| **ump-pine-parity** | project | Realtime-semantics ruling (D3/D6/A2 conform). Fixes D1/D2/D4/D5/D11. All 26 settings live (show_* rendering-only). Expiry-to-expiry continuity + OVERNIGHT CARRY (live too): full-life replay, 15:29 cut (later 15:40), Pine timeout = wall-clock, End-Exit inert under carry, 15:25 expiry force-close, exit-day P&L attribution. |
| **ump-tv-parity-2026-09-02** | project | 20-contract TV diff: fixed 15:40 close (since 2026-08-03), official bhavcopy daily H/L/C, archive OHLC, puller expiry sentinel, trail label. 98.7% events aligned from 26 Aug. TV runs Trigger Timeout = 1. Harness cmds in `logs/`. |
| **project-context-map** | project | `docs/PROJECT-CONTEXT-MAP.md` (2026-09-02) = 5-part briefing; read the relevant part before any change. |
| **qa-algo-backtest-2026-09-02** | project | 4 high defects open: H1 stale-draft overwrite after paper reset, H2 120-day premium floor hides old UMP history, H3 preflight dup-risk stale vs 0013, H4 confirm-off bypass; M: Telegram "Connected" hardcoded, copy reloads → gate, replay deep link. |
| **td-relay-and-gapfill** | project | `/ws/td-relay` owner (`TD_RELAY_ENABLED`) + `FEED_VENDOR=td_relay` follower share one TD login; boot gapfill task; doc `docs/td-relay-and-gapfill.md`; local `.env` has `TD_RELAY_ENABLED=true`. Not deployed to VPS. |

---

## Part C — Contacts and external references

### People
| Name | Role | Where |
|---|---|---|
| **Ayush ("Ayush Bhai")** | Client/owner; branch namesake; dashboard + algo admin user `ayush` | README, `data/symbols.json`, `.env` |
| **Dharmik Solanki** (`Dharmik-Solanki-G <dharmiksg7@gmail.com>`) | Developer; sole git author; "Dharmik's PC" = local stack | git log, memories |
| `orvinus` | GitHub owner of the remote | `github.com/orvinus/Option-Chain-OS` |
| Session user | `atik06116@gmail.com` (not in any file) | — |

### Vendor contacts
| Vendor | Contact | Where |
|---|---|---|
| TrueData support | `support@truedata.in`, `+91-7304-22-44-66` | `TD_API_Documents/TrueData_Migration_Feasibility_Report.md:145`, `XTS_vs_TrueData_Ultra_Comparison.md:25` |
| TrueData docs | `https://truedataapi.readme.io/reference`, `https://tdcorpapi.readme.io/reference`; Postman workspace `truedata-9910.postman.co` | `TD_API_Documents/README.txt` |
| Lakshmishree / Symphony | portal `https://trades.lakshmishree.com/` (md: `/apimarketdata`; interactive: `/interactive/...`); docs `symphonyfintech.com/xts-trading-front-end-api-v2/`; demo host `developers.symphonyfintech.in/apibinarymarketdata`; account id `MM31147` (investor client) | `.env`, memories |
| Hostinger | KVM 4, Mumbai AS47583; support articles 5723772/7978544/8306612 | `docs/HOSTINGER-KVM4-DEPLOY.md` |
| Telegram | `@BotFather` → `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` (unset everywhere) | `.env.example`, `docs/MONITORING.md` |
| Healthchecks | `HEALTHCHECKS_URL` → `hc-ping.com` (optional) | `scripts/oi-doctor.sh` |

### Hosts, ports, endpoints
| Item | Value |
|---|---|
| Production | `https://oialgo.tech` (Caddy → 127.0.0.1:8080 frontend; nginx `/api`,`/ws` → backend:8000); VPS IP **200.97.169.102** (old 187.127.206.41 dead); `ssh root@oialgo.tech` with `id_ed25519`; repo `/root/nifty-oi`; venv `~/.venvs/oi`; compose project `docker` |
| Local | frontend :80, API 127.0.0.1:8000, DB 127.0.0.1:5434, Vite dev :5173, socat proxy :55432 (when present); bridge 172.31.0.0/16 (VPS 172.28.0.0/16); network `nifty-oi` |
| TrueData | `auth.truedata.in/token`, `history.truedata.in`, `api.truedata.in` (master, logoutRequest), `push.truedata.in` **wss 8084** (prod) / 8086 (trial), `greeks/analytics/corporate/mf.truedata.in` (add-ons), `corp.truedata.in:9092`, `tcp.truedata.in:7070`, `wstest.truedata.in` |
| WARP (VPS) | `socks5://127.0.0.1:40000` host / `warp-proxy:40000` in container (`scripts/systemd/warp-socks-bridge.service`) |
| Cross-check | NSE `nseindia.com/api/option-chain-v3`, Sensibull `oxide.sensibull.com/v1/compute/cache/...` (NIFTY 256265, SENSEX 265), Kite `api.kite.trade/instruments/NFO|BFO`, Yahoo `query1.finance.yahoo.com/v8/finance/chart/^NSEI` |
| XTS tokens | NIFTY 26000, BANKNIFTY 26001, FINNIFTY 26034, MIDCPNIFTY 26121, NIFTYNXT50 26054, SENSEX 26065, BANKEX 26118; segments NSECM 1, NSEFO 2, NSECD 3, BSECM 11, BSEFO 12, MCXFO 51 |
| Schedules | token rotation 08:35 IST; heartbeat 08:50; session 09:15–15:40; expiry force-close 15:25; oi-sentinel 1/min; oi-backup 02:30 IST; oi-topup 17:35 & 21:00 Mon–Fri + 06:30 Tue–Sat; Windows `OI-Nightly-Topup` DISABLED; market-up task 09:05 |

### Subscription / plan facts
- TrueData paid tier activated 2026-08-21, **expires 2026-09-20**; port 8084; maxsymbols 50; `tick`; NSE F&O + BSE F&O + indices; one realtime login per user (hence the relay). Trial ended ~Aug 20 (port 8086).
- XTS: ~50 instruments per event type (1501 touchline, 1510 OI); one session per appKey; Lakshmishree ₹17/order options; RMS rejected 1-lot test for ₹416 margin (account then unfunded; later ₹1,000).
- Lots: NIFTY 65, SENSEX 20; MCX from Multiplier col 13.

### Secrets — where they live (values masked)
| File | Keys | Tracked? |
|---|---|---|
| `.env` | XTS_MD_APP_KEY `3df***`, XTS_MD_SECRET_KEY `Jwl***`, TRUEDATA_USER `tdw***`, TRUEDATA_PASSWORD `ayu***`, MAIN_USER/PASSWORD `ayu***`/`DDA***`, ALGO_ADMIN_USER/PASSWORD `ayu***`/`ayu***`, LAKSHMISHREE_INTERACTIVE_APP_KEY/SECRET `218***`/`Dne***`, TD_RELAY_KEY `PRz***`, POSTGRES_PASSWORD `pos***` | no (gitignored) |
| `backend/app/market_data/truedata_rest.py:352`, `backend/app/ingest/truedata_feed.py:328-329` | TrueData password in comments | tracked (uncommitted diff) / untracked — **remove** |
| `TD_API_Documents/`, `TD Postman Collection/` | vendor user `utkr1` + 2 passwords, 53 bearer tokens | untracked — keep untracked |
| memory `truedata-realtime-auth.md` | raw TD password | outside repo |

---

## Part D — Documents (every file)

### Root
- `README.md` (7.4 KB, 2026-08-04) — branch scope (NIFTY 50 + SENSEX, OI Change only), stack, three run modes (`Setup-and-Run.bat`, `start.bat`, manual), ports, auth notes, SENSEX entitlement warning.
- `.env.example` (18.4 KB, 2026-09-02) — all env keys with commentary (see Part C and code inventory §2).
- `Setup-and-Run.bat`, `start.bat` — launchers → `scripts/bootstrap.ps1` / `scripts/start-all.ps1`.
- `.gitattributes` (LF pins for shell/systemd), `.gitignore`.

### docs/
| File | Size / date | Content |
|---|---|---|
| `ARCHITECTURE.md` | 6.1 KB, 08-04 | Pipeline XTS → ws_client → queue → aggregator → TimescaleDB → OI engine → REST/WS → React; boot sequence; PERSIST_BUCKET semantics; failure modes. |
| `AUTH.md` | 6.0 KB, 08-04 | XTS md auth flow, env, recovery (`POST /api/auth/login` is the only force path), dead `XTS_LOGIN_AT_STARTUP` removed. |
| `DEPLOY.md` | 4.2 KB, 08-06 | Hosting requirements (one replica, no scale-to-zero), scheduling. |
| `LOCAL-DEV.md` | 3.9 KB, 08-18 | Single-session rule; local replay default; guards. |
| `UPDATE-PRODUCTION.md` | 4.2 KB, 08-06 | VPS update routine (pull + rebuild); **stale IP 187.127.206.41**. |
| `DATA-VERIFICATION-REPORT.md` | 14 KB, 08-04 | XTS vs NSE cross-check design (`/api/verify/*`, `scripts/verify_option_chain.py`). |
| `MONITORING.md` | 7.6 KB, 08-06 | Three-layer reliability (steward, oi-sentinel, Telegram), strict-health contract, drills D1–D5, SPOFs. |
| `RUNBOOK.md` | 7.6 KB, 08-06 | Setup, daily checks, config table, troubleshooting. |
| `HOSTINGER-KVM4-DEPLOY.md` | 19 KB, 08-06 | Full VPS guide incl. firewall duality, `.env` table, poller tier rollout §11a, backups. |
| `VPS-DEPLOY-PLAN.md` | 11 KB, 08-04 | Generic phases A–I. |
| `VPS-PROVIDER-PRICING.md` | 6.1 KB, 07-16 | Pricing snapshot 13 May 2026 (Hostinger/DO/Hetzner/Lightsail). |
| `hidden-standalone-rebuild.md` | 18 KB, 08-18 | Blueprint to rebuild the removed `/hidden` app standalone (REST/WS contracts, components, math). |
| `qa-algo-backtest-2026-09-02.md` | 8.4 KB, 09-02 | QA report: H1–H4, M1–M4, L1–L7 + 30-case API matrix. |
| `spec-conformance-audit-2026-08-18.md` | 24 KB, 08-18 | 17 fixes + Round 2 (P0 crash loop, ingest, perf, governance). |
| `td-relay-and-gapfill.md` | 6.7 KB, 09-03 | Relay owner/follower design, wire format, gapfill settings, pull-eod addendum. |
| `ump-pine-comparison-2026-08-18.md` | 24 KB, 09-02 | 67-block Pine↔port table, 26 inputs, divergence register, §6b overnight carry. |
| `ump-pine-conformance.md` | 14 KB, 09-03 | Section map, 11 verdicts, vl72 addendum, TV data-path corrections. |
| `ump-tv-parity-report-2026-09-02.md` | 8.3 KB, 09-03 | Final TV parity numbers per contract, residuals, open items. |
| `PROJECT-CONTEXT-MAP.md` | 45 KB, 09-02 | 5-part technical map (ingest, API/DB, algo, frontend, ops). |
| `data-consistency-audit.html` | 25 KB, 07-31 | Two-engine drift audit, Side rule, F1–F5. |
| `platform-guide.html` | 30 KB, 08-15 | Operator guide (identical copy served at `/platform-guide.html`). |
| `reference/nifty_ultra_master_pro_v20.pine` (vl72, 1,553 lines), `..._2026-08-18.pine` (1,486 lines, SHA 8C5545…), `ump_v20_parity_debug.pine` (1,591 lines) | 09-02 | Pine authority + debug export variant. |

### TD_API_Documents/ (+ duplicate `TD_API_Documents (1)/`, untracked)
- `README.txt` — vendor portal URLs.
- `TrueData_Historical_Data_Reference.md` (22 KB) — every historical endpoint, CSV column order (volume before oi), lookback (ticks 5 d, bars 6 mo, daily 10 y), rate-limit contradictions, 11 open items.
- `TrueData_Migration_Feasibility_Report.md` (131 KB, Rev 2, 2026-08-07) — conditional proceed; 13 probes; 46-risk register; Annex E security (creds in Postman).
- `XTS_vs_TrueData_Ultra_Comparison.md` (38 KB) — 19-section vendor comparison.
- PDFs: API endpoints list (670 KB), Corporate list (670 KB), Corporate API v1.4 (1.7 MB), Market Data API v2.6 (1.9 MB), Mutual Funds v1.1 (1.5 MB), TCP API v2.3 (1.7 MB); `News_API_Documentation (1).docx` (320 KB).
- `TD Postman Collection/` (7 JSON) — Analytics, Auth, Corporate, Greeks, Mutual Funds, Rest API 1.0, Symbol Master (endpoint lists in the document-sweep; contain real creds/tokens).

### logs/
- `backfill/*.md` (34): probes 2026-08-10 (P-C/P-E pass on re-run), NIFTY/SENSEX pulls (up to 10.4 M rows/run), ticks (~1.35 M), EOD (742 days index), flows/news 0 rows, validate reports (40 spot-less sessions, 20 OI-collapse days catalogued 2026-08-11).
- `validation/2026-08-17-td/REPORT_RAW.md` — 566 PASS / 51 FAIL (layer B window mismatches, D/E edge strikes) + JSON layers/fixtures; `validation/2026-08-13/layer_fx.json`.
- `parity/`, `parity5/` — TV export `tv_logs.{txt,json}`, per-contract dumps, `compare_report{,2,3}.txt`, `signal_diff*.txt` (final 98.7% from 26 Aug).
- `ump_23900ce_z2.json`.

### scripts/ (see code inventory §1 for the full table)
Launchers (`start-all.ps1` always layers the replay override; `bootstrap.ps1`; `start-backend/frontend/timescale.ps1`), VPS ops (`deploy.sh`, `harden-vps.sh`, `install-sentinel.sh`, `oi-sentinel.sh`, `oi-doctor.sh`, `backup-db.sh`, `nightly_topup_vps.sh`, `systemd/*`), data tools (`truedata_backfill.py` 1,111 lines, `repair_archive_tz.py`, `run_*.cmd` detached repair runners, `build_symbols*.py`, `seed_scripmaster.py`, `probe_mcx.py`, `probe_truedata_ws.py`, `verify_option_chain.py`), trading (`lakshmishree_live_order_test.py` — USER-RUN ONLY), QA (`stack_scenario_sweep.py`).

### Other
- `packaging/nifty-oi-windows.spec` — PyInstaller spec (still lists Angel One hidden imports).
- `.claude/` — empty. `data/symbols.json` (230 symbols), `data/nse_holidays.json` (3 fixed dates only).

---

## Part E — Code structure (summary; full detail in `docs/PROJECT-CONTEXT-MAP.md`)

- **Backend** (`backend/app`, 175 files): `main.py` lifespan spawns supervised tasks (session watch, ATM drift, steward, proxy watch, poller, IV/greeks history, algo-orchestrator, algo-stream, data-health, gapfill). `core/config.py` = 100+ settings (table in the code sweep). `ingest/` = XTS `ws_client.py`, TrueData `truedata_feed.py`, relay follower, feed factory, two stewards, budget, symbol controller, pollers, gapfill. `services/` = OI change engine (820 lines), timeseries, option chain, replay, IV/greeks, NSE cross-check. `algo/` (5,663 lines) = config models/store/audit/auth, series builders, orchestrator (1,674), engines (oi_structure, mtf_ratio, mqae, ump/{levels,engine}), broker (xts_interactive, execution), backtest (data, deps, preflight, runner, store), live_stream. `ws/` = oi-stream hub, algo-stream hub, td-relay.
- **Routes**: 69 HTTP + 3 WS (full table in the code sweep output; algo routes under `/api/algo/*` need the admin cookie).
- **Migrations**: linear 0001 → 0013 (`0013_data_health` = quality-aware `live_days` via `oi_day_stats`).
- **Tests**: 33 files / 362 tests + 3 golden fixtures. **Validation**: 17 harness modules.
- **Frontend** (`frontend/src`, 81 files): React 18 + Vite 5 + Tailwind + ECharts + lightweight-charts; tabs OI Change, Charts, Multi-TF, Ratio, Replay, Algo Config, Backtesting + Guide; `LoginGate` → `POST /api/auth/main-login`; `AlgoAdminGate` → `/api/algo/auth/*`.
- **Docker**: timescaledb (pg16, `DB_BIND`), backend (`API_BIND`, `stop_grace_period 45s`, `alembic upgrade head && uvicorn`), frontend (nginx; `FRONTEND_BIND`); network `nifty-oi` pinned subnet; local override pins `RUN_MODE=replay`.
- **Git**: 70 commits; remote `origin` = `orvinus/Option-Chain-OS`; branches `main`, `Combined`, `ayush-bhai-branch` (prod), `ayush-bhai-v2` (current), `backup/pre-main-reset`. Working tree: 31 modified, 91 untracked (all algo + TrueData work uncommitted).
- No TODO/FIXME/HACK markers anywhere in `backend/app` or `frontend/src`.
