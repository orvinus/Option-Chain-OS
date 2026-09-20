# Complete sessions: the afternoon the chart was throwing away

**2026-09-12** · follows `docs/UMP-SETTLEMENT-HISTORY-2026-09-11.md`.
Local only: nothing committed, pushed, or sent to the VPS.

---

## 1. What the screenshot showed

The UMP 5-minute chart for NIFTY 23450 CE ended around **13:10** even though
the session runs to **15:40**. The data was not missing. It was in the database
the whole time, and the reader was discarding it.

Three separate faults, stacked:

| # | Fault | Effect |
|---|---|---|
| 1 | Archive preference applied per **day** | every live minute of a partially archived day was dropped |
| 2 | Nothing detects a **truncated archive day** | the archive stayed truncated forever |
| 3 | Untraded minutes left **holes** in the chart | a thin strike showed one candle for a whole session |

---

## 2. Fault 1 — day-level source preference

`_PREMIUM_MINUTES_TEMPLATE` prefers the vendor 1-minute archive over the live
tick table for completed days, and rightly so: archive bars carry exchange-grade
open/high/low/close per minute, while live rows fold from LTP ticks. That
preference was correct. Its **granularity** was not:

```sql
arch_days AS (SELECT DISTINCT strike, option_type, (bucket AT TIME ZONE 'Asia/Kolkata')::date AS d FROM arch)
...
FROM live lv WHERE NOT EXISTS (SELECT 1 FROM arch_days ad WHERE ... ad.d = lv's date)
```

If the archive held **one row** for a date, the entire live contribution for
that date was discarded. The rule silently assumed that a day present in the
archive is *complete* there.

Measured on 23450 CE:

| day | archive held | live held | served |
|---|---|---|---|
| 2026-09-10 | 09:15–13:11 (237) | 09:15–15:40 | **237, ending 13:11** |
| 2026-09-11 | 09:15–11:54 (160) | 09:15–15:40 | **160, ending 11:54** |

That is the screenshot exactly.

**Fix:** match per minute instead of per date. The archive still wins every
minute it has — the parity property the day rule was protecting is untouched —
and live tops up only the minutes the archive genuinely lacks. This is not a new
idea in that query: the `arch_today` branch has always done exactly this for the
current day.

After the fix, with no other change: **10 Sep 237 → 370 minutes, 11 Sep 160 →
386**, both running to 15:40.

---

## 3. Fault 2 — a truncated archive day is invisible

The truncation was not specific to one contract. For **every** NIFTY contract
the archive stopped at 13:11 on 10 Sep and 11:54 on 11 Sep — an interrupted
nightly top-up.

Nothing would ever have repaired it. The boot gap-fill asks `missing_days()`,
which reads `oi_day_stats.usable_minutes` — and that column takes the **better**
of the two sources:

| day | live_minutes | arch_minutes | usable_minutes | detector says |
|---|---|---|---|---|
| 2026-09-09 | 241 | 386 | 386 | fine |
| 2026-09-10 | 372 | **237** | 372 | fine |
| 2026-09-11 | 386 | **160** | 386 | fine |

So the day was never "missing", the broad pull never looked at it, and the
archive stayed short permanently. Meanwhile the live tick table is retained
~120 days, so the loss would eventually have become total.

**Fix:** `archive_short_days()` flags a past session whose archive is short
**while the live table proves the session was complete** — the precise signature
of an interrupted pull. Each flagged day gets one targeted `pull --day` repair.

Requiring live to be complete is what stops it looping: a day the vendor simply
cannot serve is never flagged, and a repaired day stops matching as soon as its
archive fills. The repair also waits for `historical_allowed`, the same
post-close window the other vendor pulls respect, so it never competes with the
live feed mid-session.

**Both days were repaired** by running the pull directly: 101,161 rows for
10 Sep, 118,542 for 11 Sep. The vendor had the data all along. A rescan across
the last 40 days now finds **zero** remaining short days.

---

## 4. Fault 3 — holes inside a session

Even with every real minute served, a far-OTM strike leaves genuine holes: 23450
CE traded **once** on 3 Sep (one lot), 7 minutes on 4 Sep, 66 on 7 Sep. Those
minutes have no data anywhere, from any source — the contract did not trade.

`session_filled_minutes` holds the last traded price across them so every
session runs open to close. A held price is the honest reading of an untraded
minute: nothing happened, the contract is still worth its last trade, and it is
the same value the exchange's own daily bar carries for such a day.

Three rules keep it truthful:

- **Display only.** The engine still replays the real traded minutes.
  TradingView draws no bar for a minute with no trade, Pine therefore never
  evaluates one, and our parity numbers depend on matching that. The engine
  already survives a missing minute — a 5m window whose final minute never
  arrives still fires its confirmed close from the stored running values — so
  there was nothing to repair there, only something to break.
- **Never draws the future.** The last real minute in the series is the
  frontier; the session holding it fills only up to that minute. Earlier
  sessions fill to their own close.
- **Marked.** The response carries `candles_source: "minutes+held"` and a count,
  and the chart header states it: *"1162 minute(s) of this contract's sessions
  had no trade — shown at the last traded price."*

---

## 5. Result

For 23450 CE, every intraday interval now runs the full stored life:

| interval | candles | first | last |
|---|---|---|---|
| 1m | 2695 | 2026-09-03 09:15 | 2026-09-11 15:39 |
| 5m | 539 | 2026-09-03 09:15 | 2026-09-11 15:35 |
| 15m | 182 | 2026-09-03 09:15 | 2026-09-11 15:30 |
| 1h | 49 | 2026-09-03 09:15 | 2026-09-11 15:15 |

The 5-minute chart that stopped at 13:10 on 10 Sep now reaches 15:35 on 11 Sep.

---

## 6. Verification

| check | result |
|---|---|
| Unit suite | **536 passed** (was 523; 13 new) |
| `validation/backtest_parity.py` | **633/633** |
| `validation/backtest_golden.py 2026-09-01 2026-09-03` | **PASS**, both halves |
| `scripts/stack_scenario_sweep.py` | **180/180** |
| Browser, Algo Config + Backtesting, all 6 intervals | no problems |
| Backtest #144 vs #139, 145 days | **every metric identical, zero delta** |
| Remaining archive-short days in the last 40 | **0** |

The backtest result is the useful negative. Its window (2026-02-05 → 2026-09-09)
holds no archive-short day, so recovering the two September afternoons could not
move it — and the per-minute precedence change, which touches every historical
day, moved nothing either: same 121 trades, same 32 wins, net P&L and max
drawdown identical to the paisa. The fix adds the minutes that were being
dropped and changes nothing else.

---

## 7. Files

`backend/app/algo/series.py` — per-minute source preference in
`_PREMIUM_MINUTES_TEMPLATE`; new `session_filled_minutes`.
`backend/app/api/algo_engines.py` — intraday display fill.
`backend/app/ingest/gapfill.py` — `archive_short_days` + targeted repair.
`backend/tests/test_session_gapfill.py` — new.

---

## 8. Honest limits

- The hold-last fill is **display only** and deliberately so. If the engine
  should also evaluate held minutes, that is a change to Pine parity and needs
  measuring against a TradingView export first, not assuming.
- `archive_short_days` only catches a truncated day while the live table still
  holds it (~120 days retention). A day truncated longer ago than that cannot be
  distinguished from a day the vendor never had, and is not flagged.
- The threshold is `gapfill_min_minutes` (300 of 386). A day truncated by fewer
  than ~86 minutes is not flagged.
- The two repaired days were pulled by hand today; the automatic path is now in
  place but has not yet had a real truncation to catch on its own.
