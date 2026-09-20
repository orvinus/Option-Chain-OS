# Platform ↔ Specification Conformance Audit — 2026-08-18

**Scope:** the entire trading platform audited line-by-line against the four
source documents the user supplied: the master system spec (§1–§19), the OI
Structure Engine spec, the MTF Ratio Master Guide, and the Pine Script
"NIFTY ULTRA-MASTER PRO" (header v20 — saved in-repo at
`docs/reference/nifty_ultra_master_pro_v20.pine`).

**Verdict in one line:** the *calculation engines are clean* — every default,
weight, threshold, tolerance and rule semantic matches the specs exactly, and
the Ultra Master Pro port is conformant to the Pine source with zero math
divergences. The real mistakes were in the *orchestration and plumbing layer*:
17 concrete defects were found, and **all 17 were fixed the same day**, plus
the two user-requested changes (Top-N multi-strike hunting; full SENSEX
support including a live order-routing bug that would have sent SENSEX orders
to the wrong exchange).

---

## Part 1 — The mistakes, step by step (all FIXED 2026-08-18)

### Mistake 1 — The "Strategy Active" switch did nothing (worst finding)
The per-zone Strategy Active toggle existed in the config and the UI, but the
orchestrator **never read it**. A zone you switched OFF would still hunt and
place trades. Spec §2.4 requires it to lock execution while the filter layer
keeps observing.
**Fix:** the orchestrator now evaluates and records the indicators (observation,
per spec) but with the switch OFF discards all hunts, reports state
`strategy_inactive`, and can never enter. Regression-tested. *(All 15 zones in
the live config currently have it ON, so nothing changes for the current
setup — but the switch now actually works.)*

### Mistake 2 — Multi-week P&L percentages were inflated
The blended return summed **one** allocation per weekday *name* while summing
P&L across every *date* — a 4-week range counted Monday's capital once against
four Mondays of P&L, inflating percentages ~4×. Spec §12.2 demands
sum(P&L) ÷ sum(allocated across the same days).
**Fix:** allocation now counts per traded DATE (`app/api/algo_trades.py`); the
one-week default view was always correct and is unchanged.

### Mistake 3 — Live SENSEX orders would go to the WRONG exchange
The broker adapter hardcoded `exchangeSegment: "NSEFO"` for every order.
SENSEX options trade on **BSEFO**.
**Fix:** the segment now derives from the contract's index
(SENSEX/BANKEX → BSEFO, NSE indices → NSEFO), threaded through entry, exit
and the adapter (`broker/xts_interactive.py`, `broker/execution.py`).

### Mistake 4 — Only one strike was ever considered (the user's headline item)
Live and backtest both picked exactly ONE strike (nearest the premium-band
midpoint) and hunted only it. The user's decision: consider many strikes at
once.
**Fix (Top-N multi-strike hunting):** new per-zone setting **`strike_scan_count`
(default 3)** — the N in-band strikes nearest the band midpoint EACH get their
own Ultra Master Pro engine hunting in parallel; the first valid entry wins
(same-minute ties resolve nearest-mid-first, then lowest strike); all other
hunts are discarded on entry — still strictly one position at a time. Applied
identically in live and backtest, exposed in Daily Trading Config (zone row
"Strikes"), in the backtest per-run overrides, and the live status shows
"hunting (N strikes)". `strike_scan_count = 1` reproduces the original
single-strike behaviour bit-for-bit (regression-pinned).

