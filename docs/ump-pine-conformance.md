# Ultra Master Pro — Pine-script conformance audit

**Audited:** 2026-08-14 (doc refreshed 2026-08-18)
**Authority:** `nifty_oi_level_24.txt` = `nifty_oi_level_24 (1).txt` (byte-identical,
SHA-256 `8C5545621B1748A78EB03C497A68C967E4A397058AA41985BB325392A9296916`, 75,085 bytes,
1,487 lines). The filename says v24; the header doc-block self-describes as v20 —
the file **content** is the authority either way, and it is the exact file the
Python port was built from.
**In-repo copy (added 2026-08-18):** the user re-supplied the script (header
"v20", same document family) and it now lives at
`docs/reference/nifty_ultra_master_pro_v20.pine`, so the ~40 Pine line citations
below are finally re-verifiable inside the repository. (The original download
was never committed; that provenance gap is what this copy closes.)
**Port:** `backend/app/algo/engines/ump/levels.py` + `engine.py`
**Pinned by:** `backend/tests/test_ump_levels.py` (13) + `test_ump_engine.py` (40 —
6 fidelity tests added 2026-08-18 on top of the 34; the earliest count of 27 was stale)

**Verdict: CONFORMANT — zero FORMULA divergences.** Every decision-affecting
Pine formula maps byte-equivalently to the Python engine. The 2026-08-18 deep
re-audit (`docs/ump-pine-comparison-2026-08-18.md` — the authoritative,
block-by-block table) additionally found **behavioral divergences in the
feed/timing layer**: five were fixed that day (session-anchored 1H buckets,
intraday 1H accumulation, equal-close `new5mClose` suppression, missing-minute
close recovery, exit-candle re-entry lockout — D1/D2/D4/D5/D11), three are
conformant under the script's realtime semantics (D3/D6/A2), and the
ERROR-latch family (D7–D10) deliberately follows the script's own documented
contract over its code regressions (item 3 below is one of them).

---

## 1. Section map — Pine → Python → pinning tests

| Pine lines | Section | Python | Pinned by |
|---|---|---|---|
| 42–79 | Inputs (5 groups) | `config_models.py` `UmpParams` (institutional / structural / visual / entry / dashboard) | config-model suite |
| 84–134 | Data feeds + `new5mClose` | `UmpFeeds`; `process_minute` 5m aggregation | `test_trigger_requires_green_confirmed_close` et al. |
| 145–153 | Data-ready gate | `UmpFeeds.loaded` + `levels_ready` latch | `test_levels_ready_gate_blocks_entries` |
| 158–177 | 1H reversal stream (+dedup) | `levels.detect_h1_reversals` | 5 `test_h1_*` tests |
| 182–196 | `f_matrix` 13-pivot (P, R1–R5, S1–S5, **R4x, R5x**) | `levels.pivot_matrix` | `test_pivot_matrix_known_values`, `test_r4x_is_algebraically_r5` |
| 225–395 | `f_buildLevels` (7 stages) | `levels.build_levels` | 7 pipeline tests incl. hand-derived full run |
| 397–500 | Trade-state `var` block | `UmpEngine.__init__` state | all engine tests |
| 502–552 | Level build + freeze gate + `levelsReady` | `_tick` build block | `test_levels_freeze_during_trade` |
| 554–573 | EMA/ATR/RSI/ADX "regime" | **unported — display-only** (see §3) | n/a |
| 714–764 | `f_findZone` SC1/2/3 + origin rule | `_find_zone` + `_zone_of` | 4 trigger tests |
| 766–789 | Step 1 trigger (confirmed green close) | `_tick` Step-1 block | `test_sc1/2/3_trigger_classification`, open-below-Base, green-only |
| 791–947 | Step 2 test candle: guard, timeout, S1A–S3C | `_tick` Step-2 block | 8 sub-scenario tests + guard + timeout |
| 949–1034 | Retest model: ERROR-2 re-assertion, R1, R2 | `_tick` retest blocks | 5 retest tests + `test_same_candle_exit_is_final_no_reassertion_reopen` |
| 1064–1132 | Step 3 head: NB, Q1–Q3, ERROR-1, trail ladder, ERROR-3 | `_tick` Step-3 | ERROR-1/-3 tests, ladder ratchet + one-rung |
| 1134–1167 | System B zone trailing | `_tick` System-B block | `test_system_b_arms_raises_and_exits`, never-for-retest |
| 1169–1313 | Exit chain P1→P2→P2b→P3→P4 | `_tick` exit ladder + `_exit` | 7 exit tests |

