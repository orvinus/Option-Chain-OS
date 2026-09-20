# Plan: six changes (2026-09-13)

Status: **approved 2026-09-13, implementing**. All four open decisions closed.
Deploy rule: build and verify locally. Nothing committed, pushed, or sent to the VPS.

Ordered by risk, lowest first. Each part is independently shippable and
independently verifiable, so we can stop after any of them.

---

## Part 1 — SENSEX expiries (smallest, fully diagnosed)

**Symptom.** Selecting SENSEX for a day still lists NIFTY expiries.

**Cause.** The panel asks for one hard-coded symbol:

```tsx
// DailyConfigPanel.tsx:103
const res = await fetch("/api/expiries?symbol=NIFTY", { credentials: "same-origin" });
```

while the symbol selector 250 lines below writes a per-day `index_symbol`.

**The backend is already right.** Verified live:

| symbol | first two expiries |
|---|---|
| NIFTY | 2026-09-15, 2026-09-22 |
| SENSEX | 2026-09-17, 2026-09-24 |

**Change.** In `DailyConfigPanel.tsx`:
1. Read the selected day's symbol (`d.index_symbol`) and pass it to the fetch.
2. Add it to the effect's dependency array so switching day or symbol refetches.
3. Keep a cancellation guard so a slow response cannot overwrite a newer one.
4. Label the strip with the symbol, since it is now symbol-specific.
5. `currentWeekly` already derives from the list, so it corrects itself.

**Scope note.** This panel is rendered by both `AlgoConfigPage` and
`BacktestingPage`, so one change fixes both, exactly as reported. Backtesting has
no separate expiry selector; it derives the symbol from the config.

**The fix already exists in this codebase, one panel over.** The engine-tabs
expiry dropdown had the identical bug and was repaired on 2026-08-18:

```tsx
// EnginesPanel.tsx:82-84
// The evaluated symbol is the selected DAY's index (SENSEX days list SENSEX
// expiries — this dropdown was hardcoded to NIFTY until 2026-08-18).
const daySymbol = draft.days[day]?.index_symbol ?? "NIFTY";
```

So this is not a new design, it is applying a proven one to the panel that was
missed. Copy that pattern verbatim, including the follow-up at `EnginesPanel.tsx:107-114`
which clears a pinned expiry once it no longer belongs to the selected symbol's
chain — a SENSEX day holding a NIFTY expiry is meaningless.

**Verify.** In the browser, set Monday to SENSEX and confirm the strip flips to
Thursday expiries and that the "current weekly" chip lands on the right one.
Switch back to NIFTY and confirm it flips back.

---

## Part 2 — Change ID and password, stored in the backend

**What you asked for.** Move the credentials out of the environment file, because
a restart risk is unacceptable, and allow changing both the ID and the password.

**Good news first.** A real credential store already exists: table `algo_users`
with `user_id`, a UNIQUE `username`, a PBKDF2 `password_hash`, and a role. The
environment values are only used to seed that table **when it is empty**:

```python
# auth.py:124-126
n = (await session.execute(_COUNT_USERS_SQL)).scalar_one()
if n and int(n) > 0:
    return
```

So a changed password would already survive restarts. The database is already
the source of truth.

**The actual blocker.** Two places refuse *all* access unless the environment
variables are still present, even when a valid user row exists:

```python
# auth.py:189 (require_admin) and api/algo_auth.py:43 (login)
if not settings.algo_admin_user or not settings.algo_admin_password:
    raise HTTPException(503, "Algo admin login is not configured. …")
```

That is what forces the secret to stay in the file. Removing it is the workaround.

**Changes.**

1. **Relax the gate.** Replace the environment check with a check that at least
   one user row exists. Keep the loud 503 when there is genuinely no way in, so
   misconfiguration is never an open door. The environment stays supported purely
   as a first-run bootstrap, so existing installs are unaffected.
