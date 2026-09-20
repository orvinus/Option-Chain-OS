"""Timestamp-level Pine ↔ platform parity report (markdown).

    python -m scripts.parity.report --tv logs/parity/tv_logs.txt --ours logs/parity5 \\
        --out docs/ump-tv-parity-report-<date>.md [--min-date 08-26] [--decisions decisions.json]

Sections: stage diff (daily/weekly/1H/levels), per-contract event alignment
(exact matches, LABEL/VALUE diffs, TV-only, platform-only, ±1-bar timing
offsets), root-cause tags, and — when a decisions export is supplied —
"filtered signals": platform-only ENTRY events joined to the orchestrator's
decision records, naming the filter that rejected the minute.

Root-cause tags (each documented in the report): first-week-divergence
(untraded-day settlement bars on TV), truncated-capture (Pine log limit, no
END marker), timeout-semantics (chart Trigger Timeout ≠ dump), vendor-tick-high,
realtime-vs-bar (1-minute vs 5-minute evaluation).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.parity.align import align_events, compare_stages  # noqa: E402
from scripts.parity.tv_parse import our_events, parse_tv, tv_symbol_to_file  # noqa: E402


def _root_cause(sym: str, al, dump: dict, tv) -> list[str]:
    tags: list[str] = []
    if tv.truncated:
        tags.append("truncated-capture")
    sessions = dump.get("sessions", [])
    if al.first_structural is not None and sessions:
        # first divergence inside the contract's first 3 traded days
        first_days = set(sessions[:3])
        fs = al.first_structural.minute[:5]      # 'mm-dd'
        if any(d[5:] == fs for d in first_days):
            tags.append("first-week-divergence")
    if dump.get("trigger_timeout_bars") not in (None, 1):
        tags.append("timeout-semantics")
    if not dump.get("bar5"):
        tags.append("realtime-vs-bar")
    if any(d.kind == "VALUE" and d.event_kind.startswith("TRAIL") for d in al.diffs):
        tags.append("vendor-tick-high")
    return tags


def _filtered_signals(platform_only_entries: list[tuple[str, str, str]], decisions: list[dict]) -> list[dict]:
    """Join platform-only ENTRY events (sym, 'mm-dd HH:MM', kind) to decision
    rows by minute → the responsible filter (the decision's reason)."""
    by_min: dict[str, dict] = {}
    for r in decisions:
        ts = r.get("ts")
        if not ts:
            continue
        try:
            m = datetime.fromisoformat(str(ts)).strftime("%m-%d %H:%M")
        except ValueError:
            continue
        by_min[m] = r
    out = []
    for sym, minute, kind in platform_only_entries:
        r = by_min.get(minute)
        out.append({
            "symbol": sym, "minute": minute, "kind": kind,
            "orchestrator_decision": r.get("decision") if r else None,
            "responsible_filter": (r.get("reason") or r.get("stage")) if r else "no decision row for that minute",
            "gate_blocks": r.get("gate_blocks") if r else None,
        })
    return out


def build_report(tv_text: str, ours_dir: Path, *, min_date: str | None, decisions: list[dict] | None,
                 tv_label: str) -> str:
    tv = parse_tv(tv_text)
    lines: list[str] = []
    lines.append(f"# Pine ↔ platform parity — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
    lines.append(f"TV export: `{tv_label}` · our dumps: `{ours_dir}` · alignment key = (5-minute bar, event kind)"
                 + (f" · from {min_date}" if min_date else "") + "\n")
    tot = Counter()
    stage_tot = Counter()
    per: list[str] = []
    tags_all: Counter = Counter()
    platform_only_entries: list[tuple[str, str, str]] = []
    for sym in sorted(tv):
        f = tv_symbol_to_file(sym)
        p = ours_dir / f if f else None
        if not p or not p.exists():
            per.append(f"| {sym} | — | our dump missing | | | | | |")
            continue
        dump = json.loads(p.read_text(encoding="utf-8"))
        st = compare_stages(tv[sym], dump)
        stage_tot["daily_ok"] += sum(st.daily_ok)
        stage_tot["daily_n"] += 3
        stage_tot["weekly_ok"] += int(st.weekly_ok)
        stage_tot["weekly_n"] += 1
        stage_tot["h1_ident"] += int(st.h1_tv == st.h1_ours == st.h1_matched)
        stage_tot["lv_ident"] += int(st.levels_tv == st.levels_ours == st.levels_matched)
        stage_tot["n"] += 1
        al = align_events(sym, tv[sym].signals, our_events(dump), min_date=min_date, truncated=tv[sym].truncated)
        c = Counter(d.kind for d in al.diffs)
        tot["tv"] += al.tv_n
        tot["ours"] += al.ours_n
        tot["matched"] += al.matched
        tot["exact"] += al.exact
        for k in ("LABEL", "VALUE", "TV_ONLY", "PLATFORM_ONLY", "TIMING"):
            tot[k] += c[k]
        tags = _root_cause(sym, al, dump, tv[sym])
        tags_all.update(tags)
        for d in al.diffs:
            if d.kind == "PLATFORM_ONLY" and d.event_kind.startswith("ENTRY:"):
                platform_only_entries.append((sym, d.minute, d.event_kind))
        first = (f"{al.first_structural.kind} {al.first_structural.minute} {al.first_structural.event_kind} "
                 f"(after {al.tv_before_first} TV events)") if al.first_structural else "none"
        per.append(
            f"| {sym} | {al.tv_n}{'*' if tv[sym].truncated else ''} | {al.ours_n} | {al.matched} | {al.exact} | "
            f"{c['LABEL']}/{c['VALUE']} | {c['TV_ONLY']}/{c['PLATFORM_ONLY']}/{c['TIMING']} | {first} | {', '.join(tags) or '—'} |"
        )
    n = max(stage_tot["n"], 1)
    lines.append("## Totals\n")
    lines.append(f"- contracts: {stage_tot['n']}")
    lines.append(f"- daily candles (D1..D3) exact: {stage_tot['daily_ok']}/{stage_tot['daily_n']} · weekly exact: {stage_tot['weekly_ok']}/{stage_tot['weekly_n']}")
    lines.append(f"- 1H reversal arrays identical: {stage_tot['h1_ident']}/{n} · level sets identical: {stage_tot['lv_ident']}/{n}")
    pct = (100.0 * tot["matched"] / tot["tv"]) if tot["tv"] else 0.0
    lines.append(f"- events: TV {tot['tv']} · platform {tot['ours']} · matched (bar+kind) {tot['matched']} ({pct:.1f}% of TV) · exact (bar+kind+value) {tot['exact']}")
    lines.append(f"- diffs: LABEL {tot['LABEL']} · VALUE {tot['VALUE']} · TV-only {tot['TV_ONLY']} · platform-only {tot['PLATFORM_ONLY']} · timing (±1 bar) {tot['TIMING']}")
    lines.append(f"- root-cause tags: {dict(tags_all) or 'none'}\n")
    lines.append("## Per contract\n")
    lines.append("| Contract | TV events | platform events | matched | exact | LABEL/VALUE | TV-only/platform-only/timing | first structural diff | root-cause tags |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    lines.extend(per)
    lines.append("\n`*` = truncated TV capture (no END marker — the Pine log limit cut the export; its trailing events are unknown).\n")
    lines.append("## Filtered signals (platform-only entries → responsible filter)\n")
    if decisions is None:
        lines.append("n/a: no decisions export supplied (`--decisions <json>` from `/api/algo/decisions` or `/runs/{id}/decisions`).\n")
    elif not platform_only_entries:
        lines.append("none — every platform entry has a TradingView counterpart in this window.\n")
    else:
        lines.append("| Contract | minute | entry | orchestrator decision | responsible filter |")
        lines.append("|---|---|---|---|---|")
        for r in _filtered_signals(platform_only_entries, decisions):
            lines.append(f"| {r['symbol']} | {r['minute']} | {r['kind']} | {r['orchestrator_decision'] or '—'} | {r['responsible_filter']} |")
        lines.append("")
    lines.append("## Tolerances (documented, not hiding misses)\n")
    lines.append("- level / candle prices: 0.05% of the value with a 0.0051 floor (half a paisa above the 0.05 tick).")
    lines.append("- event values: 0.011 (a matched pair beyond it is a VALUE diff); MAX_SL labels carry the running low and are exempt.")
    lines.append("- time: the 5-minute bar (Pine evaluates once per completed bar on history; the engine runs 1-minute commits and the dump folds them — `--bar5`).")
    lines.append("- kind: TRAIL_SET vs TRAIL_RAISE count as the same rung (Pine labels the first rung SET) and are reported as LABEL diffs.\n")
    lines.append("## Root-cause tag legend\n")
    lines.append("- **first-week-divergence** — TV's daily feed carries settlement bars for listed-but-untraded days; the vendor bhavcopy has no zero-volume rows, so the Data-Ready gate opens later on the platform (first 1–3 traded days of a contract).")
    lines.append("- **truncated-capture** — Pine log limit cut the export before END; trailing TV events unknown.")
    lines.append("- **timeout-semantics** — the dump's Trigger Timeout differs from the chart's (chart runs 1).")
    lines.append("- **vendor-tick-high** — a trail value differs because the vendor's 1-minute high differs from TV's tick high.")
    lines.append("- **realtime-vs-bar** — the dump was produced at 1-minute granularity (no `--bar5`).")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tv", required=True, help="Pine Logs paste (tv_logs.txt)")
    ap.add_argument("--ours", required=True, help="directory of engine dumps (scripts.parity.dump --out)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--min-date", default=None, help="mm-dd filter (both sides)")
    ap.add_argument("--decisions", default=None, help="JSON list of decision rows for the filtered-signals section")
    args = ap.parse_args()
    decisions = None
    if args.decisions:
        raw = json.loads(Path(args.decisions).read_text(encoding="utf-8"))
        decisions = raw.get("rows", raw) if isinstance(raw, dict) else raw
    md = build_report(
        Path(args.tv).read_text(encoding="utf-8", errors="replace"), Path(args.ours),
        min_date=args.min_date, decisions=decisions, tv_label=args.tv,
    )
    Path(args.out).write_text(md, encoding="utf-8")
    print(f"wrote {args.out}")
    for line in md.splitlines()[3:10]:
        print(line)


if __name__ == "__main__":
    main()
