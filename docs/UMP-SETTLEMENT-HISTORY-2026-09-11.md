# Daily and weekly history for Ultra Master Pro — NSE settlement bars

**2026-09-11** · builds on `docs/UMP-DISCOVERY-FIX-2026-09-10.md` (vl74).
Local only: nothing committed, pushed, or sent to the VPS.

---

## 1. The problem, in one picture

The user sent two TradingView screenshots of the same contract, NIFTY 23450 CE
expiring 2026-09-15:

- the **4-hour** chart begins **Thu 3 Sep 09:15** — exactly where our data begins;
- the **weekly** chart shows candles for **w/c 17, 24, 31 Aug and 7 Sep**.

TradingView's daily and weekly series carry a bar for every day a contract was
**listed**, not only the days it **traded**. Pine's UMP script reads precisely
that series:

```pine
[wH, wL, wC]    = request.security(tickerid, "W", [high[1], low[1], close[1]], lookahead_on)
[d1H, d1L, d1C] = request.security(tickerid, "D", [high[1], low[1], close[1]], lookahead_on)
```

Our engine had no such series. It folds daily and weekly candles out of the
1-minute bars it replays, so for 23450 CE it saw nothing before 3 Sep. Two
consequences:

1. **The Data-Ready Gate opened days late.** It needs 3 completed dailies plus a
   completed ISO week; folding from intraday means a contract's whole first week
   is dead. Already a measured, named divergence in our own parity work —
   `first-week-divergence`, tagged on 8 of 20 contracts.
2. **The levels were built from the wrong candles.** DISCOVERY comes from exactly
   one weekly pivot matrix and BRIDGE from exactly three daily matrices. Wrong
   candles in, wrong ladder out.

---

## 2. Where the missing bars come from

**TrueData: no.** Probed 2026-09-11. `get_bars(interval="EOD")` for
`NIFTY26091523450CE` returned 7 rows starting 2026-09-03 — identical to its
1-minute feed. 23500 CE returned 12 rows from 27 Aug, the monthly 23650 CE 18
rows from 3 Aug with gaps. The vendor serves traded days only.

**NSE: yes, decisively.** The UDiFF F&O bhavcopy at

```
https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{YYYYMMDD}_F_0000.csv.zip
```

returns HTTP 200 (~1 MB, ~34k rows) after a homepage cookie warm-up, and it
carries every listed contract including the zero-volume ones. It is public and
unauthenticated — there are no credentials in this path and none may be added.

### The reconstruction rule, and the trap in it

```
traded day   (TtlTradgVol > 0):  O/H/L/C = OpnPric/HghPric/LwPric/ClsPric
untraded day (TtlTradgVol == 0): O = H = L = C = SttlmPric
```

`SttlmPric` is load-bearing and **`ClsPric` is a trap**. On an untraded day
`ClsPric` stays frozen at the contract's listing price — 1271.65 on every single
day from 14 Aug to 2 Sep — while `SttlmPric` is repriced daily:

| date | ClsPric | SttlmPric |
|---|---|---|
| 2026-08-19 | 1271.65 | 885.85 |
| 2026-08-26 | 1271.65 | 922.58 |
| 2026-09-02 | 1271.65 | 614.34 |

Building the daily bars from `SttlmPric` and folding them into ISO weeks
reproduced the TradingView candle the user hovered, to the paisa:

| | O | H | L | C |
|---|---|---|---|---|
| ours, w/c 2026-08-31 | 768.57 | 768.57 | 556.00 | 566.25 |
| TradingView, w/c 2026-08-31 | 768.57 | 768.57 | 556.00 | 566.25 |

**Independent cross-check.** On the four days both sources cover (3, 4, 7, 8
Sep) the NSE rows and the existing TrueData bhavcopy rows agree exactly on open,
high, low, close **and** volume — after converting NSE's contract count with the
row's own `NewBrdLotQty`, since the bhavcopy counts shares.

---

## 3. What changed

### Acquisition
- **`backend/app/market_data/nse_settlement.py`** (new) — fetch and parse, pure
  `parse_udiff` separated from the network so it is testable without one.
  Columns read **by header name only**; NSE has reordered this file before.
- **`scripts/nse_settlement_backfill.py`** (new) — writes `eod_bars` under
  `source='nse_settlement'` with a `nse:stl:` token namespace, distinct from the
  bhavcopy's `td:bhav:`, so the two coexist on the `(trade_date, token)` primary
  key and **no exchange-official row is ever overwritten**. Idempotent by
  row-presence, not by a ledger, so it can never wedge in a "done" state.

### Reading
- **`_OFFICIAL_CLOSES_SQL`** now accepts both sources with `DISTINCT ON`,
  ordered so **bhavcopy wins** on any date present in both, and returns `open`
  as well. `OfficialDay` gained `open` as a fourth member, **appended** so the
  legacy 3-tuple and bare-close forms still unpack everywhere.

