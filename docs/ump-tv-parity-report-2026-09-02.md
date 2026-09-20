# Ultra Master Pro — TradingView parity report (2026-09-02, final)

Scope: is the Pine indicator `vl72` ("NIFTY ULTRA-MASTER PRO v20") replicated
100 % by the platform's UMP engine (Algo Config live hunting, UMP dashboard,
day-replay and the backtester all share `backend/app/algo/engines/ump/engine.py`
fed by `backend/app/algo/series.py`)?

Method: the same Pine script plus a debug block (`docs/reference/ump_v20_parity_debug.pine`)
was run on the user's TradingView chart (5-minute, NSE) for **20 contracts**
(NIFTY 08 SEP 2026, strikes 23700…24150, CE and PE) and its Pine-log export
(`logs/parity/tv_logs.json`) was diffed stage by stage against our engine's
intermediates (`logs/ump_parity_dump.py` → `logs/parity*/…json`,
`logs/ump_parity_compare.py`, `logs/ump_signal_diff.py`).

Stages compared per contract: the three prior daily candles (H/L/C), the prior
weekly candle, the full 1H structural-reversal array, the final unified level
set (type + price), and every entry / trail / exit event in the contract's life
(1,396 TradingView events in total).

## 1. Result

| Stage | Before today | After the fixes below |
|---|---|---|
| Daily D1–D3 + weekly H/L/C (80 candles) | 0 / 80 exact | **79 / 80 exact** (the one miss is a vendor tick, §3) |
| 1H structural-reversal arrays (20) | 0 / 20 identical | **17 / 20 identical**, 3 differ by one reversal (vendor bars, §3) |
| Final level sets (20) | 0 / 20 identical | **18 / 20 identical** (the 2 follow from the bars above) |
| Events, all history, TV's own inputs | ~20 % aligned | **1,274 / 1,396 (91 %)** aligned by bar + kind, values to the paisa |
| Events from 26 Aug (after each contract's illiquid first week) | — | **994 / 1,007 (98.7 %)**; 12 / 20 contracts identical in both directions |

The 13 TradingView-only and 20 ours-only events left after 26 Aug (excluding
two contracts whose TradingView log capture was truncated, §3) all sit inside
a run that started from a first-week divergence or a one-tick bar difference;
none is a rule the engine evaluates differently. The engine's logic is the
Pine's.

## 2. Defects found and fixed today (all in the data path, none in the rules)

1. **Session end frozen at 15:30.** The exchange moved the F&O close to 15:40
   on 2026-08-03 (first 15:3x bars in the vendor archive). The premium folds and
   the engine still cut every day at 15:29, so the 15:30 and 15:35 candles, the
   last hourly bar and every daily close were missing. Fix:
   `core.time_utils.session_close_min(date)` / `session_last_bar_min(date)`
   (15:40 from 2026-08-03, 15:30 before) used by `series.py` and the engine.
2. **Daily close = last trade.** TradingView's daily bar closes at the NSE
   official close (bhavcopy; last-30-minute weighted average). Verified on 12
   contract-days to the paisa (e.g. 23900 PE 1 Sep: 86.40 official vs 77.40
   last trade). Fix: official H/L/C from `eod_bars` (bhavcopy) feed the daily and
   weekly candles (`series.fetch_official_closes*`, `official_day_hlc`,
   `UmpEngine(official_close=…)`); fallback when no bhavcopy row = H/L from the
   bars and the mean of the last 30 one-minute closes.
3. **Archive OHLC discarded.** The premium query read the vendor archive through
   the unified view, which exposes only `close AS ltp`, so every archived minute
   folded to o=h=l=c and highs/lows were understated. Fix: `_PREMIUM_MINUTES_*`
   read `oi_archive_bars` (real open/high/low/close) for completed days and the
   live tick table only for days the archive lacks and always for the current
   IST day.
4. **Bhavcopy rows unjoinable.** The puller stored option rows under the
   1970-01-01 sentinel expiry; 15,054 legacy rows re-stamped, puller fixed
   (`scripts/truedata_backfill.py`), repair is idempotent on every run.
5. **Bhavcopy not part of the top-up.** `pull-eod` added to the boot/nightly
   gap-fill (`ingest/gapfill.py`) and to `scripts/nightly_topup_vps.sh`.
6. **Trail label.** Pine writes "TRAIL SET" only when the trail lands on Base
   and "TRAIL ↑" for every higher rung, even from an unset trail; the engine
   labelled by "first placement". Cosmetic, now identical.

Tests: `backend/tests/test_session_close.py` (new, 8 tests), `test_premium_life.py`
updated; full suite 362 passed. Local stack rebuilt and healthy.

## 3. Residual differences and why they are not engine defects

- **TradingView inputs.** The user's chart runs the indicator with
  *Trigger Timeout = 1* (Pine default 6; read from the settings dialog). With
  timeout 6 the engine emits entries TradingView never shows; with timeout 1
  agreement is as in §1. The platform zone config must use the same value as
  the chart the user compares against.
- **Historical vs realtime execution.** On history Pine evaluates once per
  5-minute bar with the bar's final OHLC; live it evaluates on every tick.
  The engine runs 1-minute ticks (the realtime semantics — the 2026-08-18
  ruling: do not "fix"). The §1 event numbers were produced with the engine
  fed one evaluation per 5-minute bar (`ump_parity_dump.py … bar5`), which is
  the like-for-like comparison. Event prices match to the paisa in both modes;
  only the minute stamps differ in 1-minute mode.
- **Illiquid first week.** TradingView fires entries during a contract's first
  one to three traded days (e.g. 23900 CE on 20 Aug, one day after its first
  trade) that the Data-Ready gate (3 daily bars + a weekly) should block. The
  only explanation consistent with the data is that TradingView's daily feed
  carries settlement-price bars for listed-but-untraded days; the vendor's
  bhavcopy has no zero-volume rows (checked 12 and 18 Aug) so those bars cannot
  be reproduced from our sources. Effect ends after three traded days and one
  fully traded week; it never touches a strike inside the premium band.
- **Vendor ticks.** 23850 CE weekly high 648.65 on TradingView vs 645.00 in
  both the vendor 1-minute bars and the bhavcopy; two hourly arrays differ by
  one reversal from a single bar. Not reproducible from any source we hold.
- **Truncated captures.** 24000 PE and 24100 PE exports lack the END marker
  (Pine log limit); their TradingView lists are incomplete, which is where 38
  of the 58 "ours-only" events after 26 Aug come from.

## 4. Per-contract event agreement (timeout 1, one evaluation per 5-min bar)

All history → from 26 Aug: 23700CE 17/23 → 17/19 · 23750CE 13/13 → 13/13 ·
23800CE 51/53 → 49/49 · 23850CE 29/31 → 29/31 · 23900CE 54/58 → 50/50 ·
23950CE 48/50 → 43/44 · 24000CE 89/89 → 63/63 · 24050CE 53/64 → 49/51 ·
24100CE 75/88 → 69/71 · 24150CE 62/70 → 49/51 · 23700PE 25/27 → 8/8 ·
23750PE 19/19 → 19/19 · 23800PE 38/45 → 34/34 · 23850PE 30/42 → 26/26 ·
23900PE 101/111 → 81/81 · 23950PE 121/123 → 85/87 · 24000PE 115/131 → 63/63 ·
24050PE 109/115 → 88/88 · 24100PE 122/129 → 79/79 · 24150PE 103/115 → 80/80
(matched / TradingView events).

## 5. What changes for users

- UMP dashboard, day-replay, live hunts and backtests now see the 15:30–15:39
  candles, exchange-official daily/weekly candles and archive-grade highs/lows.
  Backtest results on past days will differ from runs before today — this is
  the correction, not drift. Re-run any golden/parity harness after 15:40.
- Live-day source: today's minutes still come from the live tick table; from
  the next morning the archived bars take over automatically.
- The nightly top-up and the boot gap-fill now also pull the NSE bhavcopy
  (official closes). SENSEX (BSE) has no bhavcopy from the vendor; its daily
  close uses the last-30-minute mean fallback (within ~0.5 % of the official
  print on the NIFTY sample).

## 6. Open items

- Set the platform's UMP `trigger_timeout_bars` to match the chart (1) or set
  the chart's input back to 6 before the next side-by-side session.
- A live-session comparison (chart open with the indicator during market hours,
  no reload) is the only way to compare realtime intrabar behaviour; history
  can only confirm the rules, which it now does.
- Untraded-day settlement bars would need the exchange's own UDiFF bhavcopy.
- QA defects H1–H4 / M1–M4 from `docs/qa-algo-backtest-2026-09-02.md` remain
  open; relay + gap-fill are not deployed to the VPS (needs an explicit ask).
