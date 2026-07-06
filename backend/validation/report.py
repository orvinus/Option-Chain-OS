"""Merge all layer JSON outputs into logs/validation/<date>/REPORT.md."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from app.core.time_utils import IST

from . import common

SEVERITY_NOTES = {
    "layer_a": "P1 if FAIL — wrong data between broker and DB/API",
    "layer_b": "P1 if FAIL — API math diverges from raw DB",
    "layer_c": "P2 if FAIL — API correct but display misleading",
    "layer_d": "INFO/P2 — third-party vendor cross-check",
    "layer_e": "INFO/P2 — official exchange cross-check",
    "layer_f": "P1 if FAIL — OI-change delta diverges from Sensibull over the same window",
}


def load_layers() -> dict[str, dict]:
    out = {}
    for p in sorted(common.OUT_DIR.glob("layer_*.json")):
        with open(p, encoding="utf-8") as f:
            out[p.stem] = json.load(f)
    return out


def fmt_val(v, limit: int = 60) -> str:
    s = json.dumps(v, default=str) if isinstance(v, (dict, list)) else str(v)
    s = s.replace("|", "\\|").replace("\n", " ")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def build() -> Path:
    layers = load_layers()
    lines: list[str] = []
    now = datetime.now(IST)
    lines.append("# XTS Data Validation Report — Shree Laxmi (Lakshmishree) broker feed")
    lines.append("")
    lines.append(f"- **Run date:** {common.RUN_DATE.isoformat()} (generated {now.isoformat(timespec='seconds')})")
    lines.append(f"- **Market state at generation:** {common.market_state()}")
    lines.append("- **Pipeline:** XTS Socket.IO (1501+1510) → 1s aggregator → TimescaleDB → FastAPI → React")
    lines.append("- **Method:** 5 layers — A broker-REST truth, B independent SQL recompute, "
                 "C frontend-transform ports, D Sensibull (Zerodha) cross-check, E NSE official cross-check. "
                 "All comparisons timestamped; raw payloads cached beside this report.")
    lines.append("")

    # ---- summary table ----
    lines.append("## Summary")
    lines.append("")
    lines.append("| Layer output | PASS | FAIL | STALE | INFO | BY_DESIGN | SKIP | SRC_UNAVAIL |")
    lines.append("|---|---|---|---|---|---|---|---|")
    totals: dict[str, int] = defaultdict(int)
    for name, data in layers.items():
        s = data.get("summary", {})
        for k, v in s.items():
            totals[k] += v
        lines.append(
            f"| {name} | {s.get('PASS', 0)} | {s.get('FAIL', 0)} | {s.get('STALE', 0)} "
            f"| {s.get('INFO', 0)} | {s.get('BY_DESIGN', 0)} | {s.get('SKIP', 0)} "
            f"| {s.get('SOURCE_UNAVAILABLE', 0)} |")
    lines.append(
        f"| **TOTAL** | **{totals.get('PASS', 0)}** | **{totals.get('FAIL', 0)}** | {totals.get('STALE', 0)} "
        f"| {totals.get('INFO', 0)} | {totals.get('BY_DESIGN', 0)} | {totals.get('SKIP', 0)} "
        f"| {totals.get('SOURCE_UNAVAILABLE', 0)} |")
    lines.append("")

    # ---- failures per layer ----
    lines.append("## Failures and notable findings")
    lines.append("")
    any_fail = False
    for name, data in layers.items():
        rows = [f for f in data.get("findings", [])
                if f["verdict"] in ("FAIL", "STALE", "SOURCE_UNAVAILABLE", "INFO")]
        if not rows:
            continue
        any_fail = True
        lines.append(f"### {name}")
        base = name.split("_round")[0].rsplit("_", 1)[0] if "_round" in name else "_".join(name.split("_")[:2])
        lines.append(f"_{SEVERITY_NOTES.get(base, '')}_")
        lines.append("")
        lines.append("| Verdict | Metric | Key | Ours | Theirs | Diff | Note |")
        lines.append("|---|---|---|---|---|---|---|")
        for f in rows[:40]:
            lines.append(
                f"| {f['verdict']} | {f['metric']} | {fmt_val(f['key'], 30)} | {fmt_val(f['ours'])} "
                f"| {fmt_val(f['theirs'])} | {fmt_val(f['diff'], 40)} | {fmt_val(f.get('note', ''), 160)} |")
        if len(rows) > 40:
            lines.append(f"| … | {len(rows) - 40} more | | | | | see {name}.json |")
        lines.append("")
    if not any_fail:
        lines.append("No failures.")
        lines.append("")

    path = common.OUT_DIR / "REPORT_RAW.md"
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"raw report -> {path}")
    return path


if __name__ == "__main__":
    build()