### Mistake 5 — The runtime safety gate implemented 6 of the spec's 8 points
Missing: the Strategy-Active check (now real via Mistake 1) and an End-Exit
guard.
**Fix:** a day with End-Exit OFF now produces a loud §15 warning ("no
end-of-day close — overnight risk"); Strategy Active is enforced at runtime.

### Mistake 6 — Four of the five alert toggles were dead, and only Telegram exists
Only "Telegram trade entry/exit" was ever consulted. Zone/day-kill alerts,
the daily summary, and config-change notifications had switches that did
nothing; email/SMS/push transports don't exist in the codebase.
**Fix:** every toggle now drives a real event through the one wired transport
(Telegram): kill alerts with reason (once per day, toggle-gated), a **15:45
daily summary** (trades, net P&L, per-zone) at session close, config-change
notifications with the changed-field count, and the max-loss breach alert
stays always-on exactly as the spec demands. Email/SMS transports still need
credentials — the report lists that as a pending user decision, and the UI
says so honestly.

### Mistake 7 — Trading a known NSE holiday out of the box
The config's holiday list seeded EMPTY while the platform's own
`data/nse_holidays.json` was never consulted by the trading engine.
**Fix:** fresh configs seed from the platform file, and the live runtime
treats a date as a holiday if EITHER list says so (union) — a stale config
list can never trade a known holiday. Backtests and tests are unaffected
(hook is live-runtime-only, keeping results deterministic).

### Mistake 8 — Paper "latency" and "fill source" were decorative
Both settings were stored and displayed but the simulator ignored them.
**Fix:** `latency_ms` is now REAL in live paper trading — the engine waits it
out (capped 2 s) and re-prices the fill from the contract's freshest tick
before the adverse slippage applies (exactly what real latency does).
Backtests keep deterministic minute-close fills (documented in the panel).
`fill_source = bid/ask mid` cannot be simulated (the feed stores LTP only) —
selecting it now produces an explicit validation warning and the UI labels it
NOT supported.

### Mistake 9 — Roles existed but were never enforced (RBAC, spec §11.2)
`admin`/`editor`/`viewer` roles were stored and displayed, but every
authenticated user could do everything — a viewer could have flipped the
master kill.
**Fix:** viewers are read-only (403 on any mutation); editors may edit config
and run backtests but any save touching a kill switch or the paper/live
routing flag is refused; kill-switch authority, paper-ledger reset, engine
resume and broker test-login are admin-only. Zero visible change today (one
admin user exists) — but the wall is real now.

### Mistake 10 — Runtime events were never audited (spec §11.3)
The audit module's own docstring claimed kills, gate blocks and trade events
were audited; the orchestrator never wrote a single row.
**Fix:** append-only audit rows now record every runtime transition — kill
engaged, safety-gate block, entry fill, exit fill, risk day-kill/profit-lock,
consecutive-loss pause — deduplicated per day, attributed to "engine", visible
in the Security tab's audit table.

### Mistake 11 — The Engines tab's expiry dropdown was hardcoded to NIFTY
Selecting a SENSEX day still listed NIFTY expiries.
**Fix:** the dropdown follows the selected day's `index_symbol` (with the
symbol shown as a badge), and a stale expiry from another symbol's chain
auto-clears.

### Mistake 12 — The Ultra Master Pro page showed a contract the engine might never trade
The dashboard defaulted to the ATM strike; the orchestrator trades the
premium-band pick.
**Fix:** the page now defaults to the band pick, displays a
BAND / MANUAL / ATM-fallback source badge, and offers the full multi-strike
candidate list (the exact set the orchestrator hunts) as a selector — for live
AND archived sessions.

### Mistake 13 — MTF timeframe rows mixed rounding precisions
Trailing deltas were rounded to 2 dp but `full_day` and history-clamped rows
passed through raw — a rule comparing 1m against full_day compared different
precisions.
**Fix:** uniform 2 dp across all rows; golden test extended to pin it.

### Mistake 14 — Exit reasons displayed as raw engine codes
`MAX_SL`, `SB_EXIT` etc. rendered verbatim; spec §12.4 names readable
categories.
**Fix:** all trade tables and chart markers now show labels ("Stop-Loss (Max
cap)", "Trailing Stop", "Zone Trail (System B)", "Target", "Zone End-Exit",
"EOD Force Close"); the raw code stays in the data and CSV (a label column was
added there).

