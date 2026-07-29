"""Shared core for the validation harness: token loading, XTS quote fetching,
DB helpers, finding records, tolerances and output paths.

READ-ONLY contract: never calls /auth/login (see package docstring).
"""
from __future__ import annotations

import asyncio
import json
import math
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import create_engine, text

# psycopg async needs SelectorEventLoop on Windows (same reason run.py exists).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.core.config import settings  # noqa: E402
from app.core.time_utils import IST, is_nse_regular_session_open, market_open_today  # noqa: E402
from app.market_data.xts_client import (  # noqa: E402
    MSG_OPENINTEREST,
    MSG_TOUCHLINE,
    SEG_BSECM,
    SEG_BSEFO,
    SEG_NSECM,
    SEG_NSEFO,
    authed_headers,
    base_url,
)

API_BASE = "http://127.0.0.1:8000"
REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_DATE = datetime.now(IST).date()
OUT_DIR = REPO_ROOT / "logs" / "validation" / RUN_DATE.isoformat()
FIXTURE_DIR = OUT_DIR / "fixtures"

QUOTE_CHUNK = 25

VERDICT_ORDER = ["FAIL", "STALE", "SOURCE_UNAVAILABLE", "INFO", "BY_DESIGN", "SKIP", "PASS"]


@dataclass
class Finding:
    layer: str
    metric: str
    key: str
    ours: Any
    theirs: Any
    diff: Any
    tol: str
    verdict: str  # PASS | FAIL | STALE | BY_DESIGN | SKIP | INFO | SOURCE_UNAVAILABLE
    ts_ours: str | None = None
    ts_theirs: str | None = None
    note: str = ""


@dataclass
class SourceSnapshot:
    source: str
    fetched_at_utc: str
    ok: bool
    payload_path: str | None = None
    error: str | None = None
    extra: dict = field(default_factory=dict)


class TokenInvalid(RuntimeError):
    pass


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def market_state() -> str:
    return "live" if is_nse_regular_session_open() else "closed"


def js_round(x: float) -> int:
    """JavaScript Math.round semantics (half toward +Infinity), unlike Python's
    banker's rounding — needed to port frontend ATM math faithfully."""
    return math.floor(x + 0.5)


def pct_diff(ours: float, theirs: float) -> float:
    denom = max(abs(theirs), 1e-9)
    return abs(ours - theirs) / denom * 100.0


def ensure_dirs() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)


def save_json(name: str, obj: Any, subdir: Path | None = None) -> Path:
    ensure_dirs()
    target = (subdir or OUT_DIR) / name
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, default=str)
    return target


def sync_engine():
    return create_engine(settings.db_url_sync, pool_pre_ping=True)


# ---------------------------------------------------------------------------
# XTS session token (read-only reuse — NEVER login)
# ---------------------------------------------------------------------------

def load_xts_token() -> tuple[str, str, datetime]:
    """Latest (token, user_id, issued_at) from auth_sessions. No login, ever."""
    eng = sync_engine()
    try:
        with eng.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT jwt_token, client_code, issued_at FROM auth_sessions "
                    "ORDER BY issued_at DESC LIMIT 1"
                )
            ).mappings().first()
    finally:
        eng.dispose()
    if not row or not row["jwt_token"]:
        raise TokenInvalid("No XTS token in auth_sessions — is the backend logged in?")
    issued = row["issued_at"]
    if getattr(issued, "tzinfo", None) is None:
        issued = issued.replace(tzinfo=timezone.utc)
    return str(row["jwt_token"]), str(row["client_code"] or ""), issued


async def check_token(client: httpx.AsyncClient, token: str) -> bool:
    """Harmless read to prove the token is alive (indexlist GET)."""
    try:
        r = await client.get(
            f"{base_url()}/instruments/indexlist",
            params={"exchangeSegment": SEG_NSECM},
            headers=authed_headers(token),
            timeout=15.0,
        )
        return r.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# XTS quotes (REST truth)
# ---------------------------------------------------------------------------

def _parse_quote_entries(payload: Any) -> list[dict]:
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        return []
    quotes = result.get("listQuotes") or result.get("quoteList") or []
    out: list[dict] = []
    for q in quotes:
        obj = q
        if isinstance(q, str):
            try:
                obj = json.loads(q)
            except (ValueError, TypeError):
                continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