2. **New endpoint** `POST /api/algo/auth/change-credentials`. It requires a valid
   session, and in the body: current username, current password, new username,
   new password. Server-side it re-authenticates the current pair before touching
   anything, rejects a new username that is already taken, and writes the new
   username plus a fresh PBKDF2 hash in one transaction.
3. **Confirmation fields are checked in the browser**, not sent twice. Confirming
   the old password and the new password is a UI concern; the server needs each
   value once.
4. **Audit it.** Write a `credentials_changed` row through the existing audit
   helper, recording who changed it and when, never the password.
5. **Force re-login.** Clear the session cookie on success so the new credentials
   are proven immediately.
6. **UI.** A "Change sign-in credentials" card in the Security and Audit tab,
   next to the existing "Signed-in user" card. Fields in your stated order:
   username, old password, confirm old password, new ID, new password, confirm
   new password, then the button. Client-side checks: both confirmations match,
   new password meets a minimum length, new values differ from old.

**Rate limiting.** The change endpoint verifies a password, so it is a guessing
surface. It is already behind a valid session, and every attempt is audited. I
will add a short server-side cooldown after repeated failures.

**Two consequences you should know.**
- After changing the credentials, the values in the environment file are stale
  and misleading. They can then be deleted. I will note that in the file's
  comments rather than deleting them for you.
- My own local test harnesses read those environment values to sign in. If you
  change the credentials, those harnesses need the new ones. That affects my
  verification tooling only, not the running product.

**Verify.** Change the password, restart the backend, confirm the new password
still works and the old one does not. Change the ID, restart, confirm the same.
Confirm the main dashboard password is untouched throughout. Confirm a wrong old
password is rejected and audited.

---

## Part 3 — OI Change becomes four charts

**Target**, matching your screenshot and the reference file
`docs/reference/oi_smc_engine.html`, a two by two grid:

| position | chart | type | extras |
|---|---|---|---|
| top left | Call OI Change, 5 Min | candles, red | footer "Last candle: RED -> routes to Call" |
| top right | Put OI Change, 5 Min | candles, blue | footer "Last candle: RED -> routes to Put" |
| bottom left | Call OI Change, 1 Min | line, red | swing labels, shaded zone, breakout, Structure toggle |
| bottom right | Put OI Change, 1 Min | line, blue | swing labels, shaded zone, Structure toggle |

The timeframe pill bar is removed entirely, including 10, 15 and 30 minute.

**This restores the original intent.** The panel's own header comment already
describes exactly this layout: "quad charts (Call/Put 5-minute candles on top,
Call/Put 1-minute lines with swing labels + the shaded range zone below)". The
implementation had collapsed it into one interval-switched chart.

**No backend work.** One call already returns everything, per side: the 1-minute
series, swings, zone, breakout and red-candle flag for both Call and Put. The
5-minute candles are derived in the browser from the same 1-minute series.

**The real work is splitting the chart component,** which currently hard-codes
both sides in roughly ten places: the series array, the side-by-side candle
offset, the zone-band loop, the swing scatter (it concatenates both sides), the
red-candle pin loop, the legend, the header and the footer. It gains a `side`
prop and a `showStructure` prop.

**The reference has been found** and archived at
`docs/reference/oi_smc_engine.html` (supplied 2026-09-13). Three docstrings cite
it but it had never been in the repo. It is now the spec for this part, and it
settles several details I had guessed at:

| detail | reference says |
|---|---|
| grid | `grid-template-columns:1fr 1fr`, gap 12px |
| chart height | **220px**, not the 500px we use today |
| toggles | **exactly two**, both labelled "Structure", on the 1-Minute charts only |
| 5-Minute charts | no toggle, no structure overlay, plain candles plus a footer |
| footer text | `Last candle: RED -> routes to Call` / `Green -> ignored` |
| title suffix | `· 16 swings, breakout DOWN` as a dim hint inside the title |
| red candle | `candles[candles.length-2]` with the comment `// last fully closed` |