### The engine seed
- **`HtfSeed` + `UmpEngine.seed_htf`** install the D/W context that exists at the
  open of the first replayed bar: the 3 newest prior sessions, the last completed
  ISO week, and the part of the running ISO week that precedes it.
- **`series.htf_seed_from_official`** builds it, anchored **strictly before** the
  first replayed bar — no look-ahead, which the golden resume sentinel enforces.
- Wired into `orchestrator.warmup_ump_engine_full_life` (live and backtest) and
  `api.algo_engines.replay_ump` (the UMP page), so all three agree.
- **Pine's cap of 3 dailies and one weekly is untouched**, and `_roll_htf` is
  unchanged — it extends a seeded feed exactly as it grows an empty one.
- An unseeded path remains, so `test_gate_opens_mid_replay_from_empty_feeds`
  stays meaningful.

The running-week carry-in matters more than it looks. For a contract that first
trades on a Thursday, the week's real high lives in the **Monday settlement
bar**; without it the week publishes 593.05 instead of 768.57 when it rolls.

### The chart
`series.daily_series_minutes` builds the DAY and WEEK charts from the exchange's
own daily record, with the **running session still folded from its own minutes**
(using an official row there would draw a full day when the cursor is
mid-session — look-ahead, visible on the chart). This fixed a second, separate
bug: a completed day's chart candle used to close at the last 1-minute print
rather than the official close (4 Sep on 23450 CE: 561.10 drawn against 566.25
official), so the chart disagreed with the levels drawn on it.

Intraday intervals are deliberately untouched — TradingView's 4-hour chart for
this contract starts 3 Sep and so must ours. The response carries
`candles_source: "minutes+settlement"` and names the settlement dates, and the
chart header shows the note, so the origin is stated rather than implied.

### Freshness
The importer runs in the boot gap-fill pass (`SETTLEMENT_BACKFILL_ON_BOOT`,
default on, 75 days). It runs **first and unconditionally**, because its
preconditions differ from everything else there: the archive needs no vendor
credentials, and the rows it fills are days with no minute bars at all, which a
minute-gap scan can never detect. Failures are recorded and never block the
vendor pass.

The pass is **NSE-only**, and that restriction is load-bearing rather than
cosmetic. This is the NSE F&O file, so a BSE underlying (SENSEX) is simply not
in it — and because nothing is ever written for it, the importer's
"already present" check can never short-circuit. Left unrestricted it
re-downloaded the whole 55-file archive window on every boot to find nothing.
Caught by watching the first real boot run, not by a test.

### One free win
`deps.py` called the single-contract `fetch_official_closes` once per contract
per day while the batched form already existed. Now prefetched once per day
alongside the premium warm: ~4,000 queries → 145 over a 145-day run.

---

## 4. Data imported

| source | rows | first | last |
|---|---|---|---|
| nse_settlement | 303,391 | 2026-01-01 | 2026-09-10 |
| td_bhavcopy | 149,532 | 2026-02-09 | 2026-09-08 |

40,312 of the July–September rows are settlement-only. Across the whole import,
roughly half of all listed contract-days never traded — that is the size of the
hole. The 10 weekdays with no file are all NSE holidays.

This also closed a side issue the plan flagged: `eod_bars` was two sessions
behind (last bhavcopy day 2026-09-08) because `pull_eod` never re-opens a ledger
unit marked done. The settlement import covers 9 and 10 Sep. **The `pull_eod`
ledger defect itself is still open** — not fixed here, just routed around.

---

## 5. Verification

| check | result |
|---|---|
| Unit suite | **523 passed** (was 495; 28 new) |
| `validation/backtest_parity.py` | **633/633** |
| `validation/backtest_golden.py 2026-09-01 2026-09-03` | **PASS**, both halves |
| `scripts/stack_scenario_sweep.py` | **180/180** |
| Browser, Algo Config + Backtesting | no problems, no JS errors |

### The screenshot was the acceptance test

For 23450 CE, from the running stack:

- **1h** — 37 candles, first `2026-09-03T10:15`, source `minutes`. Unchanged.
- **1d** — 23 candles, first `2026-08-12`, source `minutes+settlement`.
- **1W** — 5 candles at w/c 10, 17, 24, 31 Aug and 7 Sep.

w/c 31 Aug reads **O 768.57 H 768.57 L 556.00 C 566.25** — the TradingView
readout. The Data-Ready Gate reads `Daily feed (3/3) · Weekly feed · 1H feed
(36) · GATE OPEN`, and DISCOVERY rungs are drawn at 917.12 and 704.55.

### Controlled A/B, identical feeds, seed off vs on

14 contracts. The gate opens **earlier on 13 of 14**, often by days:

