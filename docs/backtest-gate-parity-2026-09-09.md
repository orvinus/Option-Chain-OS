# Backtest ↔ live gate parity — benchmark rerun (2026-09-09)

User-approved change: the backtest now applies the three gates live always had
— the 180-second feed-staleness gate, the platform NSE-holiday union, and the
date's own session close (15:30 before 2026-08-03, 15:40 since) — and records
data gaps before execution.

## Method

Run **#112** (frozen inline config "sdfc", 2026-02-05 → 2026-09-09, compounding,
₹30,000, min 300 min/day, default exclusions) was re-run as **#118** with the
identical frozen document and settings on the new code (only the range's last
day, 2026-09-09, is new data). Per-day comparison from `algo_backtest_days`.

## Result

| | #112 (old code) | #118 (new code) |
|---|---|---|
| days run / skipped | 112 / 43 | 140 / 15 |
| trades | 84 | 117 |
| net P&L | −₹5,196.08 | −₹7,694.76 |
| fees | — | ₹7,836.46 |
| decision rows | — | 52,850 (accept 117 · manage 1,257 · reject 48,972 · skip 2,504) |
| `stale_data` minutes | n/a (gate absent) | 180 |
| days with ≥3-min gaps | — | 4 (07-09, 07-10 ×2, 08-18, 09-07 15:09–15:38) |
| days on the 375-minute session (pre-2026-08-03) | — | 119 |

**On the 112 days both runs simulated:** trades 84 = 84; net −₹5,196.08 → −₹4,772.86;
**109 days byte-identical, 3 days differ** (all after the 2026-08-03 close change):

| day | #112 trades / net | #118 trades / net | why |
|---|---|---|---|
| 2026-08-24 | 2 / −1,090.67 | 2 / −1,345.14 | staleness gate / 15:40 boundary changed an exit minute |
| 2026-08-31 | 2 / −266.67 | 2 / +44.78 | same |
| 2026-09-07 | 2 / −1,359.82 | 2 / −993.58 | 30-minute data hole 15:09–15:38 → `stale_data` blocks re-entry; exit differs |

Every pre-2026-08-03 day is identical → the 385→375-minute frame change produced
no output difference on those days (the ten phantom slots never carried bars),
exactly as predicted in the plan.

**28 extra days ran** (33 trades, −₹2,921.90): these were wrongly skipped by #112 as
"live/archive duplication risk (refresh live_days)" — the pre-0013 presence rule
(QA H3). The new preflight keys the risk on `oi_day_stats`; all 28 are legitimate
archive-won days.

## Platform holidays

`settings.platform_holidays` froze as **empty** for #118 — verification showed the
Docker image never shipped `data/nse_holidays.json` (only `symbols.json`), so
`known_holidays()` was empty inside every container: the live orchestrator's
holiday union, the gap-fill day grid and the hold-last window have never seen a
holiday in Docker. Fixed in `docker/Dockerfile.backend` (+ test
`test_image_ships_holidays.py`). The three listed dates (2026-10-02, 2026-12-25,
2027-01-26) are all after this run's range, so #118's numbers are unaffected.

## Reproduce

```
POST /api/algo/backtest/runs  {config:{source:"inline",document:<#112 config>}, settings:<#112 settings>}
GET  /api/algo/backtest/runs/{id}/decisions?compact=true
SELECT trade_date, detail->>'trades', detail->>'net' FROM algo_backtest_days WHERE run_id IN (112,118)
```