**Red-candle question: settled by the reference, no longer open.** It reads the
last **fully closed** candle. Our backend engine already matches. Only the
frontend pin is out of step, pinning the last candle which may still be forming.
The pin moves to match the engine and the reference.

**Toggle question: settled by the reference.** There are two toggles, not six.
Each gates only the swing labels and the shaded zone on its own 1-Minute chart.
It is pure display: the engine's scoring is governed by the separate 1m and 5m
master switches that already exist in our panel. Because it is pure display, the
state is remembered in the browser and never marks the config unsaved.

**Open, and smaller than before:** you also said "all of the charts should be
customizable, on and off". The reference has no per-chart show and hide, only the
two Structure toggles. I will build the reference exactly. Say the word and I
will add four show and hide toggles on top; it is a small addition.

**Four details that are easy to get wrong, so they are called out now.**
1. The grouped candles are offset half a width left and right so two sides can
   share one axis. With one side per chart that offset must go to zero and the
   candle width must widen, or the candles render as thin sticks off centre.
2. Each chart needs its own drawing-persistence key, or all four share one
   storage bucket and drawings bleed across them.
3. Chart height drops from 500px to the reference's 220px, and the grid padding
   and legend need to shrink with it or the panel will not breathe.
4. The zero line currently lives inside the same overlay as the structure
   markings. Turning Structure off must not take the zero line with it.

**Verify.** All four charts render from one fetch. The structure toggles hide
only the swing labels, zone and breakout. Hiding charts rearranges the grid
cleanly. No timeframe pills remain. Swing counts and breakout text in each title
match the per-side data.

---

## Part 4 — Multi-TF uses Lowest-OI Side (scope corrected 2026-09-13)

**The code change is tiny. The behaviour change is not.**

Both side definitions already exist and are already carried on every row, already
serialised, already typed, already displayed side by side. There is exactly one
place in the entire backend where the rule compares a side:

```python
# mtf_ratio.py:172-175
if cond.side != "Any" and r.side != cond.side and r.side != "Neutral":
    passed = False
    reasons.append(f"Ratio Side failed (expected {cond.side}, got {r.side})")
```

Changing `r.side` to `r.lowest_side` is the whole functional change. The stored
config field is named generically, so **no migration and no config version bump**.
Saved configs and frozen backtest runs keep loading byte for byte.

**How much does it actually change?** I measured both rules over four real
sessions, every cursor position, every timeframe:

| timeframe | readings that differ |
|---|---|
| 1m | 29.5% |
| 3m | 28.2% |
| 5m | 28.3% |
| 10m | 26.9% |
| 15m | 25.1% |
| 30m | 25.7% |
| 1h | 25.1% |
| 2h | 17.5% |
| 3h | 11.7% |
| full day | 0.0% |
| **overall** | **21.8% of 15,360 readings** |

In the frozen reference fixtures the two sides disagree on **four of five**
timeframes.

**So this will change which rules fire and therefore which trades are taken.**
That is what you asked for, and it makes the platform's own guide accurate, since
that guide already claims the Multi-TF engine uses the signed dominant side. But
it is a real trading-behaviour change, not cosmetic. I want that acknowledged
before I make it.

**A second, subtler effect.** A Neutral side currently passes any side filter.
Neutral is much rarer under the new rule, because it now requires the two signed
values to be exactly equal rather than equal in magnitude. A row that used to slip
through a Call filter on a mirror-image reading will now hard-fail. My
recommendation is to keep the Neutral escape hatch for symmetry, and to note that
it will fire far less often.

**Golden fixtures will break, correctly.** They were generated from the original
reference implementation, which uses the magnitude rule. I will split that suite:
keep the reference-parity assertions on the row maths, which are unchanged, and
regenerate the rule-outcome expectations under the new semantics, clearly marked
as a deliberate divergence.

**Changes, corrected 2026-09-13.** Ratio Side disappears from the Multi-TF page
entirely. Two distinct edits:

