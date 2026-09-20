"""§6 Export — pure, DB-free streaming serialisers (CSV / NDJSON) and the
row flatteners the export router feeds them with.

Everything here is a plain function over async iterators of dicts, so the
router stays a thin dispatcher and the formatting is unit-testable without
a database:

- ``stream_csv(headers, rows)``   → async iterator of RFC-4180 text chunks
  (``csv.writer`` with ``\\r\\n`` line ends, quoting only where needed; 500
  rows per chunk; no row cap).
- ``stream_ndjson(headers, rows)`` → first line ``{"__headers__": [...]}``
  then one JSON object per row projected onto exactly those keys (the
  frontend XLSX writer relies on the stable column set).
- ``flatten_trade``    → ledger row + ``fee_*`` columns, ``fee_total``,
  ``held_min`` (fees JSON removed).
- ``flatten_decision`` → decision-trace row with the JSON columns serialised
  as canonical strings (``sort_keys=True``) so a cell diff is meaningful.

Cell rules (shared by both formats): ``None`` → empty cell (CSV) / ``null``
(NDJSON); dicts/lists → canonical JSON text; dates/datetimes → ISO-8601;
``Decimal`` → float; bools → ``true``/``false`` in CSV, JSON booleans in
NDJSON.
"""
from __future__ import annotations

import csv
import io
import json
from collections.abc import AsyncIterator, Iterable, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

CHUNK_ROWS = 500

FEE_KEYS: tuple[str, ...] = (
    "brokerage", "stt", "exchange_txn", "sebi", "ipft", "gst", "stamp_duty",
)

# Column order of a flattened ledger/backtest trade row. ``id`` carries the
# ledger id (live/paper) or the in-run ``seq`` (backtest).
TRADE_HEADERS: tuple[str, ...] = (
    "id", "trade_date", "day", "zone_id", "index_symbol", "side", "strike", "expiry",
    "entry_ts", "entry_price", "exit_ts", "exit_price", "lots", "pnl_rupees", "pnl_pct",
    "exit_reason", "ledger", "sub_scenario", "held_min",
    *(f"fee_{k}" for k in FEE_KEYS), "fee_total",
)

DECISION_HEADERS: tuple[str, ...] = (
    "id", "ts", "trade_date", "day", "zone_id", "symbol", "expiry", "ledger",
    "config_version", "stage", "state", "direction", "unanimous", "decision", "reason",
    "gate_blocks", "readings", "candidates", "sizing", "position", "zone_snapshot",
    "data_age_s", "trade_id",
)
DECISION_JSON_COLS: tuple[str, ...] = (
    "gate_blocks", "readings", "candidates", "sizing", "position", "zone_snapshot",
)

SIGNAL_HEADERS: tuple[str, ...] = (
    "id", "ts", "trade_date", "day", "zone_id", "indicator", "reading", "payload",
)


def _json_text(v: Any) -> str:
    return json.dumps(v, sort_keys=True, default=str, separators=(",", ":"))


