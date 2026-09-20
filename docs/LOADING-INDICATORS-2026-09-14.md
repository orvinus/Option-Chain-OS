# Loading indicators across the software

**2026-09-14.** Local only: nothing committed, pushed, or sent to the VPS.

---

## What you asked for, and what the audit found underneath it

You asked for a loading animation whenever something is loading. Auditing every
page and panel first turned up something more serious than a missing spinner:
**while data was loading, several screens stated things that were false.**

Ultra Master Pro is the clearest case. Its evaluation replays the contract's
entire stored life and takes seconds. During that wait the page reported:

- "No trades in the contract's stored life."
- "No levels yet."
- Max SL "idle", Trail Exit "not armed", Zone Trail "arms near NB"

None of that was a reading the engine had made. It was the absence of a reading,
formatted to look like one. The OI Change tab did the same with "No evaluation
yet." for **9.2 seconds**, measured.

Worse, three dashboard pages kept the *previous* answer on screen while the new
one loaded, so a symbol, date or bucket change silently showed the wrong
session's numbers under the new session's heading.

---

## 1. One indicator for the whole app

Both API layers funnel every call through a single function, so counting there
covers every page, every panel, every poll, and anything added later.

- `api/inflight.ts` — a small in-flight counter.
- `api/rest.ts` and `api/algoRest.ts` wrap their one shared request function.
- `components/Loading.tsx` — `GlobalLoadingBar`, `Spinner`, `LoadingBlock`
  (nothing to show yet) and `LoadingOverlay` (has data, refreshing).
- Mounted once in `App.tsx`.

Two timing rules stop it becoming noise. Nothing appears for 180 ms, because
most calls return in tens of milliseconds and a spinner that flashes on every
one is worse than none. Once shown it stays 400 ms, so a call that resolves just
past the threshold does not produce a one-frame flicker.

Three raw `fetch` calls bypassed both layers and were invisible to it. They now
go through the same counter.

Motion respects the OS "reduce motion" setting: the indicator stays **visible**,
because it is information, but stops moving.

---

## 2. Panels now distinguish "loading" from "empty"

The four engine panels had **no data-load flag at all** — only `data` and
`error`. Each now has one, and every false-empty message is gated behind it.

The flag has to know which fetch it is. Each panel has one `load()` serving both
the user-initiated path and a 30-second background poll, so `load()` takes
`{ background: true }` from the poll. Without that the panels would blink every
30 seconds over data that was already correct.

| panel | said while loading | now |
|---|---|---|
| Ultra Master Pro | "No trades in the contract's stored life." | "Replaying the contract's stored life…" |
| Ultra Master Pro | "No levels yet." | loading state |
| Ultra Master Pro | "idle" / "not armed" / "arms near NB" | "evaluating…" |
| OI Change | "No evaluation yet." | loading state |
| OI Change | "Per-strike deltas unavailable (older backend or the strike query failed)" | loading state |
| Multi-TF | "No evaluation yet." | loading state |
| Ratio | "No evaluation yet." | loading state |
| Ratio | "No active conditions met." ×3 | loading state |

**Ultra Master Pro's flag has one extra rule.** A context change fires two
overlapping requests, so only the newest may lower the flag. A superseded
response landing late must not hide an indicator for a request still running.

"Re-run Engine Now" had no feedback whatsoever and read as a dead button on a
multi-second call. It now shows "Re-running…" and disables while running.

---

## 3. Stale answers are no longer presented as current

This is the part that was not a cosmetic problem.

**`useMultiTimeframe` and `useRatioTimeseries`** kept a `hasData` flag that was
only ever reset when the hook was disabled. After the first success, `loading`
never fired again **and** the old data was never cleared. Changing symbol, date,
ATM window or bucket left the previous answer on screen, with its own timestamp,
indefinitely. Both now re-arm on a dependency change. The reset is in the effect
body, not inside the fetch, so the background poll stays silent.

`useMultiTimeframe` feeds three of the five dashboard pages, so this landed in
all three.

**`useOITimeseries`** did not clear its points, so the Charts page drew the
previous window's candles under the new window's heading.

**`useReplayFrames`** did not clear its frames, so every chart, table and KPI on
the Replay page rendered a complete, convincing view of the **wrong day**. The
existing comment on that page's export button records that exactly this produced
mislabelled export files.

---

## 4. Smaller fixes in the same pass

**Two theme colours were used but never defined**, so they silently produced no
styles: `text-foreground` at 39 call sites and `bg-surface` at 4. That is why the
two pre-existing loading chips rendered as bare text with no background over a
dimmed chart. Both are now defined, which repairs every call site.

**Multi-TF had no empty state at all.** A result with zero rows and a fetch that
had not started both rendered a blank area under the headers, identical to
loading. It now says which it is.

**The Replay page's greeks message** asserted a permanent cause for a transient
state: "No stored greeks for this session yet — greeks are persisted going
forward". It said that while still loading them.

---

## 5. Deliberately left alone

- **`LiveStrip`** — its header states "never a spinner" as a design decision. A
  frozen page reads "REPLAY — last stored session" rather than pretending to be
  live. Respected.
- **Backtest run progress** — already a determinate days/percent bar with a
  "simulating {date}…" line. A spinner on top would be duplication.
- **Export dialog** — already stages progress with live row counts.
- **Button actions** — Saving…, Signing in…, Cancelling…, Restoring… were
  already correct and are untouched.
- **Background polls** — health 3s, expiries 30s, paper session 5s and the rest
  stay silent by design. They refresh data that is already right.

---

## Verification

| check | result |
|---|---|
| Backend unit suite | 559 passed, unchanged (no backend files touched) |
| Frontend `tsc` and production build | clean |
| Indicator appears on all 5 dashboard tabs + Algo Config + Ultra Master Pro | **yes, 7/7** |
| Earlier six changes re-verified in both pages | **0 problems** |
| JavaScript errors | none |

Verified under a throttled network so the indicator was observable, then
re-verified at normal speed.

**One measurement worth keeping.** The OI Change evaluation takes **9.2 seconds**
against the live database. My first verification run screenshotted at 9 seconds
and reported the charts missing; the endpoint timing, not a regression, was the
cause. That nine-second window is exactly what used to read "No evaluation yet."

---

## Files

**New.** `frontend/src/api/inflight.ts`, `frontend/src/components/Loading.tsx`.

**Changed.** `api/rest.ts`, `api/algoRest.ts`, `App.tsx`, `index.css`,
`tailwind.config.js` · engines `OiStructurePanel`, `MtfRatioPanel`, `MqaePanel`,
`UmpPanel`, `EngineActions`, `EnginesPanel` · `DailyConfigPanel`, `MiscPanels` ·
hooks `useMultiTimeframe`, `useRatioTimeseries`, `useOITimeseries`,
`useReplayFrames` · pages `MultiTimeframePage`, `ReplayPage`.