**(a) The rules — "Ratio Direction Conditions".** The per-condition side selector
becomes **Lowest-OI Side**. This is the one comparison in the engine, its failure
message, the rules-editor column header, and the explainer line beneath it. The
rest of a condition is untouched: the Above/Below threshold still operates on the
"Call : Put" ratio value, and the Call Sign and Put Sign facets are unchanged.

**(b) The positions table — "Multi-timeframe position ratio".** The **Ratio Side
column is removed**. The **Lowest OI Side column stays**. Today the table carries
both:

| # | column | after |
|---|---|---|
| 1 | Timeframe | keep |
| 2 | Call OI Δ | keep |
| 3 | Put OI Δ | keep |
| 4 | Call : Put | keep, the rules' threshold still reads this |
| 5 | Ratio Side | **remove** |
| 6 | Lowest OI Side | keep |

The empty-state `colSpan` drops from 6 to 5.

Also: the docstrings describing the rule, the panel hint, and moving the
"(filter)" annotation in the decision trace from the Ratio Side column to the
Lowest-OI Side column. The two guide files become correct as a side effect.

**What is NOT removed.** The Ratio Side *computation* stays in the engine and on
the wire. It still drives the "Call : Put" text, and the decision trace keeps
showing it for diagnosis. Only the rule comparison and the table column change.

**Not changed.** Types, the endpoint, the backtest deps, the overrides, the
Pydantic field name, any stored config. Backtesting uses the same engine and the
same panel, so it is covered automatically.

**Verify.** Unit suite with regenerated fixtures. A before and after backtest over
the same window to quantify the change in trades and profit and loss. The decision
trace showing the Lowest-OI Side column driving the pass or fail.

---

## Part 5 — Ratio replay follows the selected interval

**Today.** The slider is hard-coded to 5-minute steps, 76 positions, labelled
09:15 to 15:30.

**Wanted.** The replay step follows whatever is selected on the PCR-over-time
chart, one for one:

| selected | replay step |
|---|---|
| 1m | 1 minute |
| 5m | 5 minutes |
| 10m | 10 minutes |
| 15m | 15 minutes |
| 30m | 30 minutes |
| Full Day | 1 minute (chosen 2026-09-13) |

Confirmed unchanged on 2026-09-13: this item was already understood correctly.

**The obstacle is component structure.** The interval lives inside the chart
component. The slider lives in the panel above it. Nothing connects them, and
state flows the wrong way.

**Change.** Lift the interval into the panel and make the chart a controlled
component. This is a small, conventional refactor, and there is already a
precedent in the same file: the velocity chart is *already* written to accept the
interval as a prop, but the panel never passes it, so that chart is permanently
stuck on one minute. Lifting the state fixes that bug for free.

Then the step maths take the step size as an argument instead of the literal 5.

**No backend work.** Bucketing is entirely client-side; the endpoint returns raw
one-minute arrays and has no interval parameter. The replay cursor is already
minute-resolution, and every interval in the list is a whole number of minutes, so
all of them are expressible.

**Two decisions.**

1. **Full Day keeps one-minute steps** (decided 2026-09-13). Full Day is not a
   coarse bucket; it is a cumulative-since-open view over the one-minute data, and
   its value genuinely changes every minute, so scrubbing it minute by minute is
   the point.
2. **A one-minute step means many more positions**, and every move refetches. I
   will debounce the slider so dragging does not fire hundreds of requests.

**A latent bug I will fix while in there.** The slider tops out at 15:30, but the
session has closed at 15:40 since August. The end of the range is currently
unreachable. The maximum will derive from the session close rather than a
hard-coded number.

**Verify.** Each interval produces the right step size and the right number of
positions. Switching interval while scrubbing re-snaps the position to the new
grid so the thumb and the label agree. The slider reaches 15:39.

---

## Part 6 — UMP replay shows movement inside the candle

**Today.** The playhead is a whole-candle index, and each step reveals a finished
candle. At five-minute display, an entire five-minute bar appears at once.