| contract | gate, seed off | gate, seed on |
|---|---|---|
| 23450 CE | 08 Sep 09:15 | 04 Sep 12:16 |
| 23450 PE | 07 Sep 09:15 | 02 Sep 10:15 |
| 23500 PE | 24 Aug 09:15 | 18 Aug 12:22 |
| 23600 PE | 07 Sep 09:15 | 31 Aug 11:15 |

End-of-life ladders are identical on 13 of 14 (the feeds cap at 3 dailies and
one weekly, so a mature contract converges). The exception is 23450 CE, whose
weekly high moves 593.05 → 768.57 and adds DISCOVERY rungs at 560.53, 699.41,
840.28 and 1055.40 — exactly the staircase the user asked to have aligned.

### Pine parity, same code, same DB, same pinned cut

Measured against the stored TradingView export with only the seed switched:

| | seed off | seed on |
|---|---|---|
| matched (bar+kind) | 1261 (90.3% of TV) | **1365 (97.8%)** |
| exact (bar+kind+value) | 1232 | **1341** |
| TV-only events | 135 | **31** |
| platform-only | 117 | **90** |
| timing ±1 bar | 13 | **6** |
| LABEL / VALUE diffs | 2 / 27 | **1 / 23** |
| `first-week-divergence` | 8/20 | **2/20** |
| daily D1..D3 exact | 60/60 | 60/60 |
| weekly exact | 19/20 | 19/20 |

Every metric improves or holds. Reports:
`docs/ump-tv-parity-report-2026-09-11-noseed.md` and
`docs/ump-tv-parity-report-2026-09-11-settlement.md`.

### Backtest, run #139 against baseline #129

Same window (2026-02-05 → 2026-09-09), same config, same settings, 145 days.

| metric | #129 | #139 | delta |
|---|---|---|---|
| trades | 121 | 121 | 0 |
| wins / losses | 32 / 89 | 32 / 89 | 0 |
| net P&L | −7,698.89 | −7,683.50 | +15.39 |
| max drawdown | 13,652.22 | **12,711.94** | −940.28 |
| max drawdown % | 37.97 | 36.29 | −1.68 |
| profit factor | 0.89 | 0.89 | 0 |

Gross win +492.72 and gross loss +477.33 with an unchanged trade count means
individual entries and exits moved on a few days while the run's shape held —
which is what an earlier-opening gate and corrected levels should do.

*(Run #135 is an earlier partial rerun, taken when only July onward had been
imported. #139 is the complete one.)*

---

## 6. Open item, NOT caused by this change

**TradingView level-set agreement fell from 18/20 to 9/20, and it is vl74's, not
this change's.** The proof is direct: with the seed off and on, the ladder diff
against TradingView is **byte-identical** — same 9/20, same 16 ours-only rungs,
same 8 TV-only rungs. Today's work moves the ladder not at all for these mature
contracts.

Recomputing the pre-vl74 dumps' own stored inputs with today's `build_levels`
reproduces exactly the extra rungs, and they are overwhelmingly type 2
(DISCOVERY) at the top of the ladder — 96.98, 127.07, 147.30, 183.98, 251.27,
747.43, 811.13, 863.05, 1082.45. These are the **R4x/R5x extension rungs vl74
added** on 2026-09-10.

The stored TradingView export was captured on **2026-09-03, before vl74**, so a
chart running the older Pine cannot show rungs the newer Pine invents. Two
readings, and we cannot yet tell them apart:

1. the user's chart now runs vl74 and a **fresh export would match**; or
2. vl74 over-extends the DISCOVERY staircase relative to the real Pine.

**Resolving this needs a fresh TradingView export from the chart running vl74.**
Until then it is an open question, not a verified regression and not a verified
pass.

---

## 7. Honest limits

- A NIFTY **weekly** is listed ~34 calendar days before expiry, so it reaches
  ~4–5 weekly candles — never "two months". Monthlies reach 2–3 months.
  TradingView is bounded identically, so matching it is the achievable goal.
  23450 CE was listed 2026-08-12 and now shows 23 daily and 5 weekly candles.
- A day a contract genuinely did not trade has **no** intraday bars anywhere.
  Settlement gives it a daily O/H/L/C, which is what TradingView plots and what
  Pine reads — but the 1h/5m/1m charts for such a day stay empty, correctly.
- **Interleaved gaps are not handled.** The seed fills the stretch *before* the
  first replayed bar. If a contract trades, goes quiet for a day mid-life, then
  trades again, `_roll_htf` still skips the quiet day while TradingView includes
  it. The approved plan explicitly ruled `_roll_htf` out of scope. The common
  case — a prefix of idle days — is fully covered.
- `SETTLEMENT_BACKFILL_DAYS` is 75. A contract listed longer ago than that seeds
  short until someone widens the window or runs the script with `--from`.

---

## 8. Rollback

All edits are local and uncommitted: revert the working tree, rebuild the two
images, recreate. The new rows are additive and tagged, so
`DELETE FROM eod_bars WHERE source = 'nse_settlement'` removes them entirely.
No schema migration was needed.
