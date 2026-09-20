# Live broker burn-in — Lakshmishree (XTS Interactive), from the Algo Config UI

**2026-09-15, Tuesday (NIFTY weekly expiry), 14:12–14:55 IST.** Local stack only.
Nothing committed or pushed. All actions driven from the Algo Config page in
Google Chrome; screenshots are in this folder.

## Result in one line

The first real orders through this adapter **worked end to end from the UI**: a
₹971.75 BUY filled in 1.9 s, the engine took over management, the broker and
the engine agreed on the position, and a Square-off SELL filled in 0.3 s.
Net cost of the test: **−₹210.69**. Paper Mode was restored at 14:52.

---

## What was built for the test

Neither control existed — the UI had no way to place or close a position by hand.

| | |
|---|---|
| **Place test entry** | Picks the nearest strike in a ₹0.5–₹2 band and sizes it to **≤ ₹1,000 notional** (hard server cap, the UI cannot raise it). Uses the engine's own `execute_entry` (paper or live per Paper Mode), records the ledger row, hands the position to the orchestrator. |
| **Square off** | Latches a `MANUAL_SQUARE_OFF` exit and runs the engine's own `_manage_position` exit code immediately. A failed live sell keeps the latch, so the per-minute loop retries it — the same guarantee as an engine exit. |
| Safety | Both share a lock with the per-minute engine pass, so a manual order can never interleave with an engine entry/exit. Paper Mode OFF demands an explicit confirmation (dialog + server check). |

Files: `backend/app/algo/orchestrator.py` (`manual_test_entry`, `manual_square_off`,
`_pass_lock`), `backend/app/api/algo_trades.py` (`/algo/manual/test-entry`,
`/algo/manual/square-off`), `frontend/src/components/algo/MiscPanels.tsx`
(`ManualOrderTestCard`, Integrations & Broker tab). 565/565 backend tests pass.

## Config used (and restored)

One save (v233) so the size cap and the live switch landed together — the live
config otherwise sizes from **₹3,00,000 × 100 %**, which at ₹1 premium would
have been ~4,600 lots:

| setting | v232 (normal) | v233 (burn-in) |
|---|---|---|
| Paper Mode | ON | **OFF** |
| Demat balance (sizing base) | ₹3,00,000 | **₹1,000** |
| Tue Z3 premium band | ₹52–₹88 | **₹0.5–₹2** |
| Tue Z3 filters | OI Change + Multi-TF + Ratio | **OI Change only** |
| Tue Z3 max trades | 1 | 5 |

**Restored at 14:52:44 as v234 (= v232).** Engine back on the paper ledger from 14:53.

---

## Step 1 — Paper rehearsal (14:37)

`paper_2_entry_result.png`, `paper_5_exit_result.png`

| | |
|---|---|
| Trade | #5 [paper] NIFTY 23600 CE, 11 × 65 = 715 qty @ ₹1.31 (₹936.65) |
| Square-off | ₹1.34, MANUAL_SQUARE_OFF, net −₹20.45 |
| Entry latency | 7.2 s — **6.9 s waiting for the engine's per-minute pass** (the lock), 0.25 s order |
| Exit latency | 0.37 s |

## Step 2 — LIVE entry (14:44:53)

`live_2_entry_result.png`, `live_4_account_after_entry.png`

| | |
|---|---|
| Trade | **#6 [live] NIFTY 23600 CE**, exp 15 Sep 2026 |
| Decision → fill | ₹1.10 → **₹1.15** |
| Size | **13 lots × 65 = 845 qty, ₹971.75** |
| Broker order | **1210407205 — BUY 845 Market Filled @ 1.15**, NRML |
| Latency | click → result **1,944 ms**; lock 0 ms · strike 14 ms · **order placed + fill confirmed 1,908 ms** · ledger 6 ms |

## Step 3 — Position open (14:45–14:49)

`live_r1_reconcile.png`, `live_r4_pnl_live.png`, `live_3_header_in_trade.png`

| check | result |
|---|---|
| Engine management | picked up at the next pass: 14:45 ₹1.10 (−₹42.25), 14:46 ₹1.15, 14:47 ₹1.00 (−₹126.75) |
| **Reconcile positions** (UI) | ✔ broker holds 845, engine expects 845 (instrument 47301) — **247 ms** |
| Broker margin | utilised ₹971.75 / available ₹28.25 (took a few minutes to update — showed ₹0 utilised right after the fill) |
| LIVE ledger trade log | shows the row as **open**: 14:44 @ 1.15, 13 lots, MANUAL_TEST |

## Step 4 — LIVE square-off (14:49:46)

`live_5_exit_result.png`, `live_6b_account_after_exit.png`, `after_0_account_before.png`

| | |
|---|---|
| Broker order | **1210407206 — SELL 845 Market Filled @ 0.95** |
| Broker net position | **0** |
| P&L | **−₹210.69** (−₹169 price, −₹41.69 charges) — matches broker margin utilised ₹169 + charges |
| Latency | click → result **367 ms**; exit order + fill **315 ms** |

## Step 5 — Safety gate fired (14:50)

