# Signal Console — Features 1, 4 and 5

**19 September 2026. Built and verified locally. Nothing committed, pushed or sent to the VPS.**

Features **2** (decision history) and **3** (indicator panel + shared playhead) were explicitly
out of scope and are not touched.

---

## What changed, in plain language

### 1. Combination rule — you can now choose how the indicators vote

**Before:** every switched-on indicator had to agree, always. An indicator that currently had no
direction always blocked the trade. Neither rule could be changed.

**Now**, per zone, per weekday, two dropdowns sit right under the indicator switches:

| Setting | Options | Default |
|---|---|---|
| **Combine** | Unanimous · Majority | Unanimous *(= old behaviour)* |
| **Neutral =** | Blocker · Abstention | Blocker *(= old behaviour)* |

- **Unanimous** — every switched-on indicator with a direction must agree.
- **Majority** — the side with more votes wins; a tie is No Trade.
- **Blocker** — a switched-on indicator with no direction stops the trade.
- **Abstention** — it is ignored, and the others decide.

A switched-off indicator is a third thing entirely: it is never evaluated and never votes. The
spec warns at length about confusing "off" with "no direction" — we were already clear of that
trap, because a disabled indicator is simply absent from the zone's list rather than carrying a
reading. That is now covered by a test so it stays true.

**Checked against the detailed instruction of 19 September.** All sixteen rows of its §13 matrix,
its numbered Examples 1–18, the §11 state table and the §9/§10 empty cases are now executable
tests — **30 cases, all passing**, written in the document's own OFF/NEUTRAL/ABSTAIN/BLOCK
vocabulary so the table can be diffed against the document line by line.

Writing those tests found a real defect: `combine_readings` treated *any* unrecognised rule value
as Majority. A typo, a wrong case, or a config from a future version would have silently **loosened**
the entry filter. Majority is now matched explicitly and unanimity is the fallback, so an unknown
value degrades to the strictest behaviour — the one direction a bug of this kind must never go.

**The important repair:** the rule existed in *two* places — the trading engine and the live
on-screen strip, which had its own copy. The moment Majority became selectable, those two would
have disagreed and the screen would have shown a signal the engine never acted on. Both now call
one shared function (`backend/app/algo/combine.py`), and a test fails if a second copy ever
reappears.

The decision trace also improved. It used to say "indicators not unanimous" — which would be a
lie under Majority, and never said how the vote split. It now records the evidence:
`enabled indicators disagree (2 call, 1 put)`, `majority tied 1 to 1`,
`2 enabled indicators are neutral, which blocks a decision`,
`majority: 2 call against 0 put (1 neutral ignored)`.

### The instruction's "four questions", answered for every row

> *What did MTF say? What did QAE say? What did SMC/OI say? Why did the combination produce this?*

A search of the whole repository — backend, frontend, validation scripts — confirmed **no other
copy of the rule exists**: every screen and every mode displays a value the shared function
produced. Replay and signal history replay *recorded* combined rows rather than re-deriving them.
Three gaps in *traceability* did turn up, and all three are fixed:

| Gap | Fix |
|---|---|
| The config screen's summary line said "all must agree" no matter which rule was selected — it contradicted the dropdown 50 lines above it | The note now describes the configured rule and neutral mode |
| A stored decision recorded *which* indicators voted but not *which rule* counted them, so a row could not be re-derived after the setting changed | `combine_rule` and `neutral_mode` are now part of every decision's zone snapshot, and shown in the Decision Trace |
| The combination evidence was recorded only on no-trade minutes — when a direction *was* produced it was dropped | Entries now read `entered via R1 · all 3 voting indicators agree (call)`; the live strip carries the reason and the active rule on its frame |

### 4. Swing scoring lookback — the confidence gate means something again

**Before:** every swing confirmed since 09:15 kept adding points for the rest of the day. On your
17 September file the totals reach **495 / 330** by the close, so the 55-point confidence gate was
cleared from mid-morning onward — it said "no trade" on only **35 of 384 minutes**.

**Now** there is one setting: **Lookback (bars, 0 = whole session)**. Set it to 30 and only swings
from the last 30 bars score. On that same day it changes the signal on **44 minutes** and produces
**24 more no-trade minutes** — the gate starts doing its job.

**Default is 0, which is exactly today's behaviour.** Nothing changes until you switch it on.

**The rule that must never be broken:** lookback filters *what scores*, not *what the structure
is*. The range zone and the breakout test still read the complete session. A short lookback must
not erase a zone formed earlier in the day. This is pinned by a test that asserts the zones and
breakouts are byte-identical with and without the filter.

