# Why the two Ratio charts showed different crossover times

**2026-09-14.** Local only: nothing committed, pushed, or sent to the VPS.

---

## The answer first: the data was never different

The Ratio tab in Algo Config and the Ratio chart on the dashboard Replay page
showed the same crossover at different clock times — 10:15 against 10:16/10:17
for NIFTY, 11 Sep 2026, expiry 15 Sep.

Pulling both backends for the same session and comparing row by row:

| | first point | last point | rows |
|---|---|---|---|
| Ratio tab, `/api/algo/engines/mqae` | 09:15 | 15:40 | 386 |
| Replay page, `/api/replay` | 09:16 | 15:41 | 386 |

**Aligned by index, all 386 rows are identical — call OI and put OI both, to the
contract.** Same strike window (22900–23900, ATM 23400 ± 10), same totals, same
everything. The two pages compute the same numbers from the same data.

The only difference was the **label on the bucket**, and it was exactly one
minute.

---

## Why the labels differed, and why both were defensible

A one-minute bucket covering 09:15:00–09:15:59 can honestly be called either.

**The Ratio tab calls it 09:15.** `time_bucket` labels a bucket with its start,
and that is the convention every other chart in the app uses: a candle covering
09:15 to 09:16 sits at 09:15.

**The Replay page called it 09:16**, and did so deliberately. From
`services/replay.py`:

> `time_bucket` labels a bucket with its START, so the bucket labelled 11:00
> holds the last tick in [11:00, 11:01) and the player used to display an 11:00
> clock over data observed up to 11:00:59 — one step of look-ahead into the
> future, in the one tool whose whole job is replaying a session honestly.

That reasoning is right, and it is load-bearing for the **transport**: a frame
labelled T must never contain an observation later than T, or the player leaks
the future.

So neither page was wrong on its own. They were wrong **together**, because the
Replay chart's own footnote claimed it was "matching the Ratio tab's Full Day
mode" — and it was off by one minute.

---

## The fix

The relabelling now happens **once, at the source** — in `seriesPoints`
(`ReplayPage.tsx`), where the frame list is turned into chart points. Every
chart on the Replay page draws from that one array, so all of them moved onto
the app-wide convention together:

- Ratio & PCR
- Call OI Change
- Put OI Change

The **transport clock is untouched** and keeps the end label, because that is
what stops the player revealing the future. Its footnote now states the
convention rather than claiming a parity it did not have.

Three things this fixed that a per-chart shift did not:

1. **The OI candle charts were one minute ahead too.** With the playhead at the
   end they labelled a candle **15:41** — a minute that does not exist in a
   session that closes at 15:40.
2. **The 5m/15m/30m bar grouping was off by one bucket.** `ratioLines` groups on
   `sinceOpen`; with end labels each bar folded in the previous bar's last
   minute. Grouping on the corrected timestamps fixes it for free.
3. **The step is now derived robustly.** It is the *smallest* positive gap
   between frames, not `frameMs[1] - frameMs[0]`: a session with a missing
   minute leaves a wider gap, and the naive form would read that hole as the
   step and shift the whole axis by it.

---

## Verified in the browser, not on paper

`TimeSeriesChart` writes the hovered clock and every series value into a real
DOM `<div>`, so a Playwright run can sweep the crosshair across each chart and
recover exactly what is plotted — no pixel diffing, no canvas OCR. Swept both
charts on 11 Sep, Full Day, ATM ± 10:

| | points read | crossover |
|---|---|---|
| Algo Config → Ratio | 383 | **10:15** |
| Dashboard → Replay | 385 | **10:15** |

Values at every clock across the crossing are identical:

| clock | Ratio | PCR |
|---|---|---|
| 10:13 | 1.15 | 0.87 |
| 10:14 | 1.03 | 0.97 |
| **10:15** | **1.00** | **1.00** |
| 10:16 | 0.96 | 1.04 |
| 10:17 | 0.95 | 1.05 |

On the 230 clocks both sweeps covered in a single pass, **229 matched to the
displayed precision**. The exact crossing falls *between* samples — 10:15 is
where the two rounded values meet at 1.00 — which is why it reads as "the
crossover is at 10:15" on both.

The OI candle charts now start at **09:15** and end at **15:40**.

Harnesses: `scratchpad/crossover_compare.py`, `scratchpad/replay_only.py`.

---

## One thing I got wrong on the way, worth recording

My first comparison reported a much larger gap — a crossover at 10:09 and a
completely different strike range of 21700–26200. That was **my error, not a
bug**: I queried the Friday zone while the screenshots were taken on the Monday
zone, and Friday's Z1 is configured `strikes_atm_window = -1`, meaning the whole
chain rather than ATM ± 10.

Worth knowing as a live fact about the config: **only Monday's Z1 uses ATM ± 10;
Friday, Tuesday, Wednesday and Thursday are all set to the full chain.** If that
is not deliberate, the four days are evaluating a far wider basket than Monday.

---

## A second, separate inconsistency I did NOT change

The Replay page's strike window is centred on the **playhead's** spot, and that
same window is then applied retroactively to the whole session
(`seriesPoints` → `windowFilter(current, …)`). So scrubbing changes the history:

| playhead | ATM used | crossover |
|---|---|---|
| at the session end | 23400 | 10:15 |
| at the open | 23250 | earlier |

The Ratio tab instead resolves one fixed basket from the spot at the window end.

Both are arguable. A moving window answers "what does the currently relevant
strike band look like", a fixed one answers "what did this band do all day". But
it does mean the Replay page's history is not stable as you scrub, which is
surprising. Left alone deliberately: it is a behavioural choice, not a defect,
and it is not what was reported. Say the word if you want it pinned.