The loss exceeded Tuesday's max daily loss (20 % of ₹1,000 = ₹200). At the next
pass the engine logged **"max daily loss breached — day auto-killed"** and
blocked all new entries. Correct behaviour.

The natural engine entry ("real rules") was therefore **not** tested live — the
day was killed, and the account had only ₹831 margin left. Stopped at the
user's decision.

---

## Defects and gaps found

| # | Where | What | Severity |
|---|---|---|---|
| 1 | Header strip | **An open live position is not shown** — no contract, entry, lots or running P&L. Only "LIVE ≠ TRADED" and "TRADED CALL @ 14:44". | High — the one place you look |
| 2 | Header strip | The LIVE readings show all three filters even when the zone uses only one (showed "NO_TRADE: oi CALL · mtf NO_TRADE · ratio CALL" while the engine used OI Change alone). | Medium — misleading |
| 3 | Live Account card | Unrounded money: **₹28.250000000000114**, **₹971.7499999999999**, ₹831.0000000000001. | Low — cosmetic |
| 4 | Live Account card | First Refresh timed out ("transport failure: ReadTimeout", 13.7 s) before the session was warm. | Low |
| 5 | Live Account card | Broker MTM / RealizedMTM stay 0.00 and margin utilised lagged a few minutes after the fill — the broker's own reporting, not ours. | Info |
| 6 | P&L Summary tiles | Count only closed trades; the open live trade appears only in the Detailed Trade Log as "open". | Medium |
| 7 | Paper Trading tab | Shows "OPEN POSITION flat" during a live position (it tracks the paper session only). | Info |
| 8 | Manual entry | Can wait up to ~7 s for the engine's per-minute pass before placing the order. Order itself 1.9 s live. | Info — by design |
| 9 | Exit card | "Reference price ₹1.15" is the last stored minute close; the market was ₹0.95. It is only a paper-fill fallback, but reads like a quote. | Low |

## Side effects to know about

- **Paper ledger:** rehearsal trade **#5 (−₹20.45)** is in today's PAPER ledger and
  used up Tuesday Z3's `max_trades = 1`, so the paper engine could not take a Z3
  entry after 14:37 today.
- **Live ledger:** trade **#6 (−₹210.69)** is recorded; it will appear in live P&L.
- **Broker account:** cash ₹1,000, net margin ₹831 after the test.
- **Config history:** v233 (burn-in) and v234 (restore of v232) are in the version log.

---

## Follow-up: the UI defects, fixed (same day, 15:02–15:20)

Verified in Chrome with paper positions (trades #7 and #9) — every check passes,
no JS errors. Backend 573/573 tests (8 new in `tests/test_manual_orders.py`).

| # | Defect | Fix | Proof |
|---|---|---|---|
| 1 | Header strip showed no open position | New chip: `IN TRADE · PAPER CALL 23600CE × 13 in 1.11 → 1.10 −₹8.45 (-0.9%) #9`; clears the moment the position closes | `fix_1_header_position.png` |
| 1b | **Hidden:** the stream marked the position at the strip's scoped strike (23150CE ₹84.70), not its own contract — a phantom **+₹70,599.75** on the burn-in trade | Priced from the position's own newest tick (`series.latest_contract_quote`) → −₹169.00 | `test_strip_prices_the_position_at_its_own_contract` |
| 1c | **Found while verifying:** after a manual Square off the chip stayed up to a minute | `manual_square_off` clears `status.position` immediately | `test_square_off_uses_the_newest_tick_and_clears_the_strip` |
| 2 | Readings listed all three filters | Only the zone's enabled filters, labelled with the zone they belong to, plus "engine on Tue Z3" when the strip is scoped elsewhere | strip text `oi CALL · mtf NO_TRADE · ratio CALL (Tue Z1) · engine on Tue Z3` |
| 3 | Unrounded money; 13.7 s cold refresh; broker MTM 0.00 | ₹ formatted to 2 dp; the three broker reads run in parallel and a timed-out READ is retried once (orders never); new "Last price (ours)" and "MTM (ours, gross)" columns = SellAmount − BuyAmount + open qty × our last tick; note that broker margin lags | refresh **499 ms / 1.4 s** (fetched in 261 ms); burn-in row MTM **−₹169.00**; `fix_3_broker_card.png` |
| 4 | P&L tiles counted closed trades only; Paper tab said "flat" during a live trade | "Open position" card (contract, lots, entry, last price, unrealized ₹/%); Total Trades reads "1 closed · 1 open"; the open log row shows running P&L; Paper tile shows "LIVE trade #N open — not part of this paper session" | `fix_4_pnl_summary_open.png`, `fix_4b_paper_tab.png` |
| 5 | Exit "Reference price" was the last closed minute | "Last traded price before the sell ₹1.65 @ 15:18:14" (newest tick, with its time), "Exit fill (broker)", "Slippage vs last price" | `fix_5_exit_card.png` |

Note: the margin lag itself (broker RMS) and the broker's 0.00 MTM fields are the
broker's own reporting and cannot be changed from here — the card now shows our
own figures next to them.