Display-only Pine (deliberately unported): live/label drawing (`f_store`'s canvas
side), `barstate.islast` render block, `f_nearest`, `f_pos`, Step-4 guide lines,
dashboard table, key-levels panel, all `show*`/color/offset inputs.

Python-only additions (from the system spec, not Pine): `entry_guard` (§4.2
premium-band check), `reset_direction_state` (§5.3 direction-flip discard),
`UmpTrade` ledger, clock-aligned 5m aggregation from 1-min bars.

## 2. The audited items — verdicts

1. **Feed semantics (L84–95) — VERIFIED.** Pine's D/W feeds use offset `[1..3]`
   with `lookahead_on` — the standard idiom that yields **completed prior bars**
   (non-repainting). `series.build_premium_history` supplies exactly that:
   daily = strictly-prior IST dates, newest first, ≤3; weekly = last completed
   ISO week strictly before the session's week; 1H = completed **session-anchored**
   hourly buckets 09:15–10:15–… (fixed 2026-08-18 — the original clock-hour fold
   was divergence D1), with today's completed hours joining the feed inside the
   engine as they close (fixed the same day — the feed was previously frozen at
   prior days, divergence D2). The
   decision-affecting 1H reversal stream (L158) uses `lookahead_off` with
   `[1]/[2]` offsets — completed bars as well. No look-ahead exists anywhere in
   the decision path.
2. **`eff_low/high = min/max(live, confirmed)` (L817–818) — EQUIVALENT.** The
   "confirmed" (L98) and "live" (L107) 5m securities are the *same call* (no
   offset), so both are the forming candle's running values; `min(x, x) = x`.
   The Python port's single running set is exact. Same reasoning covers the
   Step-1 note (L773–774) and the retest extremes (L977–979).
3. **Freeze-gate rebuild cadence — DELIBERATE, follows the script's contract.**
   Pine's comment (L530–537, "INTRABAR STABILITY") mandates rebuilds *only on
   confirmed 5m closes* and documents why (every-tick rebuilds caused orphaned
   entries on shuffling levels). The code at L542 (`if not em_inTrade`) lost
   that guard — a comment-vs-code regression *inside the authority*. The port
   implements the documented contract: `if not in_trade and (new5m or not
   self.levels)`. This is the only place the port sides with the script's
   stated intent over its literal code, and it is the *safer* reading.
4. **`feedsLoaded` composition (L145–150) — EQUIVALENT.** Pine requires 9
   daily values + 3 weekly + `h1H/h1C` non-na; Python requires 3 daily HLC
   tuples + weekly tuple + ≥1 H1 candle — the same data in different shapes.
   The `h1O/h1L` legs of the 60m feed are dead in Pine beyond this gate.
5. **Retest re-assertion guard (L970) — EQUIVALENT + now pinned.** Pine
   excludes both `em_trailExitFired` and `em_exitFired` same-candle latches;
   every trail exit (P2 L1212, P2b L1243) *also* sets `em_exitFired`, so the
   port's single `_exit_fired` latch subsumes the pair. Additionally both
   sides clear the retest latch on any exit (Pine `em_retestFired := false` in
   every exit branch; Python `_exit()`), making a reopen doubly impossible.
   Pinned by `test_same_candle_exit_is_final_no_reassertion_reopen`.
6. **S3C condition (L935) — EQUIVALENT.** The OR of the live pair and the
   confirmed pair collapses to one condition (same series, item 2).
