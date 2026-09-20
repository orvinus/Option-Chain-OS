# Platform audit and changes — 2026-09-09

Scope: the seven-phase plan approved on 2026-09-09 (read-only audit → gap-free
ingestion → one canonical engine path → decision traces → backtest ↔ live gate
parity → QA fixes → Backtesting scope → Pine vl73 parity). Everything was built
and verified locally; nothing was committed, pushed or deployed to the VPS.

Audit evidence and the dependency map: `C:\Users\DHARMIK\.claude\plans\you-are-a-senior-humming-cocke.md`
(Phase 1 section) and `docs/PROJECT-FULL-INVENTORY-2026-09-06.md`.

---

## 1. Verified defects → fixes

| # | Defect (evidence) | Fix | Where |
|---|---|---|---|
| I1 P0 | Boot gap-fill subprocess died on `ModuleNotFoundError: No module named 'app'` in the image (path `/app/backend` does not exist) yet logged `status=ok`. | Dual package-path resolution in the script; `PYTHONPATH` exported by the parent; non-zero exit → `status=error` + page + last 20 lines kept; failed units never marked `done` (script exits 2). | `scripts/truedata_backfill.py:43-49`, `backend/app/ingest/gapfill.py::_run_puller` |
| I2 | Today's session never restored (boot at 11:28 → 09:15–11:28 missing forever). | **Session catch-up**: `pull --day` (nearest 2 expiries, 09:15 → last completed minute, `partial` ledger units) + promotion of restored minutes into `option_oi_snapshots` with `src=1` where no live row exists. Runs at boot, every 5 min in session, and on feed recovery. | `gapfill.py::session_catchup/_promote_restored_minutes`, `truedata_steward.py` (recovery hook), migration 0014 (`src`) |
| I3 | Aggregator overwrote persisted OI with any later tick for an older bucket. | Per-token last-flushed bucket; older buckets dropped (`late_dropped`), equal buckets still allowed (failover poller). | `aggregator.py::_absorb/_flush_closed` |
| I4 | Hold-last window hard-coded `15:35`; no holiday check. | `session_close_min(date)` + `is_nse_holiday`. | `truedata_feed.py::_in_session_window` |
| I5 | No market-hours policy; boot race with the day-index refresh; `gapfill` not cancelled at shutdown. | Refresh first; historical pull deferred to close+20 min in session (unless fresh install); 60 s scheduler; `gapfill` in the cancel list. | `gapfill.py::run_gapfill_loop`, `main.py` |
| I6 | Ledger churn (daily-changing IDX/FUT keys; `eod:*` never refreshed; error units never retried). | Month-aligned window start; reset predicate re-opens `eod:td:*` and `status='error'`; mirrored in the nightly script. | `truedata_backfill.py::_window`, `gapfill.py::_RESET_LEDGER_SQL`, `scripts/nightly_topup_vps.sh` |
| I7 | Today served by both view arms until the 16:00 refresh. | Promotion makes today's winner `live`; premium path reads the archive's true OHLC for promoted minutes (`arch_today` arm). | `series.py::_PREMIUM_MINUTES_TEMPLATE` |
| I8 | No integrity surface. | `services/data_integrity.py` (10 categories) + `GET /api/health/data`, `/api/health/data/integrity`, CLI `integrity`, nightly hook, `data_integrity_runs`. | new |
| C1 | No per-minute, per-candidate decision trace. | `algo_decisions` / `algo_backtest_decisions` (migration 0014); one row per evaluated minute with stage, gates, readings + engine traces, candidates (UMP state, band, fired), sizing, data age, decision + reason; APIs + UI cards (live snapshot, replay). | `orchestrator.py`, `algo/decisions.py`, `backtest/{deps,runner,store}.py`, `api/algo_trades.py`, `api/algo_backtest.py`, `components/algo/DecisionTrace.tsx` |
| C2 | Backtest skipped the 180 s staleness gate and the NSE-holiday union; hard-coded 385 minutes. | `data_age_s` from the frame's last basket tick; holidays frozen into the run (`settings.platform_holidays`) and unioned; per-date session length (`frame.minutes_per_day`); gaps ≥3 min recorded per day and announced. | `backtest/{data,deps,runner,preflight}.py`, `api/algo_backtest.py` |
| C3 | Engine API used a fixed 15:40 window for any date. | `session_close_min(date)`. | `api/algo_engines.py::_window_for_date` |
| C4 | QA H1–H4, M4. | H1 `base_version` → 409 + client reload; H2 premium-life floor anchored on the cursor; H3 preflight dup-risk from `oi_day_stats`; H4 modal decided from the saved doc; M4 deep link carries strike/side. | `api/algo_config.py`, `series.py::build_premium_life`, `preflight.py`, `hooks/useConfigDraft.ts`, `BacktestDayReplay.tsx`, `UmpPanel.tsx` |
| C5 | Backtesting page showed Paper Settings, Holiday Calendar, Integrations & Broker; no settings-used view. | Tabs removed from the Backtesting page only (Algo Config unchanged); New Run "Simulation settings" (slippage, brokerage, record-trace toggle); "Settings used" card; gaps flag in the days table. | `pages/BacktestingPage.tsx`, `components/algo/backtest/BacktestPanel.tsx`, `api/algo_backtest.py::SimSettings/_apply_sim` |
| P1 | Pine vl73 not in repo; parity tools untracked under `logs/`. | vl73 installed as authority (vl72 kept beside it); `scripts/parity/{dump,tv_parse,align,report}.py`; conformance addendum §7. | `docs/reference/`, `docs/ump-pine-conformance.md` |

