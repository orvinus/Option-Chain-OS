# Ultra Master Pro — the DISCOVERY fix (Pine vl74), 2026-09-10

Built and verified locally. Nothing committed, pushed or deployed.
Source supplied by the user: `vl72_discovery_fix.txt` (kept verbatim at
`docs/reference/vl72_discovery_fix_2026-09-10_source.txt`).

## 1. What the supplied file actually contained

A normalised diff (CRLF and encoding folded, so no false hits) against the two
reference scripts showed the file is **not** a descendant of our authority. It
sits on a **pre-vl72** ancestor and is missing three fixes the repo already had:

| Missing from the supplied file | Ours |
|---|---|
| vl73 "GAP-OVER FIX" on `em_postHigh` | kept |
| `varip` exit latch — the ERROR-5 "first exit wins" guarantee | kept |
| exit-label re-emit (5 exit points + the force-close block) | kept |

Adopting it wholesale would have silently reverted all three, so the user chose
to **cherry-pick the DISCOVERY change on top of vl73**. The result is the new
authority, **vl74**.

Entry logic is untouched: `f_findZone`, the Step 1 trigger, the Step 2 test
candle and Retest R1/R2 are byte-identical across all three scripts.

## 2. The change

Two hunks that must ship together (the second indexes what the first creates):

1. `f_matrix` grows 11 → 13 members: `r4x = r3 + (r3 − r2)`, `r5x = r4x + (r4x − r3)`.
2. The weekly DISCOVERY staircase walks `_dIdx = (1, 2, 3, 4, 5, 11, 12)` —
   R1…R5 then the two extensions — instead of `for i = 1 to 5`. The `uBase`
   ratchet, the strict `> uBase × (1 + Expansion%)` test and the level push are
   unchanged.

### The algebra that explains the real effect

| Member | Formula | Effect |
|---|---|---|
| `r4x` | `2·r3 − r2` = `h + 3(p − l)` | **exactly r5** — tested straight after r5 against a ratchet r5 has just raised, so it can never win a rung |
| `r5x` | `2·r4x − r3` = `h + 4(p − l)` | the genuine addition: one `(p − l)` step above R5, admitted only when `d > h/2` (i.e. `c > 0.5h + 2l`) — a wide weekly bar closing near its high |

**Side effect the script does not mention:** Pine's daily bridge pool spreads
the whole matrix, so it grows 33 → 39 pivots. The three R4x duplicates die at
the Expansion pass (a 0% gap fails `≥ 20%`); the three R5x values become real
candidates for downside and scavenger bridges. Ported faithfully — the goal is
that our ladder equals the TradingView chart, not that it is tidier.

## 3. Code changed

| File | Change |
|---|---|
| `backend/app/algo/engines/ump/levels.py` | `pivot_matrix` returns 13 members (written term-for-term as Pine writes them, not simplified, so float rounding matches); new `DISCOVERY_IDX`; the staircase walks it; bridge-pool comment 33 → 39 |
| same | `_sort_asc` — a literal port of Pine's `f_sortAsc` exchange sort, replacing `sorted(zip(...))` at both sort sites |
| `backend/app/algo/engines/ump/__init__.py` | docstring 11 → 13 |
| `docs/reference/nifty_ultra_master_pro_v20.pine` | now vl74 (vl73 + the two hunks); vl73 archived as `…_2026-09-09_vl73.pine` |
| `docs/reference/ump_v20_parity_debug.pine` | the identical two hunks, so a future TV export stays comparable |
| `backend/tests/test_ump_levels.py` | expectations re-derived by hand; six new tests |

### Why `_sort_asc` came along

`sorted(zip(prices, types))` breaks price ties on the type integer — a field
Pine never reads — and the Expansion pass keeps the *first* of an equal pair, so
a tie could decide which **type** survives, which drives Retest eligibility
(MEDIAN is excluded) and the chart colour. Equal prices are unreachable under
today's laws, so this changes no output; it removes the divergence instead of
resting on a proof that this very change shows can move.

## 4. Verified effect on real data

Offline old-vs-new comparison, identical feeds, NIFTY 2026-09-15 expiry, six
contract-sides (2026-09-10):

| Contract | Before | After | Delta |
|---|---|---|---|
| 23400 CE | 10 levels, DISCOVERY ₹757.05 | 11 levels, DISCOVERY ₹757.05 · **₹958.20** | +1 rung |
| 23400 PE | 3 levels, DISCOVERY ₹84.98 | 4 levels, DISCOVERY ₹84.98 · **₹113.72** | +1 rung |
| 23500 CE / PE, 23600 CE / PE | — | — | identical |

**Purely additive: nothing removed, nothing reclassified, no bridge moved.**
The live API confirms it after the rebuild — strike 23400 now returns
DISCOVERY `[757.05, 958.20]`, strike 23500 is unchanged at `[893.67, 1141.00]`.

## 5. Integration

