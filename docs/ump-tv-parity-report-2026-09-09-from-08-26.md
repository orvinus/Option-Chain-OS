# Pine ↔ platform parity — 2026-09-09 17:26

TV export: `logs/parity/tv_logs.txt` · our dumps: `logs\parity5_vl73` · alignment key = (5-minute bar, event kind) · from 08-26

## Totals

- contracts: 20
- daily candles (D1..D3) exact: 60/60 · weekly exact: 19/20
- 1H reversal arrays identical: 17/20 · level sets identical: 18/20
- events: TV 1007 · platform 1052 · matched (bar+kind) 994 (98.7% of TV) · exact (bar+kind+value) 983
- diffs: LABEL 0 · VALUE 11 · TV-only 13 · platform-only 58 · timing (±1 bar) 2
- root-cause tags: {'vendor-tick-high': 5, 'truncated-capture': 2}

## Per contract

| Contract | TV events | platform events | matched | exact | LABEL/VALUE | TV-only/platform-only/timing | first structural diff | root-cause tags |
|---|---|---|---|---|---|---|---|---|
| NIFTY260908C23700 | 19 | 21 | 17 | 17 | 0/0 | 2/4/0 | PLATFORM_ONLY 08-26 09:45 ENTRY:S2B (after 0 TV events) | — |
| NIFTY260908C23750 | 13 | 13 | 13 | 13 | 0/0 | 0/0/0 | none | — |
| NIFTY260908C23800 | 49 | 51 | 49 | 49 | 0/0 | 0/2/0 | PLATFORM_ONLY 08-26 09:25 TRAIL_SET (after 0 TV events) | — |
| NIFTY260908C23850 | 31 | 31 | 29 | 28 | 0/1 | 2/2/0 | TV_ONLY 08-26 11:00 ENTRY:S3A (after 0 TV events) | vendor-tick-high |
| NIFTY260908C23900 | 50 | 51 | 50 | 48 | 0/2 | 0/1/0 | PLATFORM_ONLY 08-26 09:20 TRAIL_RAISE (after 0 TV events) | vendor-tick-high |
| NIFTY260908C23950 | 44 | 47 | 43 | 42 | 0/1 | 1/4/0 | PLATFORM_ONLY 08-26 09:15 TRAIL_RAISE (after 0 TV events) | vendor-tick-high |
| NIFTY260908C24000 | 63 | 63 | 63 | 63 | 0/0 | 0/0/0 | none | — |
| NIFTY260908C24050 | 51 | 52 | 49 | 49 | 0/0 | 2/3/0 | PLATFORM_ONLY 08-26 09:15 TRAIL_RAISE (after 0 TV events) | — |
| NIFTY260908C24100 | 71 | 71 | 69 | 69 | 0/0 | 2/2/1 | PLATFORM_ONLY 08-26 09:15 TRAIL_RAISE (after 0 TV events) | — |
| NIFTY260908C24150 | 51 | 51 | 49 | 46 | 0/3 | 2/2/1 | PLATFORM_ONLY 08-26 09:50 ENTRY:R1 (after 3 TV events) | vendor-tick-high |
| NIFTY260908P23700 | 8 | 8 | 8 | 8 | 0/0 | 0/0/0 | none | — |
| NIFTY260908P23750 | 19 | 19 | 19 | 17 | 0/2 | 0/0/0 | none | — |
| NIFTY260908P23800 | 34 | 34 | 34 | 34 | 0/0 | 0/0/0 | none | — |
| NIFTY260908P23850 | 26 | 26 | 26 | 26 | 0/0 | 0/0/0 | none | — |
| NIFTY260908P23900 | 81 | 81 | 81 | 81 | 0/0 | 0/0/0 | none | — |
| NIFTY260908P23950 | 87 | 85 | 85 | 85 | 0/0 | 2/0/0 | TV_ONLY 09-02 11:10 ENTRY:S2A (after 66 TV events) | — |
| NIFTY260908P24000 | 63* | 86 | 63 | 63 | 0/0 | 0/23/0 | PLATFORM_ONLY 09-01 14:05 ENTRY:S1C (after 63 TV events) | truncated-capture |
| NIFTY260908P24050 | 88 | 88 | 88 | 86 | 0/2 | 0/0/0 | none | vendor-tick-high |
| NIFTY260908P24100 | 79* | 94 | 79 | 79 | 0/0 | 0/15/0 | PLATFORM_ONLY 09-02 10:30 ENTRY:R2 (after 79 TV events) | truncated-capture |
| NIFTY260908P24150 | 80 | 80 | 80 | 80 | 0/0 | 0/0/0 | none | — |

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