Pine vl73 vs vl72: one line (L1123, "GAP-OVER FIX") — the port already uses the
forming candle's true running high, so **no engine change**; the line changes
TradingView's own historical evaluation, so the TV side needs a fresh export.

## 2. Behaviour changed (operator-visible)

- Boot now restores the current session and heals multi-day holes for real; failures page.
- New API: `/api/health/data`, `/api/health/data/integrity`, `/api/algo/decisions[/latest]`, `/api/algo/backtest/runs/{id}/decisions[/at]`; day bundle gained `minutes_per_day`, `gaps`, `decisions_index`; `POST /api/algo/config` accepts `base_version` (409 on mismatch); run creation accepts `settings.sim` and `settings.record_decisions`.
- Backtests apply the staleness gate, the platform holiday union and the date's own close → results differ from pre-change runs (see §4).
- Backtesting workspace: Runs · Daily Trading Config · engines · P&L · Security · Validation.

## 3. Data migration / backfill

- Alembic `0014_restore_decisions` (applied locally): `option_oi_snapshots.src`, `data_integrity_runs`, `algo_decisions`, `algo_backtest_decisions`. Downgrade deletes `src=1` rows before dropping the column.
- No backfill required; the first boot after deploy runs the catch-up/historical pull itself. Existing frozen backtest runs keep their stored results; re-running them under the new code reproduces the new gate behaviour.
- VPS: not deployed. Deploy after 15:40 IST; the first boot will run the historical pull (post-close rule).

## 4. Verification

| Check | Result |
|---|---|
| Backend unit suite (in the image) | **406 passed** (was 362; +44 across gapfill, day-mode script, integrity classifiers, aggregator idempotency, hold-last window, decision trace, gate parity, parity tools) |
| Frontend `tsc --noEmit` / `vite build` | clean |
| `validation/backtest_parity.py` | see §4a |
| `validation/backtest_golden.py 2026-09-01 2026-09-03` (now includes the decision rows) | **PASS** same-input + interrupt/resume byte-identical |
| `scripts/stack_scenario_sweep.py` S1–S6 | see §4a |
| Pine parity (`scripts/parity/report.py`, vl72 export, rebuilt dumps at the export cursor) | daily 60/60, weekly 19/20, 1H arrays 17/20, levels 18/20; events 1274/1396 (91.3%), 994/1007 (98.7%) from 26 Aug — identical to the 2026-09-02 figures → engine unchanged by this work. Reports: `docs/ump-tv-parity-report-2026-09-09.md`, `…-from-08-26.md` |
| Image build + migration | backend/frontend rebuilt 17:2x IST; `alembic_version = 0014_restore_decisions`; `truedata_backfill.py validate` runs inside the image (import fixed) |
| Boot gap-fill on the new build | see §4a |

