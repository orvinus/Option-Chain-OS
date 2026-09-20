"""Pure parsers for the TradingView Pine debug export (UMPDBG / UMPSIG lines)
and for our engine dumps — no I/O beyond the strings passed in.

The export block lives at the end of ``docs/reference/ump_v20_parity_debug.pine``:

    UMPDBG|SYM=<ticker>|T=<ms>|LASTC=<close>|D1=H/L/C|D2=H/L/C|D3=H/L/C|W=H/L/C
           |H1N=<n>|H1=<csv of reversal prices>|LVN=<n>|LV=<type:price,...>
    UMPSIG|<ticker>|C<chunk>|[END|N=<total>|]<t_ms>@<price>@<label text>;...

``tv_logs.txt`` is a raw paste of the Pine Logs panel; every line is prefixed
with the panel's ``[timestamp]:`` — anything that is not one of the two
markers is ignored.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

IST = timezone(timedelta(hours=5, minutes=30))

# Chart glyphs the Pine labels carry; stripped before classification.
_GLYPHS = ("▲ ", "✦ ", "✗ ", "◈ ", "● ", "◆ ", "★ ")

_SYM_RE = re.compile(r"^(NIFTY|SENSEX|BANKNIFTY|FINNIFTY|MIDCPNIFTY)(\d{2})(\d{2})(\d{2})([CP])(\d+)$")


@dataclass
class TvSignal:
    t_ms: int
    px: float          # label y (the candle price)
    text: str          # raw label text
    kind: str          # canonical kind (ENTRY:S2A, TRAIL_SET, ...)
    value: float       # the meaningful number (entry price, or the ₹ value in the text)

    @property
    def minute(self) -> str:
        """'mm-dd HH:MM' in IST."""
        return datetime.fromtimestamp(self.t_ms / 1000, IST).strftime("%m-%d %H:%M")


@dataclass
class TvContract:
    symbol: str
    dbg: dict[str, str] = field(default_factory=dict)
    signals: list[TvSignal] = field(default_factory=list)
    declared_n: Optional[int] = None    # N= on the END chunk; None = truncated capture

    @property
    def truncated(self) -> bool:
        return self.declared_n is None


def norm_tv(txt: str) -> str:
    t = txt
    for g in _GLYPHS:
        t = t.replace(g, "")
    return re.sub(r"\s*₹[\d.]+", "", t).strip()


def kind_tv(txt: str) -> str:
    t = norm_tv(txt)
    if t[:1] == "S" and t[1:2].isdigit():
        return "ENTRY:" + t.split()[0]
    if t.startswith("R1") or t.startswith("R2"):
        return "ENTRY:" + t.split()[0]
    if "TRAIL SET" in t:
        return "TRAIL_SET"
    if "TRAIL ↑" in t or "TRAIL UP" in t:
        return "TRAIL_RAISE"
    if "TRAIL EXIT" in t:
        return "TRAIL_EXIT"
    if "SL HIT" in t and "Base" in t:
        return "BASE_SL"
    if "MAX SL" in t:
        return "MAX_SL"
    if "ZONE SET" in t:
        return "SB_SET"
    if "ZONE ↑" in t or "ZONE UP" in t:
        return "SB_RAISE"
    if "ZONE EXIT" in t or "SB EXIT" in t:
        return "SB_EXIT"
    if "TARGET" in t:
        return "TARGET"
    if "TIMEOUT" in t or "TIME" in t:
        return "TIMEOUT"
    return t


def kind_ours(kind: str) -> str:
    if kind[:1] == "S" and kind[1:2].isdigit():
        return "ENTRY:" + kind
    if kind in ("R1", "R2"):
        return "ENTRY:" + kind
    return kind


def tv_value(kind: str, px: float, txt: str) -> float:
    """For entries the label's y IS the price; for everything else the
    meaningful number is inside the text (``TRAIL SET ₹60.55``)."""
    if kind.startswith("ENTRY:"):
        return px
    m = re.search(r"₹([0-9.]+)", txt)
    return float(m.group(1)) if m else px


def parse_tv(text: str) -> dict[str, TvContract]:
    out: dict[str, TvContract] = {}
    for line in text.splitlines():
        m = re.search(r"UMPDBG\|SYM=([A-Z0-9]+)\|(.*)", line)
        if m:
            sym, rest = m.group(1), m.group(2)
            kv = dict(p.split("=", 1) for p in rest.split("|") if "=" in p)
            out.setdefault(sym, TvContract(sym)).dbg = kv
            continue
        m = re.search(r"UMPSIG\|([A-Z0-9]+)\|C\d+\|(?:END\|N=(\d+)\|)?(.*)", line)
        if m:
            c = out.setdefault(m.group(1), TvContract(m.group(1)))
            for item in m.group(3).split(";"):
                if not item.strip():
                    continue
                t, px, txt = item.split("@", 2)
                k = kind_tv(txt)
                pxv = float(px)
                c.signals.append(TvSignal(int(t), pxv, txt.strip(), k, tv_value(k, pxv, txt)))
            if m.group(2):
                c.declared_n = int(m.group(2))
    return out


def tv_symbol_to_file(sym: str) -> Optional[str]:
    """``NIFTY260908C23900`` → ``NIFTY_2026-09-08_23900CE.json``."""
    m = _SYM_RE.match(sym)
    if not m:
        return None
    und, yy, mm, dd, cp, k = m.groups()
    return f"{und}_20{yy}-{mm}-{dd}_{k}{'CE' if cp == 'C' else 'PE'}.json"


def tv_symbol_contract(sym: str) -> Optional[tuple[str, str, int, str]]:
    """``NIFTY260908C23900`` → ('NIFTY', '2026-09-08', 23900, 'CE')."""
    m = _SYM_RE.match(sym)
    if not m:
        return None
    und, yy, mm, dd, cp, k = m.groups()
    return und, f"20{yy}-{mm}-{dd}", int(k), "CE" if cp == "C" else "PE"


def triple(s: str) -> Optional[list[float]]:
    try:
        return [float(x) for x in s.split("/")]
    except Exception:  # noqa: BLE001
        return None


def parse_levels(s: str) -> list[tuple[int, float]]:
    out: list[tuple[int, float]] = []
    for item in s.split(","):
        if ":" in item:
            t, p = item.split(":")
            out.append((int(t), float(p)))
    return out


def parse_h1(s: str) -> list[float]:
    return [float(x) for x in s.split(",") if x]


# ---- our dump side -----------------------------------------------------------

@dataclass
class OurEvent:
    minute: str        # 'mm-dd HH:MM' IST
    value: float
    kind: str
    text: str
    ts: str


def our_value(kind: str, px: float, text: str) -> float:
    if kind_ours(kind).startswith("ENTRY:"):
        return px
    nums = re.findall(r"(?<![\w.])[0-9]+(?:\.[0-9]+)?", text)
    return float(nums[-1]) if nums else px


def our_events(dump: dict) -> list[OurEvent]:
    out: list[OurEvent] = []
    for ts, kind, px, text in dump.get("events", []):
        minute = datetime.fromisoformat(ts).strftime("%m-%d %H:%M") if "T" in str(ts) else str(ts)
        out.append(OurEvent(minute, our_value(kind, float(px), text), kind_ours(kind), text, str(ts)))
    return out


LEVEL_TOL_PCT = 0.05    # level price tolerance (%); floor 0.0051 = half a paisa


def near_level(a: float, b: float, pct: float = LEVEL_TOL_PCT) -> bool:
    return abs(a - b) <= max(abs(b) * pct / 100, 0.0051)