**The mechanism already exists in the chart.** Live mode already grows a forming
bar incrementally, and the code even documents the exact failure we must avoid:

```tsx
// UmpTvChart.tsx:304-306
// Calling setData on every render is what stopped the forming candle from
// ever animating: each repaint discarded the in-progress bar.
```

That forming-bar path is simply disabled during replay. So the shape of the fix
is to synthesize a forming bar during replay and feed it through the path that
already works.

**But the data is not in the payload.** This is the one item that needs a backend
change. When the chart shows five-minute candles, the response contains only
five-minute candles. The engine histories are minute-stamped but carry levels and
state, not prices, so a price path cannot be reconstructed from them. The
one-minute series exists server-side on every request and is simply never sent.

**Recommended approach.** Add a sub-candle series to the same response, only for
intervals of a minute or longer, and only when the replay flag is set, which is
already replay-only. That keeps the core design intact: one fetch per replay
session, every playhead move a pure client-side derivation with no network call.
The alternative, a second request at one-minute resolution, needs no backend
change but doubles a genuinely expensive server-side replay and adds a
consistency problem between two payloads. I recommend the first.

**Changes.**
1. Backend: return the one-minute series alongside the display candles under the
   replay flag. It must be built from the **same** filled series that produced the
   display candles, or on a thin strike the sub-candles will not tile the candles
   they belong to and the forming bar will visibly disagree with the closed one.
2. The playhead gains a sub-position: which closed candle, plus how far into the
   next one.
3. The forming bar is folded from the sub-candles inside the current bucket:
   first open, running high and low, latest close, stamped at the bucket start so
   the chart replaces rather than appends.
4. The closed-candle slice stays exactly as it is, so the series is not rebuilt on
   every frame. This is the specific trap the comment above warns about.
5. Playback speed currently means display bars per second. It becomes smooth
   sub-steps, and the label is updated to match.
6. The scrubber and the clock move to sub-candle resolution, so the clock ticks
   every minute instead of every five.

**No look-ahead, and in fact stricter than today.** The existing visibility rule
generalises directly: a fact is visible once its bar has closed. With one-minute
sub-candles an entry at 10:35 appears at 10:36 rather than being withheld until
the five-minute bar closes at 10:40. That is more faithful, not less.

**Verify.** A replay at five-minute display shows each candle growing through five
one-minute steps. Pausing mid-candle shows a partial bar. No event, level or trade
appears before the engine could have seen it. The determinism harnesses stay green,
since this is a display path.

---

## Verification for the whole batch

| check | expectation |
|---|---|
| Backend unit suite | all pass, currently 536 |
| `validation/backtest_parity.py` | 633/633 |
| `validation/backtest_golden.py` | PASS, both halves |
| `scripts/stack_scenario_sweep.py` | 180/180 |
| Frontend `npm run build` and `npm run lint` | clean |
| Browser, Algo Config and Backtesting | every changed surface |
| Backtest before and after | only Part 4 should move it |

Part 4 is the only change expected to alter trading results. If any other part
moves the backtest, something is wrong and I will stop and report it.

---

## Decisions I need from you

1. ~~Part 4 changes real entries~~ — **CONFIRMED 2026-09-13: yes, proceed.**
   Originally: This is about the RULE using Lowest-OI Side; removing the Ratio
   Side column from the table is display only and changes nothing.
2. ~~OI Change toggles~~ — **settled by the reference**: two Structure toggles,
   pure display, remembered in the browser.
3. ~~Red-candle footer~~ — **settled by the reference**: last fully closed candle.
4. ~~Full Day replay~~ — **CHOSEN 2026-09-13: 1 minute per notch**, about 385
   positions, with the request debounced while dragging.
5. **New**: does "all charts customizable" mean four extra show and hide toggles
   on top of the reference's two Structure toggles, or was that the Structure
   toggles all along?

Everything else I will build as described above.
