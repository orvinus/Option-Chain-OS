# QA Report — Algo Config + Backtesting (2026-09-02)

Scope: every control on the Algo Config page (11 sub-tabs) and the Backtesting page (12 sub-tabs), exercised in Chrome against the local stack (backend rebuilt with the Pine vl72 UMP port), plus API-level negative tests through the signed-in session. Reference for expected behaviour: `backend/app/algo/config_models.py` (§14/§15 rules), `config_store.py`, `api/algo_*.py`, `backtest/*.py`, `docs/spec-conformance-audit-2026-08-18.md`.

State before/after: live config v62 → restored as v68 (byte-identical to v62); sandbox restored to its pre-test document; QA runs 105/106/107 deleted. Versions v63–v67 remain in history as QA artefacts.

## 1. Verdict

Core flows work end to end: draft → diff modal → atomic save → per-field audit → Telegram hook; restore; kill grid; per-day/zone editors; all four engine dashboards (live, historical, time-replay, run-pinned); P&L/notes; paper session; holidays; broker test-login/reconcile/account; config lock; validation; backtest create/queue/cancel/resume/delete; preflight; day replay transport and deep links; run picker; sandbox copy/save; audit.

**Defects found: 4 high, 4 medium, 6 low/usability.** None blocks paper trading; two (H1, H2) can silently corrupt config or hide history and should be fixed before relying on the backtest UI for older dates.

## 2. Defects

### High
- **H1 Stale-draft overwrite after Reset Paper Session.** Reset performs a server-side config save (v64: `session_started` → today) but the page never reloads its saved document. The next save from the same page (I added a holiday) posted the whole stale draft: v65 silently reverted `paper.session_started` back to 2026-08-16 while the confirm modal showed only the holiday change. Audit rows prove it. Root cause: `PaperPanel` gets only `draft/mutate`, no `reload`; and `POST /api/algo/config` has no base-version guard, so any out-of-page change (another tab, restore, reset) is overwritten. Fix: reload after reset, and add `base_version` optimistic-concurrency check on save (409 on mismatch).
- **H2 UMP dashboard / day-replay UMP pane cannot show contracts older than 120 days from today.** `build_premium_life` calls `fetch_premium_minutes(to_ts=now_utc)`; the SQL floors at `to_ts − 120 days`, so an as-of date of 2026-04-16 finds nothing and the API returns 404 "no stored premium data" even though 2,153 archive rows exist for NIFTY 2026-04-21 23850PE. Result: run #106's Apr-16 replay shows "no candles at/before the playhead" and the UMP deep link renders a red error. Backtest execution itself is unaffected (it uses per-day `to_ts`). Fix: anchor the floor on `cut_utc` (the as-of cursor) when given.
- **H3 Preflight skips valid archive days as "live_days matview is stale".** `dup_risk = (live − matview) ∩ arch` predates migration 0013. Any day with stray/partial live rows (Aug 10: 46 rows at 17:05; Aug 14: 89 minutes) and a full archive is now a legitimate archive-won day, but preflight labels it "live/archive duplication risk (refresh live_days)" and excludes it. Fix: derive the risk from `oi_day_stats.winner` (or drop the check) instead of live-row presence.
- **H4 Turning off "Require confirmation on save" bypasses confirmation on that very save.** `requestSave` reads the draft's flag, so the save that flips it posts immediately with no diff modal and an empty note (v66). Fix: decide modal-vs-direct from the *saved* document.

### Medium
- **M1 Integrations shows Telegram "Connected" unconditionally** (`MiscPanels.tsx:205` hardcoded). Container has no token, alerts are silently dropped.
- **M2 Copy-from-live / Copy-into-sandbox reload the page and drop the user at the dashboard login gate** (`window.location.reload()`; the MAIN_USER gate is memory-only). Unsaved sandbox edits are lost without warning.
- **M3 Header version badge goes stale after paper reset** (showed v63 while server was v64); symptom of H1.
- **M4 Deep link from a replay trade does not carry the traded contract.** "Ultra Master Pro ↗" opens with `zone = activeZone` (Z3 at 14:15, trade was Z2) and ATM-fallback strike 24450CE instead of 23850PE. Combined with H2 the user sees an error instead of the trade.