### 4a. Results of the long-running checks

| Check | Result |
|---|---|
| `validation/backtest_parity.py` | **633/633** on the rebuilt image (days 2026-08-31 … 09-02) |
| `scripts/stack_scenario_sweep.py` S1–S6 | **156/156** (12 new S6 checks: `/api/health/data`, integrity 10 categories + 400, live/backtest decisions, anonymous 401, `base_version` 409/200, day bundle `minutes_per_day`/`decisions_index`). Four historical checks in the script were stale — they assumed the "current" expiry traded on 2026-08-13; they now pin the expiry that traded that day. |
| Live decision writer | one controlled `evaluate_minute(14:00)` pass through the real runtime deps → `algo_decisions` row `stage=stale_data · reject · "market data stale — entries blocked" · config v96` (post-close, so the freshness gate fired first — the recorded reason names it) |
| Benchmark rerun (#112 → #118, same frozen config/settings) | 109/112 common days byte-identical; 3 post-2026-08-03 days differ through the staleness gate; 28 previously mis-skipped days now run (H3). 52,850 decision rows recorded; 180 `stale_data` minutes; 4 days with named gaps; 119 days on the 375-minute session. Full table: `docs/backtest-gate-parity-2026-09-09.md` |
| Boot gap-fill on the new build (post-close boot 17:23 IST) | **status ok**: NIFTY missing 09-03, 09-04, 09-08 → `missing_after: []`; SENSEX missing 09-03, 09-04, 09-07, 09-08 → `[]`; `pull` and `pull-eod` exit 0 for both symbols (17:24–17:54 IST) — the first successful in-container pull since the task existed. The final image (holiday file) re-booted at 17:56: `no_gaps`, and the post-close integrity report ran for today + 09-08. |
| Image ships the holiday file | **New defect found during verification**: `/data/nse_holidays.json` was never copied into the image → `known_holidays()` empty in every container (holiday union, gap-fill day grid, hold-last all blind). Fixed in `docker/Dockerfile.backend`; pinned by `tests/test_image_ships_holidays.py`. |

Live config: the sweeps' benign saves moved the version to v98; the v81 content was
restored as v99 (Pydantic re-serialisation only — no field differs).

## 5. Performance impact

- One decision INSERT per evaluated minute live (fire-and-forget task); backtest: one bulk insert per day (~385 rows, ~3 KB each). Golden harness runtime unchanged within noise.
- Session catch-up: ~230 vendor calls at 4 rps (≈1 min) per trigger, throttled to once per 5 min per symbol.
- Historical pull no longer runs during the session (deferred to close+20 min unless the DB is empty).

## 6. Known limitations / unresolved

- TradingView side of vl73 unverified until a fresh export (chart Trigger Timeout = 1) is provided.
- Frontend has no test runner; UI verified by type-check, build, bundle inspection and the API sweep. Adding `vitest` needs a dependency install (not done).
- Same-day catch-up probes NIFTY only in this session (SENSEX has no local live feed); the code path is symbol-generic.
- Untraded-day settlement bars on TV remain unreproducible from vendor bhavcopy (first-week divergences).
- Out-of-order detection is limited to the feed's sequence-gap counter and late-bucket drops (vendor time is not stored in the live table by design).
- The vendor archive contains a **15:40 bar** on every session (e.g. 65 CE/PE rows on 2026-09-01) because the puller's window end is the close INCLUSIVE; that bucket is outside the 09:15–15:39 session and the premium/UMP path already excludes it (`session_last_bar_min`). The integrity report's `tz_session_errors` flags it by design (`rows_outside_session`); readers that sum raw archive rows should treat it as post-close. Not changed here (the archive writer is out of scope; the flag makes it visible).
- Warm-up category: "completed 1H minutes" counts live ∪ archive minutes of the day (fixed during verification — the first cut counted the live table only and reported 0 for archive-only days).

## 7. Rollback

Revert the working tree (nothing is committed), `alembic downgrade 0013_data_health`
(drops the two decision tables, `data_integrity_runs`, deletes `src=1` rows and the
column), rebuild both images, recreate after 15:40 IST.
