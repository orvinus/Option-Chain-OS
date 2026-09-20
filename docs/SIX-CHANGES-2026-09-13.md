# Six changes — implementation report

**2026-09-13.** Plan: `docs/PLAN-2026-09-13-six-changes.md`.
Local only: nothing committed, pushed, or sent to the VPS.

---

## 1. SENSEX expiries

**Was.** Picking SENSEX for a day still listed NIFTY expiries.

**Cause.** `DailyConfigPanel.tsx` asked for one hard-coded symbol while the
symbol selector 250 lines below wrote a per-day `index_symbol`.

**This bug had already been found and fixed once**, in August, on the engine-tabs
dropdown. Its comment even names the date. The Daily Trading Config strip was
simply missed, so the fix was to apply a proven pattern rather than invent one.

**Now.** The strip follows the selected day's symbol, refetches on change, is
labelled with the symbol, and a slow response can no longer overwrite a newer
one. One panel serves both Algo Config and Backtesting, so both are fixed.

**Verified in the browser, both pages:**

| symbol | strip shows |
|---|---|
| SENSEX | 17 Sep Thu, 24 Sep Thu, 1 Oct Thu |
| NIFTY | 15 Sep Tue, 22 Sep Tue, 29 Sep Tue |

The network trace confirms the request itself changes symbol, and the "traded"
marker moves to the right contract for each.

---

## 2. Change the sign-in ID and password, stored in the backend

**The store already existed.** `algo_users` holds a unique username and a PBKDF2
hash, and the environment values only ever seeded it **when the table was empty**.
A changed password would already have survived a restart.

**The real blocker** was two guards that refused all access unless the
environment variables were still present, even with a valid user row. That is
what pinned the secret to disk. Both now ask the database instead:

```python
async def auth_configured() -> bool:
    if await user_count() > 0:
        return True
    return bool(settings.algo_admin_user and settings.algo_admin_password)
```

A stack with neither a user row nor env values still fails loud, so
misconfiguration never becomes an open door.

**New endpoint** `POST /api/algo/auth/change-credentials`, behind a valid
session. It re-authenticates the current pair against the SIGNED-IN user's own
row, so a session for one account can never rewrite another. Refusals for a wrong
password and for a right-password-wrong-account are worded identically, so the
response cannot be used to probe which IDs exist. Repeated failures back off. The
change is audited by username, never by password, and the session is cleared on
success so the new credentials are proven immediately.

**UI.** A "Change sign-in ID & password" card in Security & Backups, in the
stated field order, with the scope written on it: these credentials gate Algo
Config and Backtesting, and the dashboard sign-in is separate and untouched.

**Consequence worth knowing.** Once changed, the values still sitting in the
environment file are stale and can be deleted. My own test harnesses read them to
sign in, so they would need the new ones; that affects tooling, not the product.

---

## 3. OI Change is now four charts

**The reference was missing.** Three files in the codebase cite
`oi_smc_engine.html`, but it had never been in the repo, in git history, or on
this machine. You supplied it, and it is now archived at
`docs/reference/oi_smc_engine.html`. It settled several details I would have
guessed at: 220px charts rather than 500, exactly two toggles rather than six,
and the exact footer wording.

**Now**, a two by two grid:

| position | chart | extras |
|---|---|---|
| top left | Call OI Change · 5 Min, red candles | footer `Last candle: RED -> routes to Call` |
| top right | Put OI Change · 5 Min, blue candles | footer `Last candle: RED -> routes to Put` |
| bottom left | Call OI Change · 1 Min, red line | swing labels, zone, `· 39 swings`, Structure toggle |
| bottom right | Put OI Change · 1 Min, blue line | swing labels, zone, `· 41 swings`, Structure toggle |

The timeframe pill bar is gone entirely, including 10, 15 and 30 minute.

**No backend work.** One call already returned everything per side; the 5-minute
candles are folded in the browser from the same 1-minute series.