Live and backtest share `build_levels` through `UmpEngine`, and `backtest/deps.py`
imports the orchestrator's own warm-up, so the two cannot drift. Strike selection
is unaffected — strikes come from the premium band and the band guard reads the
running close, never a level. The order path takes a `float raw_price` and an
`int lots`; nothing level-typed crosses into `execute_entry`/`execute_exit`. Level
spacing does set the entry price, the Q-rungs, the System-B floors and the target,
so fills and P&L move — that is the intended effect, not a break.

One consequence worth recording: `ump_capture.levels` snapshots inside backtest
runs created **before** this change were captured under the old pipeline and are
historical artefacts; new runs carry the new ladder.

## 6. Verification

All on the rebuilt, recreated local stack (2026-09-10).

| Check | Result |
|---|---|
| Backend unit suite (throwaway image, all four mounts) | **493 passed** (was 487; +6 level tests) |
| `test_ump_levels.py` | 19 passed — every changed expectation re-derived by hand, arithmetic written into the test comments |
| `validation/backtest_golden.py 2026-09-01 2026-09-03` | **PASS** — same-input and interrupt/resume byte-identical |
| `validation/backtest_parity.py` | **633/633** (data-layer twin check, untouched by levels — proves nothing unrelated broke) |
| Offline level delta, identical feeds | 2026-09-15 expiry: 2 of 6 contract-sides changed, purely additive. 2026-09-08 expiry: 4 of 4 changed (details below) |
| Live API after rebuild | strike 23400 DISCOVERY `[757.05, 958.20]` (the new rung), strike 23500 unchanged `[893.67, 1141.00]` |
| Browser — Algo Config → Ultra Master Pro | Key Levels card: 6 Structural · **1 Discovery** · 2 Bridge · 1 Median; chart renders level lines, zone boxes, Q-rungs, NB target and entry markers; Data-Ready Gate OPEN; no JS errors |
| Browser — Backtesting → Ultra Master Pro | same panel on the sandbox document: 4 Structural · **1 Discovery** · 2 Bridge · 1 Median |
| Backtest before/after, 145 days (#118 → #129, identical frozen config and settings) | trades 117 → **121**, wins 30 → 32, net P&L −₹7,694.76 → −₹7,698.89 (−₹4.13), max drawdown ₹12,511.74 → ₹13,652.22 |
| Stack sweep S1–S7 | see §8 |

### Level delta on the Pine-parity contracts (2026-09-08 expiry, identical feeds)

| Contract | Change |
|---|---|
| 23700 CE | + DISCOVERY ₹1,079.70 |
| 23750 CE | + DISCOVERY ₹971.05 |
| 23750 PE | + DISCOVERY ₹221.07 |
| 23700 PE | + DISCOVERY ₹187.83, + BRIDGE ₹88.62, − MEDIAN ₹90.84 |

The last row is the one non-additive case and it is exactly the predicted
second-order effect: an R5x-derived pivot entered the 39-value pool, became a
bridge, and the gap it filled no longer qualified for a median. Nothing was
reclassified or silently dropped.

**A warning for anyone re-running the parity dumps:** the `logs/parity5_vl73`
dumps were taken on 2026-09-09. Comparing them directly against dumps taken
today shows far larger differences (events 21 → 63 on one contract) — that is
**stored history growing between the two snapshots**, not this fix. The
controlled comparison above rebuilds both ladders from *identical* feeds and is
the only valid measurement of the change.

## 7. Defect found during verification (pre-existing, not from this change)

`GET /api/algo/engines/oi-structure` returns a `by_strike` array added on
2026-09-09 for the per-strike bar chart, and the panel's caption claims its CE/PE
sums equal the engine series' last point. They do not:

| Day | Σ by_strike CE | `call_series_cr[-1]` |
|---|---|---|
| 2026-09-10 (live) | +1.802 Cr | −0.79 Cr |
| 2026-09-09 | +3.006 Cr | +2.680 Cr |
| 2026-09-08 | +2.296 Cr | +0.780 Cr |

It is **not** related to the DISCOVERY fix — different module
(`services/oi_timeseries.fetch_oi_strike_deltas` vs `algo/series.build_oi_change_pair`),
different subsystem, display only, nothing in the order path. It is not caused by
the restored `src=1` rows either: it reproduces on days with none. The sweep
check that catches it is left **failing and renamed** rather than loosened, so it
cannot be forgotten. Next step: derive `by_strike` from the same per-strike CTE
the series is summed from, instead of the parallel query.

## 8. Stack sweep

One sweep assertion added on 2026-09-09 was wrong and is fixed here: it required
the first display candle to be exactly 09:15, but a contract's stored life
usually begins mid-session, so the correct invariant is that the first candle
sits **on** the 09:15 grid (09:15 + k × interval). It passed yesterday only
because that day's auto-picked strike happened to have data from the open.

Result: **179/180**, the single failure being the known `by_strike` defect in §7.