async def fetch_xts_quotes(
    client: httpx.AsyncClient,
    token: str,
    instruments: list[dict],
    msg_code: int,
) -> dict[int, dict]:
    """POST /instruments/quotes in chunks; returns {ExchangeInstrumentID: quote}."""
    quotes: dict[int, dict] = {}
    for i in range(0, len(instruments), QUOTE_CHUNK):
        chunk = instruments[i : i + QUOTE_CHUNK]
        body = {
            "instruments": chunk,
            "xtsMessageCode": int(msg_code),
            "publishFormat": "JSON",
        }
        resp = await client.post(
            f"{base_url()}/instruments/quotes",
            json=body,
            headers=authed_headers(token),
            timeout=30.0,
        )
        if resp.status_code == 401:
            raise TokenInvalid("XTS quotes returned 401 — stored token invalid; let the backend self-heal.")
        resp.raise_for_status()
        for obj in _parse_quote_entries(resp.json()):
            iid = obj.get("ExchangeInstrumentID")
            if iid is not None:
                quotes[int(iid)] = obj
    return quotes


def quote_ltp_volume(q: dict) -> tuple[float | None, int | None]:
    """LTP + traded volume from a 1501 touchline quote (tolerant of nesting)."""
    tl = q.get("Touchline") if isinstance(q.get("Touchline"), dict) else q
    ltp = tl.get("LastTradedPrice", q.get("LastTradedPrice"))
    vol = tl.get("TotalTradedQuantity", q.get("TotalTradedQuantity"))
    try:
        ltp_f = float(ltp) if ltp is not None else None
    except (TypeError, ValueError):
        ltp_f = None
    try:
        vol_i = int(vol) if vol is not None else None
    except (TypeError, ValueError):
        vol_i = None
    return ltp_f, vol_i


def quote_oi(q: dict) -> int | None:
    for key in ("OpenInterest", "OI", "openInterest"):
        if key in q:
            try:
                return int(q[key])
            except (TypeError, ValueError):
                return None
    return None


# ---------------------------------------------------------------------------
# Universe from DB (what the feed actually subscribed today)
# ---------------------------------------------------------------------------

@dataclass
class UniverseRow:
    token: str
    symbol: str
    expiry: str
    strike: int
    option_type: str


def todays_universe(symbol: str) -> list[UniverseRow]:
    day_start_utc = market_open_today().astimezone(timezone.utc) - timedelta(minutes=30)
    eng = sync_engine()
    try:
        with eng.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT DISTINCT ON (token) token, symbol, expiry, strike, option_type
                    FROM option_oi_snapshots
                    WHERE symbol = :symbol AND ts >= :day_start
                    ORDER BY token, ts DESC
                    """
                ),
                {"symbol": symbol, "day_start": day_start_utc},
            ).mappings().all()
    finally:
        eng.dispose()
    return [
        UniverseRow(
            token=str(r["token"]),
            symbol=str(r["symbol"]),
            expiry=r["expiry"].isoformat(),
            strike=int(r["strike"]),
            option_type=str(r["option_type"]),
        )
        for r in rows
    ]


def option_segment(symbol: str) -> int:
    return SEG_BSEFO if symbol.upper() == "SENSEX" else SEG_NSEFO


# ---------------------------------------------------------------------------
# Backend API helper
# ---------------------------------------------------------------------------

async def api_get(client: httpx.AsyncClient, path: str, params: dict | None = None) -> Any:
    r = await client.get(f"{API_BASE}{path}", params=params, timeout=60.0)
    r.raise_for_status()
    return r.json()


async def active_expiry(client: httpx.AsyncClient, symbol: str) -> str | None:
    """Earliest non-expired expiry for the symbol.

    NOTE: /api/expiries also returns stale past expiries from old DB rows and
    the frontend blindly defaults to expiries[0] (useMarketContext.ts:143) —
    a confirmed defect. The harness must not inherit it.
    """
    res = await api_get(client, "/api/expiries", {"symbol": symbol})
    exps = res.get("expiries") or []
    today = RUN_DATE.isoformat()
    live = [e for e in exps if e >= today]
    return (live or exps)[0] if exps else None


# ---------------------------------------------------------------------------
# Findings output
# ---------------------------------------------------------------------------

def summarize(findings: list[Finding]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.verdict] = counts.get(f.verdict, 0) + 1
    return counts


def write_layer_output(layer: str, findings: list[Finding], snapshots: list[SourceSnapshot], meta: dict | None = None) -> Path:
    payload = {
        "layer": layer,
        "generated_at_utc": now_utc().isoformat(),
        "generated_at_ist": datetime.now(IST).isoformat(),
        "market_state": market_state(),
        "meta": meta or {},
        "summary": summarize(findings),
        "snapshots": [asdict(s) for s in snapshots],
        "findings": [asdict(f) for f in findings],
    }
    path = save_json(f"{layer}.json", payload)
    counts = summarize(findings)
    print(f"[{layer}] " + ", ".join(f"{v}={counts.get(v, 0)}" for v in VERDICT_ORDER if counts.get(v)))
    for f in findings:
        if f.verdict in ("FAIL", "STALE"):
            print(f"  {f.verdict}: {f.metric} {f.key}: ours={f.ours} theirs={f.theirs} diff={f.diff} ({f.note})")
    print(f"  -> {path}")
    return path