### Mistake 15 — Silent wrong-lot fallback (75)
Three code paths fell back to lot size 75 when the registry failed — NIFTY's
real lot is 65, SENSEX's is 20.
**Fix:** fail-loud everywhere — an unresolvable symbol refuses the entry with
a CRITICAL alert; backtest runs refuse at creation with a clear 400.

### Mistake 16 — The Shadow-Mode twin was incomparable with its live original
The paper twin carried the live trade's lots (correct per §8.5) but divided
its P&L% by the *virtual* balance — different denominator than the live row.
**Fix:** the twin now uses the same allocated-capital basis, so live-vs-paper
comparison is apples-to-apples.

### Mistake 17 — Housekeeping
(a) two orchestrator caches grew forever in a long-lived process — now pruned
at date rollover; (b) the conformance doc claimed 27 UMP engine tests, actual
34 — corrected; (c) the Pine authority file was never in the repo — the
supplied copy now lives at `docs/reference/`; (d) the backtest run-form could
not reach the symbols filter — an index-filter select was added; (e) the
run-form gained a strike-scan override (1/2/3/5) for apples-to-apples
comparisons with historical runs.

### Report-only (documented, deliberately not changed)
- **zone_start cadence** takes its one reading on the first *un-gated* minute
  of the zone (spec-ambiguous; documented).
- **Historical P&L %** is re-scored against the CURRENT config's allocation —
  exact historical basis would need per-day allocation snapshots (known
  limitation).
- **System B zone-trail** is a 5th exit rung inside the P2 trail concept —
  present in the Pine source itself, so it is spec-faithful; the master spec's
  4-rung list simply predates it.
- **`SWEEP_TOLERANCE_CR = 0.05`** stays hardcoded, exactly as the OI Structure
  spec §15 documents ("magic number pending promotion to a setting").
- **Email/SMS transports** need provider credentials before they can exist;
  all alert events are delivered via Telegram meanwhile.
- **The whole `backend/app/algo` package is untracked in git** — nothing has
  ever been committed (standing rule: commits only on explicit request). One
  disk failure loses the engine. Recommend committing soon.

---

## Part 2 — What was NOT a mistake (verified clean)

Every calculation matches its spec — pinned by golden tests generated from the
original reference implementations:

| Engine (slot) | Spec values checked | Verdict |
|---|---|---|
| OI Structure ("OI Change") | left/right bars 3/3, min swing 0.03, EQ tol 0.015, buffer 0.02, weights 15/30/10/20/10, sweep proximity 0.05, threshold 55, tie→Call, 12-route matrix defaults, candles[len−2] red check, EQ-before-HL, EQH/EQL never scored, 5m folded from 1m, both master switches | **all match** |
| MTF Ratio ("Multi-TF") | smaller-absolute side, factor = larger/smaller, Above = ≥ target, Below = < target, Any skips, top-to-bottom first-100%-match with break, per-side +/− sign checks, Neutral passes side filters, zero-Δ fails sign filters, all 10 timeframes | **all match** |
| MQAE ("Ratio") | aggressive/conservative weight rewiring (vel/OB ×3 vs SMC/cross ×3), thresholds 3/6, Yellow inverse, Trend Rider fixed ±1, history from bar 30, per-model kills | **all match** |
| Ultra Master Pro | full conformance audit `docs/ump-pine-conformance.md`: **CONFORMANT, zero math divergences**; 47 pinning tests (13 levels + 34 engine) | **clean** |

**Beyond-spec safety additions** (deliberate engineering, not deviations):
staleness entry gate (180 s), open-position data-stall alarm, restart adoption
of an open trade with deterministic replay, broker-side reconcile every 5
minutes, memory-first position ownership (duplicate-order protection),
overnight-open critical alert, failed-exit retry loop, config lock during
market hours, max-SL-vs-day-cap warning, the experimental direction-hold knob
(default 0 = exact spec).