**The chart component hard-coded both sides in about ten places** and now takes a
side. Five things that would have gone wrong silently:

1. The candles were offset half a width left and right so two sides could share
   an axis. With one side per chart that offset is zero and the candle is wider,
   or they render as thin sticks pushed off centre.
2. Each chart has its own drawing key, or all four share one store.
3. Height is a prop now, and the legend is hidden, or four charts would not fit.
4. The zero line was inside the structure overlay. It is now separate, so turning
   Structure off does not take the axis reference with it.
5. **The red-candle marker was on the wrong candle.** It pinned the last candle,
   which may still be forming and whose colour can still flip. The engine reads
   the last FULLY CLOSED candle, and so does the reference. The marker now
   matches, so the chart can no longer show a signal the engine never acted on.

**Caught during visual verification:** the 5-minute charts were also drawing
swing labels and the range zone. Those are computed on the 1-minute series, so
plotting them over 5-minute buckets maps the indices onto the wrong candles. The
reference draws structure on the 1-minute charts only, and now so do we.

**Toggles** are pure display state remembered in the browser, so hiding structure
never marks your trading config unsaved.

### Two follow-ups from the browser (2026-09-14)

**The Algo Payload card is hidden.** It printed the raw JSON handed to the
execution layer and was not wanted on the page. Commented out rather than
deleted, marked `HIDDEN-2026-09-13` in `OiStructurePanel.tsx`, with the row
above it switched from a two-up grid to a single column so Contributing Events
now spans the width. Display only: the payload is still computed and still
returned by the endpoint, so un-commenting that one block is the whole restore.

**The Structure toggle overlapped the drawing toolbar.** I had placed it with
`absolute right-4 top-4`, but the chart header's top-right corner already
belongs to the drawing toolbar, so the two sat on top of each other. Anything
floated into that corner will collide with it.

It is now a laid-out slot instead of a floated sibling: the chart takes a
`toggle` node and renders it inside the header's right-hand group, to the left
of the toolbar. Measured after the fix:

| chart | gap to first toolbar button | overlaps |
|---|---|---|
| Call OI Change · 1 Min | 19px | no |
| Put OI Change · 1 Min | 19px | no |
| both 5-Minute charts | no toggle by design | no |

---

## 4. Multi-TF filters on the Lowest-OI Side

**Two edits, and Ratio Side is gone from that page.**

The rule condition now matches the **Lowest-OI Side**, the lower signed change,
which is the platform's dominant-side rule. That is one comparison in the engine:

```python
if cond.side != "Any" and r.lowest_side != cond.side and r.lowest_side != "Neutral":
```

The positions table drops the **Ratio Side** column and keeps **Lowest OI Side**.
The Call : Put column stays, because the rule's Above/Below threshold reads it.

**No migration.** The stored field is named generically, so every saved config and
every frozen backtest run loads unchanged.

**This changes trading behaviour, as agreed.** Measured over four real sessions,
every cursor position, every timeframe:

| timeframe | readings that differ |
|---|---|
| 1m | 29.5% |
| 5m | 28.3% |
| 15m | 25.1% |
| full day | 0.0% |
| **overall** | **21.8% of 15,360** |

**A second, subtler effect.** A Neutral side still passes any side filter, kept
from the reference. But Neutral is now much rarer: it requires the two signed
values to be exactly equal rather than equal in magnitude. A reading that used to
slip through a Call filter on a mirror image now hard-fails.

**Golden fixtures.** The row maths are untouched, so the reference-parity test
still holds to the letter. Only rule outcomes could move, and **exactly one of
eleven scenarios did**. It is worked through by hand in the test: the 1-minute
row has the smaller magnitude on Call but the lower signed value on Put, so the
first rule no longer matches and evaluation falls through. Both expectations are
kept in the fixture, the reference result and the intended one, and the test
pins the divergence set so a future edit cannot quietly move more.

This also makes the platform guide correct, which already claimed this engine
used the signed dominant side.