7. **Recorded exit prices — DOCUMENTED convention.** Pine only *draws labels*
   (anchor price ≠ quoted level is cosmetic there). The port's ledger records
   the actionable price: P1 = `msl` (the stop), P2/P2b = the trail level,
   P3 = `nb` (the target), P4 = the confirmed close that broke Base. These are
   the decision prices handed to execution (which then applies real fills).
8. **`em_lowerMed` / `em_zoneBot` — CONFIRMED display-only.** Written
   throughout, read by no condition in Pine; carried identically in `_Zone`
   for parity.
9. **`new5mClose` (L131–134) — CLAIM OVERTURNED, then FIXED (2026-08-18).**
   The original audit called `minute % 5 == 4` equivalent; the deep re-audit
   found two real gaps: Pine suppresses a close exactly equal to the previous
   confirmed 5m close (D4), and a missing `:x4` minute silently erased the
   candle's confirmed close (D5). Both are now implemented
   (`_confirmed_close_tick` + `_prev_5m_close`) and pinned by
   `test_equal_close_suppresses_new_5m_close` /
   `test_missing_final_minute_still_confirms_the_close`.
10. **Display inputs** (`show*`, colors, offsets, extension bars, dashboard
    positions) are mirrored in `UmpParams` and — since 2026-08-18 — consumed
    by the platform's chart and panel layer exactly as Pine consumes them
    (rendering only, never the engine): show-toggles filter lines/zones/
    markers, `median_color`/width style type-4 rendering, positions place the
    Dashboard/Key-Levels cards. `line_extension_bars`/`label_right_offset_bars`
    remain N/A on the lightweight-charts engine (kept for config parity).
11. **`zonePct` lives in Pine's *Visual* group but is math-affecting** — it
    defines ZT/UM/LM/ZB and System B's Bottom/Mid. In the port it is
    `UmpParams.visual.zone_width_pct` and flows into `_zone_of` and System B.
    Do not mistake it for a display knob.

## 3. The EMA / "Regime & Technical Context" block (L554–573)

`ta.ema(close, 20/50/200)`, ATR, RSI, DMI/ADX and the volume SMA feed
`trendUp/trendDown/sideways/ranging → regimeText/regimeColor`, whose **only**
consumer is the dashboard table cell (L1387). No EMA or regime symbol appears
in `f_findZone`, Step 1, Step 2, the retest model, Step 3, or any exit branch.
The block is chart cosmetics — which is why the "Regime & Technical Context"
card was removed from the platform's UMP dashboard without any loss of engine
fidelity.

## 4. Rollback semantics (the port's hardest fidelity point)

Pine `var` state rolls back at every realtime tick and the bar replays from
the previous committed state. Three encoded consequences (each with a
regression test): the ENTRY candle's `post_high` is the running close
(ERROR-1); the trail/System-B set-or-raise candle's `trail_low`/`sb_trail_low`
is the running close (ERROR-3); System B advances at most one rung per 5m
candle. **Since Pine vl72 (2026-09-02) System A's trail ladder is a one-pass
multi-level jump** — see the addendum below. See `engine.py`'s module docstring
for the full derivation.


## 5. Addendum — Pine vl72 (2026-09-02)

Authority file replaced by the user's `vl72_fixed.txt` (installed as
`docs/reference/nifty_ultra_master_pro_v20.pine`, SHA-256 prefix `ec90de3b5e5adcbb`;
the 2026-08-18 copy is kept beside it as `…_v20_2026-08-18.pine`). Diff vs the prior
authority = 3 changes:

| Pine change | Lines (new) | Port verdict |
|---|---|---|
| 1H reversal feed `lookahead_off → lookahead_on` ("ZONE-CONSISTENCY FIX") | 158–174 | **Already conformant.** The port appends each completed session hour the moment the next hour opens (`engine.py::_accumulate_session_h1` → `levels.py::detect_h1_reversals`), which is exactly the `lookahead_on` visibility. The old D6 "historical lookahead_off lag" artifact is now gone on TV too, so TV replays should match the port more closely. No code change. |
| `em_exitFired` / `em_exitTime` → `varip` + exit-label re-emit | 476–487, 1083–1088, 1237–1380 | **Rendering / rollback only.** The port has no rollback; the exit-candle lockout already latches (`_exit_time == em5_time`, D11) and each exit records one event. No code change. |
| Trail ladder "MULTI-LEVEL JUMP FIX" — one-pass target rung from `em_postHigh`, walks Base→Q1→Q2 (+Q3 Retest) in one candle, one label at the final rung | 1127–1163 | **PORTED.** `engine.py` Step-3 ladder rewritten as the same one-pass; ratchet-only rule, thresholds and the ERROR-3 re-seed unchanged. System B's chain is untouched (Pine left it). Tests: `test_trail_ladder_multi_level_jump_in_one_candle`, `test_trail_multi_jump_is_error3_safe` replace the old one-rung test. |

Consequence: backtests and live UMP behaviour differ from pre-2026-09-02 runs whenever a
single 5m candle crossed two or more Q-levels (the trail now locks the higher rung
immediately). Frozen backtest run configs are unaffected in *config*, but re-running them
reproduces the new ladder — the engine is code, not config.

## 6. 2026-09-02 TradingView parity audit — data-path corrections

Twenty contracts (NIFTY 08-SEP-2026 23700–24150 CE/PE) were diffed stage by
stage against the Pine's own debug export on the user's 5-minute chart. The
rules were already conformant; three feed defects were not:

1. Session end is date-dependent: 15:40 from 2026-08-03 (NSE close change),
   15:30 before — `core.time_utils.session_close_min(date)`. The premium folds
   (`series.py`) and the engine's 1H/daily accumulation use it.
2. Daily and weekly candles carry the exchange's OFFICIAL high/low/close
   (bhavcopy `eod_bars`, source `td_bhavcopy`) — TradingView's D/W bars do.
   Fallback without a bhavcopy row: bar H/L + mean of the last 30 one-minute
   closes. Plumbed as `UmpEngine(official_close=…)` / `PremiumLife.official_close`.
3. Completed days are read from `oi_archive_bars` with real OHLC; the live tick
   table feeds only today and days the archive lacks.

Also: Pine labels "TRAIL SET" only when the trail lands on Base, "TRAIL ↑" for
every higher rung (even from unset) — the engine now does the same.

Result: 79/80 daily+weekly candles exact, 17/20 1H arrays identical, 18/20
level sets identical, 98.7 % of events from 26 Aug aligned with TradingView
(one-evaluation-per-5-minute-bar emulation, TV's Trigger Timeout = 1). Full
report: `docs/ump-tv-parity-report-2026-09-02.md`.

## 7. Addendum — Pine vl73 (2026-09-09)

Authority file replaced by the user's `vl73_fixed (1).txt` (installed as
`docs/reference/nifty_ultra_master_pro_v20.pine`, SHA-256 prefix `380f7a2d4a221524`,
1,559 lines; the vl72 copy is kept beside it as `…_v20_2026-09-02_vl72.pine`,
`ec90de3b5e5adcbb`). The debug-export variant `ump_v20_parity_debug.pine` carries the
same change (`ed732b6e428aa1ff`). Diff vs vl72 = **one line**:

| Pine change | Line (new) | Port verdict |
|---|---|---|
| "GAP-OVER FIX": `em_postHigh := math.max(em_postHigh, math.max(em5_live_high, high))` — adds the chart-native `high` because `request.security("5", high, lookahead_off)` can return the PREVIOUS bar's high for historical/confirmed bars on a same-timeframe chart, missing a gap-up that crossed a Q-level without touching it. | 1123 | **Already conformant — no engine change.** The port has no `request.security` feed: `live_h` is the forming 5-minute candle's true running high built from the 1-minute commits, so `engine.py:648-653` (`post_high = max(post_high, live_h)`) already equals `max(em5_live_high, high)` on every tick after the entry candle. On the entry candle both sides re-seed from the running close (ERROR-1 / Pine `var` rollback, `engine.py:648-649`), where the new `high` term never runs (`na(em_postHigh)` branch). No test change. |