---

## Part 3 — Which files implement the Pine Script (asked explicitly)

The Pine source (`docs/reference/nifty_ultra_master_pro_v20.pine`; the audited
download's filename said v24, its own header says v20 — same content, both
labels refer to this document) is ported by exactly **two files** — nothing
else contains UMP math:

| Pine section | Python file |
|---|---|
| 1H structural reversal detection + dedup (L158–177) | `backend/app/algo/engines/ump/levels.py` → `detect_h1_reversals` |
| `f_matrix` 11-pivot matrix (L182–194) | `levels.py` → `pivot_matrix` |
| `f_buildLevels` complete 7-stage pipeline — structural spacing, bridge pool, weekly discovery staircase, downside bridges, internal/scavenger bridges, 20 % Expansion pass, recursive Median injection (L225–395) | `levels.py` → `build_levels` |
| `f_findZone` SC1/SC2/SC3 + ±zonePct band geometry (L714–764) | `backend/app/algo/engines/ump/engine.py` → `_zone_of`, `_find_zone` |
| Step 1 trigger (confirmed green 5m close) + Step 2 test candle, two-candle guard, timeout, S1A..S3C (L766–947) | `engine.py` |
| R1/R2 Retest entries incl. the origin rule and MEDIAN exclusion (L949–1034) | `engine.py` |
| Step 3 management: trail Q-ladder, System B zone-trail, exit chain P1 MaxSL → P2 Trail → P2b System B → P3 Target(NB) → P4 Base SL, ERROR-1..5 latches (L1064–1313) | `engine.py` |
| State machine, freeze gate, levelsReady data gate (L397–552) | `engine.py` |
| Regime/EMA dashboard block (L554–573) | **deliberately unported** (display-only; user instruction) |

All nine tunable constants (Expansion 20 %, Bridge 50 %, Median 50 %, floor 50,
body-match 2.2, scan depth 250, zone width 3 %, max SL 10 %, timeout 6) live in
`config_models.py → UmpParams`, editable per zone. Note: the Appendix-A seed
runs the exact Pine defaults on **Z2**; Z1/Z3 use the reviewed tiered values
(18/…/8 %/4 and 24/…/12 %/8) — deliberate and test-pinned. **None of these
calculations were changed in this audit.**

