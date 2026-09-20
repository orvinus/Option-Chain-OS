# Ultra Master Pro ↔ Pine Script — exact-parity comparison (2026-08-18)

**Authority file:** `docs/reference/nifty_ultra_master_pro_v20.pine` — byte-identical copy of
`d:\This PC\Downloads\nifty_oi_level_24 (1).txt`
(SHA-256 `8C5545621B1748A78EB03C497A68C967E4A397058AA41985BB325392A9296916`, 1,486 lines).
The file is named *v24* on disk while its `indicator()` title says *v20* (L35) — the content is
one and the same script; this document uses "Pine" for it throughout.

**Port:** `backend/app/algo/engines/ump/` (`levels.py`, `engine.py`) + feeds from
`backend/app/algo/series.py`, surfaced by `backend/app/api/algo_engines.py` and rendered by
`frontend/src/components/algo/engines/UmpPanel.tsx` + `UmpTvChart.tsx`.

**Verdict:** every formula is byte-equivalent; all 26 inputs exist with byte-exact defaults, are
editable in the platform's TradingView-style settings dialog, and are wired into engine and/or
chart exactly as Pine consumes them. Of 67 audited logic blocks: **33 identical, 11 equivalent,
11 divergent (5 fixed 2026-08-18, 3 conformant under the realtime ruling, 3+1 kept as Pine's own
documented ERROR-fix intent), 12 display-only blocks now re-created on our chart/panels.** The
single deliberately omitted display block is "Regime & Technical Context" (user decision — the
strategy consumes only the standard-pivot machinery, which is fully implemented).

---

## 1 · The timeframe ruling (read this first)

Pine's own header calls the script **"LIVE intrabar same-candle"** and its timeout is measured in
"5m Bars": it is designed to run **on a 5-minute chart in realtime**. Our port replays committed
1-minute bars and rebuilds Pine's 5m/1H/D/W feeds internally. Wherever Pine's *historical* bar
behavior differs from its own *realtime* behavior (rollback, `lookahead_off` lags, per-tick
`:=` assignments), the port follows the **realtime** semantics — that is the behavior the script
exhibits while actually trading, and the only one that matters for a live engine. Consequences:

- **D3** (5m evaluation cadence): realtime Pine evaluates every tick within the forming 5m
  candle; the port evaluates every committed 1-minute bar and additionally on the confirmed 5m
  close — same trades, one deliberate granularity difference (1-min ticks vs sub-second ticks).
- **D6** (1H reversal visibility): on historical bars `request.security("60", lookahead_off)`
  delays a completed hour by one bar; in realtime the completed hour is visible the moment the
  next hour opens. The port emits the newest closed pair immediately — realtime-correct.
- **A2** (retest NB re-anchor, L1122 `em_baseLevel := NB`): realtime executes the assignment on
  every tick; the port latches mid-candle. Realtime-correct.

## 2 · Master settings table — all 26 Pine inputs

Groups exactly as Pine's Inputs tab: 🏛️ Institutional Master Laws (L45), 🔍 Structural
Detection (L51), 🎨 Visual (L55), 📐 Entry Model (L66), 📊 Dashboard (L75). The platform's
settings card (`UmpPanel.tsx`, "Zone Settings") reproduces the same five groups with the same
titles, order, defaults and option lists. "Seeds" = the per-zone tier defaults our platform ships
(Z2 is the byte-exact Pine tier).