---

## 5. Ratio replay follows the selected interval

The slider stepped at a fixed 5 minutes. It now steps at whatever the
PCR-over-time chart is set to.

**Verified in both pages:**

| selected | step |
|---|---|
| 5m | 5 minutes |
| 30m | 30 minutes |
| Full Day | 1 minute |

**Full Day is 1 minute by your choice**, and it is the right one on the data:
Full Day is not a bucket, it is a cumulative view that changes every minute.

**Two things fixed along the way.** The interval lived inside the chart while the
slider lived in the panel above it, so the interval was lifted and the chart made
controlled. That also fixed a pre-existing bug: the velocity oscillator already
accepted an interval but nothing ever passed one, so it was stuck on 1 minute
while the chart above it could be on 30.

And the slider's end was hard-coded to 15:30, so on every date since the close
moved to 15:40 the last ten minutes were unreachable. The range now derives from
the session close.

At a 1-minute step a drag crosses ~385 positions, so the fetch is debounced while
the thumb and the label keep tracking the drag.

### Follow-up fix, same day: the leftmost position could not be evaluated

Reported from the browser: dragging the replay fully left showed a red
`no data at or before 09:15 IST` and blanked the page.

Reproduced at the API, and it is not a display problem:

| cursor | result |
|---|---|
| 09:15 | **404** no data at or before 09:15 IST |
| 09:16 | 200, 2 points |
| 09:20 | 200, 6 points |

A ratio needs **two** closed buckets, so the session open itself can never be
evaluated. Step 0 was therefore always a dead position. It was reachable before
this work too, but 5-minute steps made it an unlikely place to land and
1-minute steps made it easy.

Two fixes, because the first alone is not enough:

1. **The slider floor is now step 1**, the first evaluable instant: 09:16 at a
   1-minute step, 09:20 at 5 minutes. Re-snapping on an interval change clamps
   to the same floor.
2. **The too-early case now reads as guidance, not failure.** A day whose series
   genuinely starts late will still hit it, and the message now says the ratio
   needs two closed buckets and to drag later into the session, rather than
   showing a raw error over an empty page.

Verified in the browser by dragging fully left on each interval:

| interval | leftmost | evaluates |
|---|---|---|
| 1m | 09:16 | yes |
| 5m | 09:20 | yes |
| Full Day | 09:16 | yes |

All requests 200, no error text, no JavaScript errors.

---

## 6. UMP replay shows movement inside the candle

**Was.** Each candle appeared complete, in one jump.

**The chart already knew how to grow a bar** — that is how the live forming
candle works — but the path was switched off during replay, and the payload
carried nothing finer than the displayed candles. The engine histories are
minute-stamped but carry levels and state, not prices, so no price path could be
reconstructed from them.

**Backend.** The response now carries a 1-minute sub-series, built from the SAME
filled series the display candles fold. Verified at the API:

| interval | history | candles | display | sub-candles | sub interval |
|---|---|---|---|---|---|
| 5m | true | 539 | 300s | 2695 | 60s |
| 15m | true | 182 | 900s | 2695 | 60s |
| 1m | true | 2695 | 60s | 0 | none |
| 5m | false | 539 | 300s | 0 | none |

539 × 5 = 2695, so they tile exactly. It is sent only during replay and only when
the display is coarser than a minute, so live and backtest responses are
unchanged.

**Frontend.** The playhead gained a sub-position, the forming bar is folded from
the sub-candles and stamped at the bucket start so the chart replaces rather than
appends, and the closed slice is untouched so the series is not rebuilt every
frame. That last point is the specific trap the chart's own comment warns about.

The step buttons and the clock move by sub-candle too, so stepping reveals the
bar growing rather than jumping a bucket.

**Verified in the browser.** On a 5-minute display the replay clock steps
**09:25 → 09:26 → 09:27 → 09:28**, one minute at a time, with no JavaScript
errors.