Consequence for parity work: the change alters **TradingView's own** historical
evaluation (it can now raise the trail on a gap-over bar it previously missed), so
the 2026-09-02 TV export (`logs/parity/tv_logs.txt`, produced under vl72) is not a
vl73 export. `docs/ump-tv-parity-report-2026-09-09.md` regenerates the timestamp-level
comparison against that vl72 export with the rebuilt engine dumps; a fresh export
under vl73 (chart Trigger Timeout = 1) is required to verify the TV side of this line.
Tooling: `scripts/parity/{dump,tv_parse,align,report}.py` (pinned by
`backend/tests/test_parity_tools.py`).


## §8 — vl74: the DISCOVERY fix (2026-09-10)

Authority moved to **vl74** = vl73 + the two hunks of the user's
`vl72_discovery_fix.txt` (`docs/reference/nifty_ultra_master_pro_v20.pine`,
SHA-256 prefix `fad703ba158c171f`, 1563 lines; the vl73 copy is archived beside
it as `…_v20_2026-09-09_vl73.pine`, and the supplied source is kept verbatim as
`vl72_discovery_fix_2026-09-10_source.txt`). The debug-export variant carries the
identical two hunks (`f6b1dc4984994443`) so a future TradingView export stays
comparable level for level.

**Why it is a cherry-pick and not an adoption.** The supplied file is built on a
**pre-vl72** ancestor: a normalised diff shows it lacks three fixes the repo
already had — the vl73 GAP-OVER line, the `varip` exit latch (the "first exit
wins" guarantee) and the exit-label re-emit. Taking it wholesale would have
reverted all three. After the cherry-pick, a diff of the authority against the
supplied file contains **zero** `r4x` / `r5x` / `_dIdx` lines (the fix is now
byte-identical on both sides) and only the three kept fixes plus two comment
labels remain — i.e. the authority is a strict superset of what was supplied.

| Pine change | Line (new) | Port |
|---|---|---|
| `f_matrix` gains `r4x = r3 + (r3 - r2)` and `r5x = r4x + (r4x - r3)`, returning 13 members | 203–206 | `levels.pivot_matrix` — transcribed term-for-term (not simplified to `2*r3 - r2`) so float rounding matches TradingView |
| Weekly discovery walks `_dIdx = array.from(1, 2, 3, 4, 5, 11, 12)` instead of `for i = 1 to 5` | 296–298 | `levels.DISCOVERY_IDX` + the staircase loop; ratchet, seeding and the strict `> u_base × (1 + Expansion%)` test unchanged |

**What it actually does.** `r4x = 2·r3 − r2 = h + 3(p − l) = r5` exactly, so
index 11 can never win a rung — it is tested straight after r5 against a ratchet
r5 has just raised. The real addition is `r5x = h + 4(p − l)`, one `(p − l)` step
above R5, admitted only when `d > h/2` (equivalently `c > 0.5h + 2l`) — a wide
weekly bar closing near its high. A side effect the script does not mention:
Pine's bridge pool spreads the whole matrix, so it grows 33 → 39 pivots; the
three R4x duplicates die at the Expansion pass and the three R5x values become
genuine bridge candidates.

Measured on real contracts (2026-09-10, NIFTY 2026-09-15 expiry, six
contract-sides): two changed, both **purely additive** — one new top DISCOVERY
rung each (23400CE ₹958.20 above ₹757.05; 23400PE ₹113.72 above ₹84.98) — and
four identical. No level was removed or reclassified.

Also ported in the same pass: `levels._sort_asc`, a literal transcription of
Pine's `f_sortAsc` exchange sort. `sorted(zip(prices, types))` broke price ties
on the type integer, a field Pine never reads, and the Expansion pass keeps the
first of an equal pair — so a tie could decide which *type* survived, which in
turn drives Retest eligibility. Equal prices are unreachable under today's laws,
so this changes no output; it removes the divergence rather than resting on the
proof staying true.

TV side: the existing export predates this fix, so a fresh export under vl74
(chart Trigger Timeout = 1) is still the outstanding item for timestamp-level
level parity.