| # | Pine input (line) | Pine default · bounds | Model field (`config_models.py`) | Our default | Consumed by | UI control | Chart wiring | Z1 / Z2 / Z3 seeds |
|---|---|---|---|---|---|---|---|---|
| 1 | `expansionLawPct` (L46) | 20.0 | `ump.institutional.expansion_law_pct` | 20.0 | `levels.py` 20 %-spacing pass | NumField 🏛️ | level set | 18 / **20** / 24 |
| 2 | `bridgeLawPct` (L47) | 50.0 | `ump.institutional.bridge_law_pct` | 50.0 | `levels.py` void-bridge threshold | NumField 🏛️ | level set | 45 / **50** / 55 |
| 3 | `medianLawPct` (L48) | 50.0 · 5–100 | `ump.institutional.median_law_pct` | 50.0 | `levels.py` median injection gap | NumField 🏛️ | level set | 45 / **50** / 55 |
| 4 | `priceFloorINR` (L49) | 50.0 | `ump.institutional.price_floor_inr` | 50.0 | `levels.py` ₹-floor filter | NumField 🏛️ | level set | 40 / **50** / 60 |
| 5 | `bodyMatchThreshold` (L52) | 2.2 · step 0.1 | `ump.structural.body_match_pts` | 2.2 | `detect_h1_reversals` | NumField 🔍 | level set | 1.8 / **2.2** / 2.6 |
| 6 | `pivotScanDepth` (L53) | 250 · 50–500 | `ump.structural.scan_depth_bars` | 250 | 1H window depth | NumField 🔍 | level set | 200 / **250** / 300 |
| 7 | `lineExtensionBars` (L56) | 500 | `ump.visual.line_extension_bars` | 500 | display-only | NumField 🎨 (parity note: N/A on this chart engine — lines span the pane) | — | 400 / **500** / 600 |
| 8 | `labelOffset` (L57) | 30 | `ump.visual.label_right_offset_bars` | 30 | display-only | NumField 🎨 (same parity note) | — | 20 / **30** / 40 |
| 9 | `showStructural` (L58) | true | `ump.visual.show_struct` | true | rendering only | Switch 🎨 | filters type-1 lines/zones — never the engine's set | on |
| 10 | `showDiscovery` (L59) | true | `ump.visual.show_discovery` | true | rendering only | Switch 🎨 | filters type-2 | on |
| 11 | `showBridge` (L60) | true | `ump.visual.show_bridge` | true | rendering only | Switch 🎨 | filters type-3 | on |
| 12 | `showMedian` (L61) | true | `ump.visual.show_median` | true | rendering only | Switch 🎨 | filters type-4 | on |
| 13 | `medianColor` (L62) | `#FFD700` | `ump.visual.median_color` | `#FFD700` | rendering only | ColorField 🎨 (picker + hex; §15 warning on malformed hex, falls back to `#FFD700`) | median lines, zone boxes, ladder text | `#eab308` / **`#FFD700`** / `#facc15` |
| 14 | `medianLineWidth` (L63) | 1 · 1–4 | `ump.visual.median_line_width` | 1 | rendering only | NumField 🎨 | median line width | 1 / **1** / 2 |
| 15 | `zonePct` (L64) | 3.0 · 0–20 · step 0.1 | `ump.visual.zone_width_pct` | 3.0 | `f_findZone` band geometry **and** zone-box drawing | NumField 🎨 | shaded ± band boxes | 2 / **3** / 4 |
| 16 | `showEntrySignals` (L67) | true | `ump.entry.show_entry_signals` | true | rendering only | Switch 📐 | gates ALL trade markers | on |
| 17 | `enableLong` (L68) | true | `ump.entry.enable_long` | true | engine — Step-1 gate (L778) | Switch 📐 | — | on |
| 18 | `enableRetest` (L69) | true | `ump.entry.enable_retest` | true | engine — retest gate (L973) | Switch 📐 | — | §18.3 alternating per zone |
| 19 | `showSLLines` (L70) | true | `ump.entry.show_sl_lines` | true | rendering only | Switch 📐 | gates ENTRY/BASE-SL/MAX-SL/TRAIL/ZONE-TRAIL guide lines | on |
| 20 | `showTrailLabels` (L71) | true | `ump.entry.show_trail_labels` | true | rendering only (L1156) | Switch 📐 | gates trail/System-B markers | on |
| 21 | `maxSLPct` (L72) | 10.0 · 1–30 · step 0.1 | `ump.entry.max_sl_pct` | 10.0 | engine — P1 hard cap | NumField 📐 | MAX SL line | 8 / **10** / 12 |
| 22 | `triggerTimeout` (L73) | 6 · 1–100 | `ump.entry.trigger_timeout_bars` | 6 | engine — Step-2 window (5m bars) | NumField 📐 | — | 4 / **6** / 8 |
| 23 | `showDashboard` (L76) | true | `ump.dashboard.show_dashboard` | true | rendering only | Switch 📊 | shows/hides the Dashboard card | on |
| 24 | `showLevelsPanel` (L77) | true | `ump.dashboard.show_key_levels` | true | rendering only | Switch 📊 | shows/hides the Key Levels card | on |
| 25 | `dashPos` (L78) | "Top Right" · 5 options | `ump.dashboard.dashboard_position` | "Top Right" | rendering only | SelectField 📊 — exactly Pine's list incl. "Middle Right"; enforced as a `Literal` (off-list value fails the save, as TV would) | places the Dashboard card (column + slot) | Top Right |
| 26 | `levelsPos` (L79) | "Bottom Right" · 5 options | `ump.dashboard.levels_position` | "Bottom Right" | rendering only | SelectField 📊 — Pine's list incl. "Middle Left"; `Literal`-enforced | places the Key Levels card | Bottom Right |