**No look-ahead, and stricter than before.** A fact becomes visible once its bar
closes. With 1-minute sub-candles an entry at 10:35 now appears at 10:36 instead
of being withheld until the 5-minute bar closes at 10:40.

### Follow-up fix (2026-09-14): the entry marker was drawn one candle early

Reported from the browser: during replay an entry appeared one candle to the
left, then silently jumped to the correct candle a few steps later.

**Cause, and it was introduced by this feature.** Markers snap to a display
candle via `snapToCandle`, whose domain was the CLOSED slice only. The forming
candle is painted by `series.update()` and is deliberately absent from that
slice — rebuilding it every frame would discard the growing bar. So an event
inside the forming candle had no bar of its own to attach to and snapped BACK to
the previous one, then moved when its candle closed and joined the slice.

Sub-candle visibility is what exposed it: an event becomes visible as soon as its
own minute closes, which is now up to four minutes before its 5-minute candle
does. Before this feature the candle appeared whole, so the gap never existed.

**Fix.** The forming bucket joins the snap domain. It is a real bar on the chart,
so a marker may legitimately attach to it. Visibility is unchanged — an event
still only appears once the engine could have seen it — so the marker is now
placed on its true candle from the first frame it is shown.

**Verified against the real payload**, replaying every playhead position and
comparing each marker's assigned candle with the bucket that actually contains
its timestamp:

| marker placements checked | 47,805 |
|---|---|
| wrong candle, before | **305** |
| wrong candle, after | **0** |

Example from the run: an `S2A` entry stamped 15:08 belongs on the 15:05 candle;
the old logic drew it on 15:00 until 15:10, the new logic draws it on 15:05
immediately.

---

## Verification

| check | result |
|---|---|
| Backend unit suite | **559 passed** (was 536; 23 new) |
| `validation/backtest_parity.py` | **633/633** |
| `validation/backtest_golden.py` | **PASS**, both halves |
| Frontend `tsc` and production build | clean |
| Browser, Algo Config + Backtesting | **0 problems, 0 JS errors** |
| `scripts/stack_scenario_sweep.py` | 179/180, see below |

**The one sweep failure is environmental, not ours.** It is
`audit covers sweep writes — missing=['broker_test_login']`. That audit row is
only written when a broker login succeeds, and the broker's own endpoint returns
404 out of hours:

```
broker test-login: 502 {"detail":"interactive login failed: <html>… 404 Not Found …"}
```

The sweep's adjacent check explicitly tolerates a 5xx off-hours; only the
audit-coverage check does not account for the same condition. Nothing in this
work touches that path.

**Two pre-existing issues found, neither introduced here.** The frontend lint
script cannot run because the repo has no ESLint config file, so verification
used the TypeScript compiler and the production build instead. And the Ratio
replay slider could never reach the last ten minutes of a session, which is fixed
in Part 5.

---

## Files

**Backend.** `app/algo/auth.py`, `app/api/algo_auth.py` (credential store and
change endpoint) · `app/algo/engines/mtf_ratio.py`, `app/algo/config_models.py`
(side filter) · `app/api/algo_engines.py` (replay sub-candles).

**Frontend.** `DailyConfigPanel.tsx` (SENSEX) · `SecurityPanel.tsx`,
`api/algoRest.ts` (credentials) · `OiStructurePanel.tsx`,
`OiStructureTimeChart.tsx` (quad charts) · `MtfRatioPanel.tsx`,
`DecisionTrace.tsx` (side) · `MqaePanel.tsx`, `MqaeRatioChart.tsx` (replay step)
· `UmpPanel.tsx`, `umpReplay.ts`, `types/algo.ts` (intra-candle replay).

**Tests.** `test_algo_credentials.py` and `test_ump_subcandles.py` are new;
`test_mtf_ratio_golden.py` and its fixture were rescoped.

**Reference.** `docs/reference/oi_smc_engine.html` archived from your copy.