### Low / usability
- L1 `Discard`, holiday `✕ remove`, MTF rule `Delete`, condition `✕`, `Delete run` have no confirmation.
- L2 MTF threshold free-text refuses invalid input silently (red border, never committed, no message).
- L3 Sandbox save discards the note typed in the confirm modal (`_note` unused).
- L4 Extra-excluded-days garbage is filtered silently (`garbage`, `2026-8-1` dropped, only `2026-08-13` sent).
- L5 Backtest P&L tab has no "change run" control once a run is picked.
- L6 MQAE panel shows the *draft* threshold ("±6") next to an evaluation computed from the *saved* config (threshold 3) while edits are unsaved.
- L7 Validation tab renders a false-green "All 15 zones pass" for a pydantic-invalid document (`zone_gate` empty). API-level confirmed; UI path needs a pydantic-invalid draft to reproduce.

## 3. What was verified working (by control)

Daily Trading Config: master kill (draft), overnight carry, live snapshot, 20-dot kill grid (save → v63 → audit row `days.wednesday.zones.Z1.zone_kill false→true`), demat balance, paper toggle, expiry strip, day tabs, copy day (16 changes), reset day to Appendix-A (3 changes, Wed Z2 killed per §18.1 seed), zone editors, End-Exit placement, risk fields + calc note, 5 alert toggles, reeval/hold/strike selects, indicator switches (rule text updates live), copy zone to all days (30 changes), engine deep links (context carried).
Engines: Save All (diff modal), Re-run (fresh GET), Reset zone to defaults, OI-structure matrix/weights, MTF pills/rules/conditions/threshold commit, MQAE mode exclusivity (active mode cannot be switched off; conservative stamps 6) and time-replay slider (re-evaluates at 11:15), UMP show/position/colour/CE-PE/strike/nearest controls, historical evaluation (MQAE Aug-21 PUT verdict).
P&L: paper/live ledgers, month nav, day-cell note open, note save/delete (label flips), zone filter, calendar note markers.
Paper: reset (confirm text correct, session restarted, audit `paper_reset`), shadow toggle, fill-source warning W-10, brokerage calculator recompute.
Holidays: add/save (v65), validation E-14 for bad dates.
Integrations: test login OK, reconcile ("nothing to reconcile"), account refresh (session live, 52-field detail), fee edit dirty.
Security: lock toggles save (v66/v67), username filter ("No audit rows yet."), Refresh, Restore v62 → v68 identical.
Validation: baseline 2 warnings (W-01, W-02), 15/15 zones eligible.
Validation matrix (API, 30 cases): every §15 error/warning string fires as catalogued (E-01…E-15, W-02a…W-10); gate-only problems (negative law, scan depth 0) save cleanly but block the zone; pydantic bounds (scan 11, dsw 0, latency −5) rejected with field paths.
API negatives: 422 invalid save with error list, 422 restore missing version, 404 version get, 400 bad note date, 400 bad/oversized/unparseable run ranges, 400 missing version, 400 unknown index symbol, 409 non-queued create while busy, 400 bad engine day/zone, 422 bad `at`.
Backtesting: new-run form (dates clamped to coverage, preset applies 9 overrides, summary line), preflight table/warnings, start → running → done (run 107), zero-trade banner, entry funnel, equity curve, days table with skip reasons, cancel at day boundary (17/102 kept), resume continues to 102/102 with 14 trades, cancel-again 409, delete-while-running 409, resume-while-running 409, delete run (UI and API), run picker → BACKTEST badge + stats, day replay (restart/step/play-pause 15×/entry-time jump/slider seek, zone strip, signals, alerts feed), sandbox copy from version (audit `backtest_sandbox_copy`, paper pinned), sandbox PUT (changed_fields 35 / no-op 0), invalid sandbox PUT 400.

## 4. Not exercised
- Editor/viewer RBAC and kill-authority 403 (only the admin user exists locally).
- Config Lock during market hours (tested off-hours only; server logic read, not triggered).
- Telegram delivery (no token configured).
- Sandbox "Save Sandbox…" modal and "Clear pin" button in the browser (page reload from M2 ended the browser session; sandbox save verified via API).
- Live-mode paper fills (market closed).