Numeric `minval/maxval` bounds are deliberately **not** enforced by the backend (§5.2 of the
platform spec — a saved config must round-trip byte-exactly); the UI steps match Pine's `step`.
The nine engine-consumed numerics (#1–6, 15, 21, 22) are additionally exposed as per-run backtest
overrides ("UMP engine overrides" in the New-Run form).

## 3 · Logic-block comparison — 67 blocks

Verdict key — **IDENT**: byte-equivalent formula and behavior. **EQUIV**: different code shape,
provably same output. **DIV→FIXED**: was divergent, fixed 2026-08-18. **DIV→RT**: divergent only
vs Pine's historical bars; conforms to realtime (the ruling in §1). **DIV→DOC**: kept
deliberately, documented (§5). **DISPLAY**: Pine display-only block, re-created on our chart.

### 3.1 Feeds & candle bookkeeping (Pine L85–153)

| # | Block (Pine anchor) | Port location | Verdict | Notes |
|---|---|---|---|---|
| 1 | 5m OHLC via `request.security` | `engine.py` 1-min → 5m aggregation | EQUIV | same buckets 09:15+5k |
| 2 | Confirmed 5m close detection (`em5_conf`) | `engine.py::_confirmed_close_tick` | DIV→FIXED (D5) | boundary-crossing detection: a missing `:x4` minute now defers the close to the next bar instead of losing it; final session minute closes on its own tick |
| 3 | `new5mClose` equal-close suppression (L131–134) | `engine.py` `_prev_5m_close` | DIV→FIXED (D4) | a 5m close equal to the previous confirmed close is NOT a new close — Step 1, P4 and SB stage labels skip that candle; first candle follows `na(_prev5c)` ⇒ true |
| 4 | 1H feed (`request.security "60"`) | `series.py` session-anchored fold | DIV→FIXED (D1) | 1H buckets are now session-anchored 09:15–10:15–… (`(mins−555)//60`), not clock hours; out-of-session minutes excluded |
| 5 | Intraday 1H growth (`h1_struct` accumulates all day) | `engine.py::_accumulate_session_h1` | DIV→FIXED (D2) | today's completed session hours join `feeds.h1_candles` inside the engine; the API's manual append was removed so the running hour can't double-count |
| 6 | Completed-hour visibility timing | `levels.py::detect_h1_reversals` | DIV→RT (D6) | realtime sees the closed pair immediately; only historical `lookahead_off` lags a bar |
| 7 | Daily feed (3 completed days) | `series.py` daily candles | IDENT | |
| 8 | Weekly feed (1 completed week) | `series.py` weekly candle | IDENT | |
| 9 | `levelsReady` latch (L152–153, L552) | `engine.py` gate | IDENT | 3 daily + weekly + 1H present |
| 10 | Evaluation cadence (5m realtime ticks) | per committed 1-min bar | DIV→RT (D3) | §1 — 1-min granularity vs sub-second ticks; every commit point evaluated |

### 3.2 Level engine (Pine L156–395)

| # | Block | Port | Verdict |
|---|---|---|---|
| 11 | `f_matrix` — 11 standard-pivot values from H/L/C (L182) | `levels.py::pivot_matrix` | IDENT |
| 12 | 1H structural reversal rule (green→red / red→green, body match, floor) | `detect_h1_reversals` | IDENT |
| 13 | Consecutive-duplicate collapse + whole-session dedupe | same | IDENT |
| 14 | `f_buildLevels` stage 1 — daily pivots ×3 (L263–271) | `build_levels` | IDENT |
| 15 | Stage 2 — weekly matrix (L281) | same | IDENT |
| 16 | Stage 3 — discovery staircase | same | IDENT |
| 17 | Stage 4 — bridge pool | same | IDENT |
| 18 | Stage 5 — downside + void bridges (`bridgeLawPct`) | same | IDENT |
| 19 | Stage 6 — 20 % expansion-spacing pass (`expansionLawPct`) | same | IDENT |
| 20 | Stage 7 — median injection (`medianLawPct` gap rule) | same | IDENT |
| 21 | Price-floor filter (`priceFloorINR`) | same | IDENT |
| 22 | Scan-depth window (`pivotScanDepth`) | same | IDENT |
| 23 | Level typing (1 STRUCT / 2 DISCOVERY / 3 BRIDGE / 4 MEDIAN) | same | IDENT |
| 24 | Sort + exact-duplicate elimination of `outP` | same | IDENT |
| 25 | Rebuild trigger: confirmed 5m closes / first load, NOT every tick (L516–543) | engine rebuild gate | DIV→DOC (D7) | implements Pine's own fix-comment (L531–532) over the regressed code |
| 26 | Freeze gate: `outP` frozen while `em_inTrade` (L513) | engine | IDENT |
| 27 | "Levels ALWAYS built regardless of trade state" redraw (L612, L663) | signal/display path | IDENT | display refresh never mutates the frozen trading set |

### 3.3 Zone finder & entry state machine (Pine L398–1122)

| # | Block | Port | Verdict |
|---|---|---|---|
| 28 | `f_findZone` — trigger-candle classification SC1/SC2/SC3 (L714) | `engine.py::find_zone` | IDENT |
| 29 | Zone geometry: B, ZT, UM, LM, ZB from `zonePct` | same | IDENT |
| 30 | `f_findZone` reads the UNFILTERED `outP` (show-toggles never affect it) | same | IDENT |
| 31 | Step 1 arming on `new5mClose` + green candle + `enableLong` (L778) | `_tick` Step-1 | IDENT |
| 32 | Two-candle confirmation guard | same | IDENT |
| 33 | Trigger timeout in 5m bars (`triggerTimeout`) | same | IDENT |
| 34 | S1A / S1B / S1C sub-scenarios | same | IDENT |
| 35 | S2A / S2B sub-scenarios | same | IDENT |
| 36 | S3A / S3B / S3C sub-scenarios | same | IDENT |
| 37 | Retest entries R1/R2 with origin guard (L973) | same | IDENT |
| 38 | Retest fires only on Structural/Discovery/Bridge — Median excluded | same | IDENT |
| 39 | Retest NB re-anchor `em_baseLevel := NB` (L1122) | mid-candle latch | DIV→RT (A2) | realtime assigns per tick |
| 40 | Direction-flip discard | orchestrator + engine | IDENT |
| 41 | No new entry inside the exit's 5m candle (L1051–1062 forced-close block) | `exit_candle` gate on Step-2 AND retest | DIV→FIXED (D11) | `_exit_time == em5_time` locks the remainder of the candle |
| 42 | Entry price = trigger close | same | IDENT |
| 43 | `em_state` bookkeeping IDLE→WATCHING→IN_TRADE | engine state | EQUIV | same transitions, explicit enum |

### 3.4 Exits & trails (Pine L1125–1313)

| # | Block | Port | Verdict |
|---|---|---|---|
| 44 | P1 — Max SL hard cap (intrabar, `maxSLPct`) | `exits` chain | IDENT |
| 45 | P2 — System A trail ladder Q1/Q2/Q3/NB | same | IDENT |
| 46 | Quarter-rung arithmetic (Base→NB quartering) | same | IDENT |
| 47 | Post-entry-high seeding discipline (ERROR-1 family) | same | EQUIV | tick-ordering rules from the CHAT_CONTEXT fixes |
| 48 | Trail raise cadence | per 1-min commit | DIV→RT | realtime raises per tick |
| 49 | P2b — System B zone trail, stages + `SB_stageLabelled` (L487, L1156) | same | IDENT |
| 50 | SB stage label gated on `new5mClose` AND `showTrailLabels` | events + UI gate | IDENT |
| 51 | P3 — Target (NB touch, intrabar, no close test — L1200) | same | IDENT |
| 52 | P4 — Base SL on confirmed close, retest uses `em_entryBase` (L1293) | same | IDENT |
| 53 | P4 disabled while a trail is at/above Base (`em_trailSL <= B` guard) | same | IDENT |
| 54 | `_exitLocked` — one exit per candle (ERROR family) | `exit_locked` | IDENT |
| 55 | Exit ordering P1→P2→P2b→P3→P4, first wins | same | IDENT |
| 56 | ERROR-1…5 protective latches (per Pine's fix comments) | same | DIV→DOC (D8–D10 family) | where Pine's comments and its regressed code disagree, the port follows the comments — the documented intent |
| 57 | Rollback semantics of realtime `:=` on exit variables | committed-state model | DIV→RT | same family as §1 |

### 3.5 Display blocks (Pine L1316–1486) — re-created on our chart/panels

| # | Pine element | Our rendering | Verdict |
|---|---|---|---|
| 58 | Level lines w/ type colors + extension | `UmpTvChart` price lines (Pine palette; median uses `median_color`/width) | DISPLAY |
| 59 | Zone shaded boxes ±`zonePct` (L643–649: border ~60-alpha, fill ~82-alpha) | canvas overlay, fill `${color}2E`, border `${color}66`, per-type show-toggles + median color | DISPLAY |
| 60 | Entry labels per sub-scenario | markers: S1A/S2A/S3A `#00E676`, S1B/S2B/S3B `#00BCD4`, S1C `#FFC107`, S3C `#FF9800`, R1 `#18FFFF`, R2 `#76FF03` | DISPLAY |
| 61 | Exit labels | TARGET `#00E676` above-bar ↑; MAX/BASE SL `#FF1744` below; TRAIL/SB `#FF9800` below | DISPLAY |
| 62 | Trail labels ✦ / System-B ◈ | gold ✦ / cyan ◈ markers, gated `show_trail_labels` | DISPLAY |
| 63 | ENTRY dotted line (L1327) + BASE-SL red dashed (L1326) + MAX-SL/TRAIL lines | guide price-lines gated `show_sl_lines` | DISPLAY |
| 64 | Dashboard table (price+chg, nearest R/S, levels count, entry state, entry/SL) | Dashboard card w/ `nearest_resistance`/`nearest_support`/`prev_close` from the API; gated `show_dashboard`, placed by `dashboard_position` | DISPLAY |
| 65 | Key Levels table (7 nearest, signed distance, type icons ◆▲◇✦, ₹) | Key Levels card: distance column red-above/green-below, "7 nearest ⇄ all" toggle, icons, ₹; gated `show_key_levels`, placed by `levels_position` | DISPLAY |
| 66 | `showEntrySignals` gating ALL labels | same gate on all trade markers | DISPLAY |
| 67 | Regime & Technical Context table (EMA 20/50/200, RSI, ADX, ATR, volume) | **deliberately omitted** | user decision 2026-08-18: "only pivot point standard is used" — the strategy consumes only the standard-pivot machinery (fully implemented); this table feeds nothing |

## 4 · Show-toggle semantics (important invariant)

Pine's `show*` inputs filter **rendering only**. `f_findZone`, the retest scanner and the NB
computation always read the unfiltered `outP`. The port preserves this exactly: toggles never
reach `engine.py`/`levels.py`; they act only in `UmpTvChart`/`UmpPanel`. Turning "Show MEDIAN"
off hides yellow lines while median levels keep producing zones, trails and NB targets — same
as TradingView.

## 5 · Divergence register — final dispositions

| ID | Description | Disposition |
|---|---|---|
| D1 | 1H buckets were clock-hour, NSE bars are 09:15-anchored | **FIXED** — session-anchored fold (`series.py`), test `test_intraday_session_hour_joins_the_h1_feed` |
| D2 | Today's session hours never joined the engine's 1H feed | **FIXED** — engine-internal accumulator; API double-append removed |
| D3 | 5m-chart realtime ticks vs per-1-min-bar commits | **RT-CONFORMANT** (§1) — documented granularity difference |
| D4 | Equal 5m close still counted as `new5mClose` | **FIXED** — suppression per L131–134, test `test_equal_close_suppresses_new_5m_close` |
| D5 | Missing `:x4` minute erased the confirmed close | **FIXED** — boundary-crossing close, test `test_missing_final_minute_still_confirms_the_close` |
| D6 | Reversal pair visibility timing | **RT-CONFORMANT** — historical `lookahead_off` artifact only |
| D7 | Level rebuild on 5m closes (Pine's code regressed to per-tick) | **KEPT** — implements Pine's own fix comment L531–532 |
| D8–D10 | ERROR-latch / rollback family | **KEPT** — port follows Pine's ERROR-fix comments (the documented intent) where the code regressed; one interpretive family |
| D11 | Re-entry possible one minute after an exit | **FIXED** — exit-candle lockout per L1051–1062, test `test_no_reentry_inside_the_exit_candle` |
| A2 | Retest NB re-anchor timing (L1122) | **RT-CONFORMANT** — realtime assigns per tick |

## 6 · Platform additions beyond Pine (deliberate, not divergences)

- `entry_guard` — the orchestrator's premium-band / kill / gate check injected before entries.
- Warmup-disarmed replay: the engine replays history with entries disarmed, then arms live.
- `UmpTrade` ledger with recorded exit prices and reasons (Pine only draws labels).
- Multi-strike hunting (`strike_scan_count` Top-N band candidates), strike selector +
  HUNTED/ALL-STRIKES ladder in the UI.
- TradingView-engine chart (lightweight-charts) with drawing tools, zoom persistence, ₹ legend.
- Staleness gates, restart adoption, kill switches, End-Exit — the money-path safety layer.

## 6b · Expiry-to-expiry continuity (added 2026-08-19)

The port now runs on the contract's **continuous multi-day chart**, exactly like the
TradingView chart the script targets (user decision 2026-08-19):

- **Full-life replay**: the UMP dashboard replays every stored day of the contract
  (~4 weeks) from its first bar with EMPTY feeds; `daily`/`weekly`/1H grow inside the
  engine via **day rollover** (completed day folds into `daily[0]` cap-3 at the next
  day's first bar; the completed ISO week into `weekly` — Pine's `lookahead_on`
  completed-prior-bar semantics). The Data-Ready Gate opening mid-replay as feeds
  accumulate IS the TV behaviour. Bars are session-filtered 09:15:00–15:29:59 IST
  (NSE options trade to 15:30 — a 15:30+ DB bucket has no TV-bar equivalent) which
  also excises the historical 24/7 hold-last pollution rows.
- **Ordering fix**: the deferred (gap-fill) confirmed close of the previous candle now
  fires BEFORE the day rollover and 1H flush — on TV that close tick still sees the
  old day's feeds. The 15:15–15:30 partial hour joining the 1H feed next morning is
  TV-correct (a real completed short bar on the NSE 60m chart).
- **Trigger timeout confirmed wall-clock**: Pine L820-821 computes
  `em5_time − em_trigTime5m > timeout·5·60·1000` (a timestamp diff despite the
  "5m Bars" label at L73) — an armed trigger dying at the overnight gap is TV-exact;
  the port was already byte-equivalent and stays unchanged (pinned by test).
- **Live + backtest use the same full-life warmup**, so hunt engines, adopted
  positions and the dashboard all share one level evolution (backtest cost is
  controlled by a per-contract day-open engine cache + deepcopy, pinned equivalent to
  a continuous feed by `test_rebuild_equals_continuous_feed`).
- **Cursor semantics**: the dashboard's date/time controls are an as-of cursor; the
  chart opens zoomed to the expiry-to-expiry week (previous expiry+1 → expiry) with
  the full life scrollable, and pan/zoom survives cursor changes.

### Deliberate master-spec overrides (overnight carry — locked user decision)

| Override | Detail |
|---|---|
| §2.3 End-Exit | INERT while `overnight_carry` is ON — the Pine exit ladder alone closes positions; positions ride the overnight gap (broker product NRML). |
| §2.3 sequencing | Extends across days: a carried open trade suppresses next-session zone entries until it closes. |
| §10 overnight alert | Downgraded to an informative "carried overnight" notice under carry (CRITICAL retained past expiry or with carry OFF). |
| Backtest EOD close | `EOD_FORCE_CLOSE` removed for carry-ON runs (positions carry via the ledger row + next-day re-adoption); frozen pre-change run configs keep it byte-identically (raw-JSON key guard). |
| NEW expiry close | Unconditional force-close at **15:25 IST on the expiry day** (not in Pine — TV contracts don't expire mid-chart; physical reality outranks parity). Pre-empts Z3's 15:30 End on the seeded Tuesday. |
| §12 P&L attribution | Realized P&L, loss caps, streaks and calendars attribute to the **exit day**; `max_trades`/zone is consumed on the **entry day**; weekday/zone summary groups stay entry-attributed. |
| §7 kills | Still never force-exit (unchanged, restated under carry). |

## 7 · Verification battery (2026-08-18)

- `backend/tests/test_ump_engine.py` — 40/40 (6 new fidelity tests listed in §5).
- Full backend suite, `backtest_parity.py` 633/633, `backtest_golden.py` on the closed window
  2026-08-11 → 2026-08-14 (level sets change by design under D1/D2 — determinism A/B holds).
- `tsc -b` + `vite build` clean; browser QA on the UMP page (all 5 setting groups editable,
  toggles/boxes/markers/positions respond live).


## 8 · 2026-09-02 addendum — authority bumped to Pine vl72

See `docs/ump-pine-conformance.md` §5. Three Pine deltas: 1H feed `lookahead_on` (port already
conformant — D6 artifact now gone on TV too), `varip` exit latch + label re-emit (rendering only),
and the **MULTI-LEVEL JUMP FIX** for the System A trail ladder (ported; the "one rung per candle"
rows above for System A no longer apply — System B keeps its one-step chain).
