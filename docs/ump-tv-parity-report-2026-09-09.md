# Pine ↔ platform parity — 2026-09-09 17:26

TV export: `logs/parity/tv_logs.txt` · our dumps: `logs\parity5_vl73` · alignment key = (5-minute bar, event kind)

## Totals

- contracts: 20
- daily candles (D1..D3) exact: 60/60 · weekly exact: 19/20
- 1H reversal arrays identical: 17/20 · level sets identical: 18/20
- events: TV 1396 · platform 1366 · matched (bar+kind) 1274 (91.3% of TV) · exact (bar+kind+value) 1255
- diffs: LABEL 1 · VALUE 18 · TV-only 122 · platform-only 92 · timing (±1 bar) 10
- root-cause tags: {'vendor-tick-high': 7, 'first-week-divergence': 8, 'truncated-capture': 2}

## Per contract

| Contract | TV events | platform events | matched | exact | LABEL/VALUE | TV-only/platform-only/timing | first structural diff | root-cause tags |
|---|---|---|---|---|---|---|---|---|
| NIFTY260908C23700 | 23 | 21 | 17 | 17 | 0/0 | 6/4/0 | TV_ONLY 08-25 09:15 ENTRY:R1 (after 0 TV events) | — |
| NIFTY260908C23750 | 13 | 13 | 13 | 13 | 0/0 | 0/0/0 | none | — |
| NIFTY260908C23800 | 53 | 58 | 51 | 49 | 1/1 | 2/7/0 | TV_ONLY 08-25 10:50 TRAIL_SET (after 1 TV events) | vendor-tick-high |
| NIFTY260908C23850 | 31 | 31 | 29 | 28 | 0/1 | 2/2/0 | TV_ONLY 08-26 11:00 ENTRY:S3A (after 0 TV events) | vendor-tick-high |
| NIFTY260908C23900 | 58 | 56 | 54 | 52 | 0/2 | 4/2/0 | TV_ONLY 08-20 09:20 ENTRY:S3B (after 0 TV events) | first-week-divergence, vendor-tick-high |
| NIFTY260908C23950 | 50 | 53 | 48 | 47 | 0/1 | 2/5/1 | PLATFORM_ONLY 08-25 15:15 TRAIL_SET (after 5 TV events) | vendor-tick-high |
| NIFTY260908C24000 | 89 | 91 | 89 | 89 | 0/0 | 0/2/0 | PLATFORM_ONLY 08-21 13:50 ENTRY:S2A (after 14 TV events) | — |
| NIFTY260908C24050 | 64 | 57 | 53 | 53 | 0/0 | 11/4/1 | TV_ONLY 08-20 09:15 ENTRY:R1 (after 0 TV events) | first-week-divergence |
| NIFTY260908C24100 | 88 | 78 | 75 | 75 | 0/0 | 13/3/2 | TV_ONLY 08-19 14:35 ENTRY:S2A (after 0 TV events) | first-week-divergence |
| NIFTY260908C24150 | 70 | 64 | 62 | 58 | 0/4 | 8/2/1 | TV_ONLY 08-19 14:35 ENTRY:S2B (after 0 TV events) | first-week-divergence, vendor-tick-high |
| NIFTY260908P23700 | 27 | 25 | 25 | 25 | 0/0 | 2/0/0 | TV_ONLY 08-13 14:10 ENTRY:S2B (after 0 TV events) | — |
| NIFTY260908P23750 | 19 | 19 | 19 | 17 | 0/2 | 0/0/0 | none | — |
| NIFTY260908P23800 | 45 | 38 | 38 | 38 | 0/0 | 7/0/0 | TV_ONLY 08-18 15:30 ENTRY:R1 (after 0 TV events) | first-week-divergence |
| NIFTY260908P23850 | 42 | 30 | 30 | 30 | 0/0 | 12/0/0 | TV_ONLY 08-21 09:15 ENTRY:R1 (after 0 TV events) | first-week-divergence |
| NIFTY260908P23900 | 111 | 101 | 101 | 101 | 0/0 | 10/0/0 | TV_ONLY 08-20 11:40 ENTRY:S2A (after 0 TV events) | first-week-divergence |
| NIFTY260908P23950 | 123 | 121 | 121 | 121 | 0/0 | 2/0/0 | TV_ONLY 09-02 11:10 ENTRY:S2A (after 102 TV events) | — |
| NIFTY260908P24000 | 131* | 154 | 115 | 111 | 0/4 | 16/39/4 | TV_ONLY 08-14 14:05 ENTRY:R1 (after 0 TV events) | truncated-capture, vendor-tick-high |
| NIFTY260908P24050 | 115 | 116 | 109 | 106 | 0/3 | 6/7/1 | TV_ONLY 08-18 15:35 TRAIL_SET (after 1 TV events) | vendor-tick-high |
| NIFTY260908P24100 | 129* | 137 | 122 | 122 | 0/0 | 7/15/0 | TV_ONLY 08-14 13:45 ENTRY:R2 (after 0 TV events) | truncated-capture |
| NIFTY260908P24150 | 115 | 103 | 103 | 103 | 0/0 | 12/0/0 | TV_ONLY 08-18 15:35 ENTRY:S3B (after 0 TV events) | first-week-divergence |

`*` = truncated TV capture (no END marker — the Pine log limit cut the export; its trailing events are unknown).

## Filtered signals (platform-only entries → responsible filter)

n/a: no decisions export supplied (`--decisions <json>` from `/api/algo/decisions` or `/runs/{id}/decisions`).

## Tolerances (documented, not hiding misses)

- level / candle prices: 0.05% of the value with a 0.0051 floor (half a paisa above the 0.05 tick).
- event values: 0.011 (a matched pair beyond it is a VALUE diff); MAX_SL labels carry the running low and are exempt.
- time: the 5-minute bar (Pine evaluates once per completed bar on history; the engine runs 1-minute commits and the dump folds them — `--bar5`).
- kind: TRAIL_SET vs TRAIL_RAISE count as the same rung (Pine labels the first rung SET) and are reported as LABEL diffs.

## Root-cause tag legend

- **first-week-divergence** — TV's daily feed carries settlement bars for listed-but-untraded days; the vendor bhavcopy has no zero-volume rows, so the Data-Ready gate opens later on the platform (first 1–3 traded days of a contract).
- **truncated-capture** — Pine log limit cut the export before END; trailing TV events unknown.
- **timeout-semantics** — the dump's Trigger Timeout differs from the chart's (chart runs 1).
- **vendor-tick-high** — a trail value differs because the vendor's 1-minute high differs from TV's tick high.
- **realtime-vs-bar** — the dump was produced at 1-minute granularity (no `--bar5`).
