# Pine ↔ platform parity — 2026-09-11 11:59

TV export: `logs/parity/tv_logs.txt` · our dumps: `logs/parity5_settlement` · alignment key = (5-minute bar, event kind)

## Totals

- contracts: 20
- daily candles (D1..D3) exact: 60/60 · weekly exact: 19/20
- 1H reversal arrays identical: 17/20 · level sets identical: 9/20
- events: TV 1396 · platform 1455 · matched (bar+kind) 1365 (97.8% of TV) · exact (bar+kind+value) 1341
- diffs: LABEL 1 · VALUE 23 · TV-only 31 · platform-only 90 · timing (±1 bar) 6
- root-cause tags: {'vendor-tick-high': 8, 'first-week-divergence': 2, 'truncated-capture': 2}

## Per contract

| Contract | TV events | platform events | matched | exact | LABEL/VALUE | TV-only/platform-only/timing | first structural diff | root-cause tags |
|---|---|---|---|---|---|---|---|---|
| NIFTY260908C23700 | 23 | 23 | 22 | 19 | 0/3 | 1/1/0 | TV_ONLY 08-25 09:25 TRAIL_RAISE (after 2 TV events) | vendor-tick-high |
| NIFTY260908C23750 | 13 | 13 | 13 | 13 | 0/0 | 0/0/0 | none | — |
| NIFTY260908C23800 | 53 | 53 | 53 | 53 | 0/0 | 0/0/0 | none | — |
| NIFTY260908C23850 | 31 | 29 | 29 | 28 | 0/1 | 2/0/0 | TV_ONLY 08-26 11:00 ENTRY:S3A (after 0 TV events) | vendor-tick-high |
| NIFTY260908C23900 | 58 | 56 | 54 | 52 | 0/2 | 4/2/0 | TV_ONLY 08-20 09:20 ENTRY:S3B (after 0 TV events) | first-week-divergence, vendor-tick-high |
| NIFTY260908C23950 | 50 | 52 | 50 | 50 | 0/0 | 0/2/0 | PLATFORM_ONLY 09-02 12:20 BASE_SL (after 44 TV events) | — |
| NIFTY260908C24000 | 89 | 93 | 87 | 86 | 0/1 | 2/6/1 | PLATFORM_ONLY 08-21 13:50 ENTRY:S2A (after 14 TV events) | vendor-tick-high |
| NIFTY260908C24050 | 64 | 64 | 64 | 64 | 0/0 | 0/0/0 | none | — |
| NIFTY260908C24100 | 88 | 92 | 85 | 85 | 0/0 | 3/7/2 | PLATFORM_ONLY 08-19 09:15 ENTRY:R2 (after 0 TV events) | first-week-divergence |
| NIFTY260908C24150 | 70 | 70 | 68 | 64 | 0/4 | 2/2/1 | PLATFORM_ONLY 08-26 09:50 ENTRY:R1 (after 22 TV events) | vendor-tick-high |
| NIFTY260908P23700 | 27 | 27 | 27 | 27 | 0/0 | 0/0/0 | none | — |
| NIFTY260908P23750 | 19 | 25 | 17 | 12 | 1/4 | 2/8/0 | PLATFORM_ONLY 09-02 09:55 ENTRY:R1 (after 0 TV events) | vendor-tick-high |
| NIFTY260908P23800 | 45 | 46 | 44 | 44 | 0/0 | 1/2/0 | PLATFORM_ONLY 09-02 11:20 TRAIL_SET (after 28 TV events) | — |
| NIFTY260908P23850 | 42 | 44 | 41 | 41 | 0/0 | 1/3/0 | PLATFORM_ONLY 09-02 10:50 TRAIL_SET (after 36 TV events) | — |
| NIFTY260908P23900 | 111 | 111 | 111 | 109 | 0/2 | 0/0/0 | none | vendor-tick-high |
| NIFTY260908P23950 | 123 | 121 | 121 | 121 | 0/0 | 2/0/0 | TV_ONLY 09-02 11:10 ENTRY:S2A (after 102 TV events) | — |
| NIFTY260908P24000 | 131* | 161 | 127 | 127 | 0/0 | 4/34/0 | PLATFORM_ONLY 08-14 15:10 ENTRY:R1 (after 2 TV events) | truncated-capture |
| NIFTY260908P24050 | 115 | 116 | 108 | 102 | 0/6 | 7/8/2 | PLATFORM_ONLY 09-02 11:20 TRAIL_SET (after 104 TV events) | vendor-tick-high |
| NIFTY260908P24100 | 129* | 144 | 129 | 129 | 0/0 | 0/15/0 | PLATFORM_ONLY 09-02 10:30 ENTRY:R2 (after 129 TV events) | truncated-capture |
| NIFTY260908P24150 | 115 | 115 | 115 | 115 | 0/0 | 0/0/0 | none | — |

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