def csv_cell(v: Any) -> str:
    """Render one value for ``csv.writer`` (which handles quoting)."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list, tuple)):
        return _json_text(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(float(v))
    return str(v)


def json_cell(v: Any) -> Any:
    """Render one value for the NDJSON object (keeps numbers/bools typed)."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (dict, list, tuple)):
        return _json_text(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return str(v)


def _project(headers: Sequence[str], row: Any) -> list[Any]:
    if isinstance(row, dict):
        return [row.get(h) for h in headers]
    return list(row)


async def stream_csv(
    headers: Sequence[str], rows: AsyncIterator[Any], *, chunk_rows: int = CHUNK_ROWS
) -> AsyncIterator[str]:
    """RFC-4180 CSV: header row first, ``\\r\\n`` line ends, cells quoted only
    when they contain a delimiter, quote or line break; embedded quotes are
    doubled. Rows are buffered ``chunk_rows`` at a time so a 300k-row export
    never materialises in memory."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(list(headers))
    n = 0
    async for row in rows:
        writer.writerow([csv_cell(v) for v in _project(headers, row)])
        n += 1
        if n % chunk_rows == 0:
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)
    yield buf.getvalue()


async def stream_ndjson(
    headers: Sequence[str], rows: AsyncIterator[Any], *, chunk_rows: int = CHUNK_ROWS
) -> AsyncIterator[str]:
    """Header line ``{"__headers__": [...]}`` then one object per row whose
    keys are exactly ``headers`` in order."""
    hdrs = list(headers)
    parts: list[str] = [json.dumps({"__headers__": hdrs}) + "\n"]
    n = 0
    async for row in rows:
        cells = _project(hdrs, row)
        obj = {h: json_cell(v) for h, v in zip(hdrs, cells, strict=True)}
        parts.append(json.dumps(obj, default=str) + "\n")
        n += 1
        if n % chunk_rows == 0:
            yield "".join(parts)
            parts = []
    if parts:
        yield "".join(parts)


async def aiter_list(items: Iterable[Any]) -> AsyncIterator[Any]:
    """Wrap an in-memory list as the async iterator the streamers expect."""
    for it in items:
        yield it


def _parse_ts(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None


def held_minutes(entry_ts: Any, exit_ts: Any) -> float | None:
    """Minutes between entry and exit (1 decimal); None while open."""
    a, b = _parse_ts(entry_ts), _parse_ts(exit_ts)
    if a is None or b is None:
        return None
    if (a.tzinfo is None) != (b.tzinfo is None):
        a, b = a.replace(tzinfo=None), b.replace(tzinfo=None)
    return round((b - a).total_seconds() / 60, 1)


def flatten_trade(row: dict[str, Any]) -> dict[str, Any]:
    """Ledger/backtest trade → flat export row: every column of
    ``trade_store.trades_filtered`` except ``fees``, plus ``fee_<key>`` for
    each fee component, ``fee_total`` and ``held_min``. Missing fee keys
    become None (an open trade has no fees yet)."""
    fees = row.get("fees")
    if isinstance(fees, str):
        try:
            fees = json.loads(fees)
        except ValueError:
            fees = None
    fees = fees if isinstance(fees, dict) else {}
    out: dict[str, Any] = {k: v for k, v in row.items() if k != "fees"}
    out["held_min"] = held_minutes(row.get("entry_ts"), row.get("exit_ts"))
    for k in FEE_KEYS:
        v = fees.get(k)
        out[f"fee_{k}"] = float(v) if v is not None else None
    total = fees.get("total")
    out["fee_total"] = float(total) if total is not None else None
    return out


def flatten_decision(row: dict[str, Any]) -> dict[str, Any]:
    """Decision-trace row with JSON columns as canonical strings (sorted keys)
    so the cell is stable across exports; everything else unchanged."""
    out = dict(row)
    for k in DECISION_JSON_COLS:
        v = out.get(k)
        if v is None:
            continue
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                out[k] = v
                continue
        out[k] = _json_text(v)
    return out


def flatten_settings(obj: Any, prefix: str = "") -> list[dict[str, Any]]:
    """Nested settings → ``{key, value}`` rows with dotted keys; lists become
    one JSON cell (their shape is not tabular)."""
    rows: list[dict[str, Any]] = []
    if isinstance(obj, dict):
        for k in sorted(obj):
            rows.extend(flatten_settings(obj[k], f"{prefix}{k}."))
        return rows
    key = prefix[:-1] if prefix.endswith(".") else prefix
    rows.append({"key": key, "value": obj})
    return rows


__all__ = [
    "CHUNK_ROWS",
    "DECISION_HEADERS",
    "FEE_KEYS",
    "SIGNAL_HEADERS",
    "TRADE_HEADERS",
    "aiter_list",
    "csv_cell",
    "flatten_decision",
    "flatten_settings",
    "flatten_trade",
    "held_minutes",
    "json_cell",
    "stream_csv",
    "stream_ndjson",
]