**Overcoming the Pine/prototype limitations** (the specs' own §15 warnings) —
already built into the platform: real TrueData feed instead of synthetic
series; closed-candle discipline enforced at the data layer (the trailing
forming minute is stripped before any engine sees it); config persisted
server-side in atomic versioned rows (not a browser toast); server-side
hashed-password auth + audit instead of client-only HTML; the engine runs in
the backend and the browser only renders. The one §15 item the spec asks to
keep hardcoded-for-now (0.05 Cr sweep tolerance) is kept hardcoded with a
comment saying exactly that.

---

## Part 4 — TrueData data + XTS Interactive orders: does the mix hurt?

**No — with three specifics, all verified:**

1. **Order placement is data-source-agnostic.** The Interactive API takes an
   instrument ID, side and quantity; it neither knows nor cares which feed
   produced the decision. Fills happen at the exchange's price either way.
2. **The one real coupling bug this split created is already fixed** (found in
   the 2026-08-17 audit): resolving the XTS instrument ID used to require an
   XTS *market-data* session, which no longer exists under TrueData — every
   live order would have failed. The sessionless fallback (Zerodha public
   instruments dump, whose exchange token is the same ID space — identity
   proven contract-by-contract) fixed it, and it maps SENSEX (BFO) too.
3. **TrueData is not slower — it leads.** The measured race showed TrueData
   ahead of XTS market data by 0–1 s. Decisions are made on *fresher* data
   than the old feed provided. The residual, unavoidable with ANY feed: the
   decision price and the exchange fill can differ by normal market movement
   in between, and the premium-band gate reads TrueData LTP while the fill
   happens at the exchange — the reconcile loop and fill-confirmation polling
   watch exactly that seam.

---

## Part 5 — SENSEX support (delivered)

- **Backtesting:** set a day's Index to SENSEX in Daily Trading Config — the
  data path was already symbol-parametric (lot 20, strike step 100 from the
  registry; SENSEX bad-data days pre-catalogued). The run form gained an index
  filter (all / NIFTY-only / SENSEX-only days).
- **Ultra Master Pro dashboard:** evaluates SENSEX for any archived session
  (and live once the feed follows SENSEX); expiry dropdown now lists SENSEX
  expiries; band-pick strike + candidates work on the SENSEX premium scale.
- **Live trading:** the BSEFO segment fix (Mistake 3) makes live SENSEX orders
  route correctly. Operational requirement, stated in-app when it bites: on a
  SENSEX trading day the live feed must actually be following SENSEX (the
  strike selector fails safe — `no_strike_in_band` with an explanatory alert —
  when no fresh SENSEX rows exist). Premium bands are per-zone: set them to
  SENSEX's premium scale on SENSEX days.

---

## Part 5½ — ROUND 2: the same-day deep audit (adversarial + performance + ingest)

A second audit ran the same afternoon — three parallel deep passes attacking the
new code adversarially, measuring performance against the live database, and
sweeping the ingest/runtime layers no audit had covered. Everything below was
**fixed the same day**, before the evening deploy.

### The blocker (P0)
**The deployed engine had been crash-looping every minute all session.** The
staleness gate added on 17 Aug imported a name that doesn't exist
(`from ..runtime import rt` — 227 ImportErrors, one per minute since 09:15),
and the loop's catch-all swallowed it silently. Consequence: the engine could
not evaluate, hunt or enter ALL DAY (it failed *safe* — zero trades, and an
open position would still have been managed). **Fix:** the gate is now a
symbol-scoped ~3 ms database probe (it measures the freshness of the exact
chain being traded — the old process-global timestamp could read "fresh" off
another symbol's ticks), wrapped so any failure means "entries blocked",
never a dead pass. Three regression tests pin it.

### Order-state safety (CRITICAL class)
1. **Repeat-BUY hazard:** a transport timeout during order placement used to
   escape all handling — the fired engine stayed in-trade and would re-send
   the BUY every minute. Now: transport failures are typed as UNKNOWN-state;
   an unknown entry **pauses the whole engine** with a critical page (never
   re-orders); an unknown exit retries safely (a duplicate sell is cleanly
   rejected; the reconcile audits the seam); an order placed but unconfirmed
   is **owned** at the decision price instead of discarded.
2. **Stop-loss freeze:** the ledger/balance reads ran before position
   management — a DB outage aborted the pass and froze the stop silently.
   Now guarded: counters degrade, entries block, a page fires, and the open
   position keeps being managed through the outage.
3. **Exit-retry cancellation:** after one refused exit, the position's engine
   could internally re-enter (its entry models were still armed) and repoint
   its trade list at a phantom — the real exit was never retried again. Now:
   a position engine is **disarmed** the moment the position is owned (and on
   restart adoption), and the exit decision is **latched** so retries can
   never lose it.

### Ingest-layer hardening (from the fresh sweep)
- **551 forced feed reconnects in one day** (off-session): the vendor's
  heartbeat frame never matched our key check, so the watchdog tore down a
  healthy socket every ~26 s whenever trades paused — a session risk on any
  mid-day lull. Now ANY decoded frame proves liveness; stale sequence state
  is cleared per connection (it was manufacturing phantom gap counts), and
  the hold-last task no longer leaks one immortal coroutine per reconnect.
- **Unsubscribe was a silent no-op** (internal tokens sent on the wire
  instead of vendor symbols) — every ATM re-centre would have leaked
  subscriptions until the vendor ceiling broke the NEW ATM strikes. Fixed.
- **The subscription budget ran with capacity 0** (the login reply carries no
  ceiling), silently disabling every truncation safeguard. 0 now means
  "unknown → assume the documented trial floor (50) and log an error".
- **Rows were being manufactured around the clock** — the hold-last refresher
  ran overnight and on weekends, writing option rows 24/7 (~3× table bloat,
  polluted session replays). Now gated to market hours.
- The failover poller stamps rows with the **bar's own time** (was `now`,
  dressing up-to-5-minute-old data as current) and carries a missing LTP for
  at most 60 s (was 300).

### Performance (measured, fixed where exactness allowed)
The multi-strike change itself measured **+7 ms/min** steady-state — not the
problem. The problem is pre-existing: most of every decision pass (13.9 s of
~19 s, against a 57 s budget) burns in unbounded scans of the live+archive
union view. What shipped today, all parity-verified byte-identical: the spot
lookup's 7-day floor (**2.4 s → milliseconds**, two-three calls per minute),
lifetime/planning floors on the bounds and premium queries (planning tax
gone; a first live-table-only attempt was 500× faster but the parity harness
caught it changing answers — reverted to exact semantics), the WS hub's
per-subscriber recompute decoupled from the ingest flush path (it could
delay the very freshness timestamps the safety gates read), config-cache TTL
matched to the engine cadence, and panel polling halved. Worst-case pass
drops to roughly 14–15 s of the 57 s budget. The REMAINING cost has a named
root cause — the archive table's column types block predicate pushdown
through the union view, and months of manufactured 24/7 rows (now stopped at
the source) inflate every scan ~3× — and its fix is scheduled after-hours
work: align the archive column types, add a strike-bearing archive index,
purge the off-session rows, and port the backtest's premium cache to the
live path. Each is DDL or behaviour-adjacent and belongs in its own
parity-gated session, not a market-day deploy.

### Governance correction (on my own change)
Multi-strike no longer arms via a silent code default. The model default is
**1** (old behaviour — so old stored documents and frozen backtest configs
can never change meaning on re-parse); the Appendix-A seed ships 3; and the
LIVE config is upgraded to 3 by an explicit, versioned, field-level-audited
save at deploy time. RBAC's kill-authority wall now also covers
`strategy_active`, All-In/allocation %, and End-Exit (an editor could
previously arm 100% deployment or remove the overnight backstop). Paper
latency was scoped to a pure wait (the fill re-price broke §7 sizing and
paper↔backtest parity). Plus ~10 smaller fixes (stale-response guard on the
UMP panel, holiday file hot-reload, audit-row placement, weekend-row P&L
edge, reconcile segment filter for BSEFO, order-id recovery from the order
book, and more — the audit trail in git-diff form is the full list).

**Verification after round 2:** all 29 backend suites green (46 orchestrator
scenarios, 6 new order-state regressions), parity 633/633, golden A/B and
interrupt+resume byte-identical on a closed window, tsc + vite clean.

---

## Part 6 — What the user should know before the next session

1. **Default behaviour changed on purpose:** every zone now hunts the top **3**
   band strikes in parallel (the fix you asked for). Set a zone's "Strikes"
   to 1 to reproduce the old single-strike engine; use the backtest override
   for A/B comparisons. Old benchmark numbers are NOT comparable to new runs
   unless you pin strike-scan = 1 (and the MTF rounding fix can also shift
   results marginally — both changes are corrections).
2. **Telegram credentials are still unset in the local backend** — every alert
   above (and all of last night's safety nets) is silent until
   `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` are added to `.env`.
3. The live config remains **paper_mode OFF / master_kill OFF** (armed) with
   `demat_balance` ₹30,000 vs the real funded ₹1,000 — same standing items as
   yesterday's money-path audit.
4. Nothing is committed to git (standing rule) — say the word and the whole
   algo package gets its first commit.