### 5. Candle read — size is a setting, and a real bug is fixed

**Two settings:** **Candle Size** (1-minute bars per candle, default 5) and **Candle Read**
(*last fully closed* or *current forming*, default last fully closed). The panel prints the active
mode underneath, e.g. `5-bar candle, last closed`.

**The bug.** The rule always read the second-to-last candle. That is correct while the newest
candle is still forming — but the moment a candle completes, it skips a genuinely finished one:

```
60 bars so far  →  [0-4][5-9] … [50-54][55-59]
                                   ^-2    ^-1  ← complete, but was being skipped
before: read [50-54]   — five minutes stale
now:    read [55-59]   — correct
```

At the default size that was wrong **one minute in five**; at size 15 it would have been wrong one
minute in fifteen. The reference HTML has the same off-by-one, so this is a deliberate divergence
from it — you chose the spec's wording over the reference's code.

**Measured cost: 2 of 384 minutes on 17 September.**

---

## How it was verified

| Check | Result |
|---|---|
| Backend test suite | **682 passed** (was 623; 59 new tests) |
| Detailed instruction §13 matrix, Examples 1–18, §11 state table | **30 of 30 cases pass** |
| Backtest parity | **633/633** |
| Reference HTML vs our engine, your CSV, 6 setting combinations × 8 bars | **46 of 48 readings identical** |
| The 2 differences | both at bar 384, the exact candle boundary — the intended fix, nothing else |
| Config back-compat | a document saved before today loads with all five settings at the old behaviour |
| Out-of-range / bogus values | rejected by validation, not silently clamped |
| TypeScript | compiles clean |
| Images | backend + frontend built, stack recreated |
| Backtest A/B, 115 trading days (1 Apr – 18 Sep) | old rule **9 trades, −₹8,400.71** · new rule **8 trades, −₹8,470.08** |

**What the candle fix actually costs: one trade and ₹69.37 over 115 trading days.** Both runs used
identical inputs; the only difference was which candle the red rule reads. The removed trade was a
winner (wins went 2 → 1), which is worth knowing — but on a sample of nine trades that is noise,
not evidence that the old rule was better. The old rule was reading a stale candle; the new one
reads the right one.

The reference comparison is the one worth dwelling on: **every** forming-mode reading, **every**
zone and **every** breakout matched the reference exactly, across lookbacks 0/30/60 and candle
sizes 5/15. The only divergence is the one we intended.

### Two warnings the config screen now gives you

- Majority with an **even** number of enabled indicators — a split vote ties, and a tie is No Trade.
- Majority **and** Abstention together — the loosest possible combination; one directional
  indicator can decide the zone while the others are neutral.

Both are warnings, not errors. They do not block a save.

---

## Files changed

**Backend** — `algo/combine.py` (new) · `algo/orchestrator.py` · `algo/live_stream.py` ·
`algo/engines/oi_structure.py` · `algo/config_models.py`
**Frontend** — `types/algo.ts` · `components/algo/DailyConfigPanel.tsx` ·
`components/algo/engines/OiStructurePanel.tsx`
**Tests** — `tests/test_combine_rule.py` (new) · `tests/test_oi_structure_golden.py` ·
`tests/test_decision_trace.py` · `tests/fixtures/oi_structure_golden.json`

No database migration. No config-version bump.

---

## Honest limits

- **Nine of the eleven golden fixtures were re-baselined.** Every one of them is 180 bars — exactly
  divisible by 5, i.e. precisely the boundary the candle fix addresses. Only the red-candle group
  of fields changed; swings, zones and breakouts remain verbatim reference values and are still
  asserted. Each fixture keeps its original values under `jsReferencePreStrict`, and the file
  records why we diverge. **No scenario changed its signal.**
- **Old stored backtest runs will not re-run to their recorded totals** (roughly 1% of minutes).
  Their recorded results stand as historical records; we re-baseline going forward. This was a
  deliberate choice over leaving the wrong rule alive behind a compatibility shim.
- The PDF's §4.6 score table does not reproduce on this data — expected, since the PDF says its
  examples are explanatory rather than test data. We verified against the reference HTML instead.
- The A/B window produced only **9 trades across 115 days**, so its rupee delta is a sanity check
  on the size of the change, not a statistical result. It confirms the fix is small; it cannot
  tell you the fix is profitable.
- **Feature 4 was not A/B'd.** Its default is 0, so it changes nothing until you switch it on —
  and when you do, the honest way to choose a value is a run per candidate on the zone you mean to
  use it in, not a single global number.
