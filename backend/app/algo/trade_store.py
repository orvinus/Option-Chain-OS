"""Trade ledger + signal log persistence (algo_trades / algo_signals).

The trade ledger is append-only in spirit: rows are INSERTed at entry and
their exit half is filled in exactly once at close — nothing else ever
updates or deletes a LIVE row. The single sanctioned exception is the §8.3
paper-session reset, which clears PAPER rows only (and is itself audited).

Every read the P&L tab needs lives here as one aggregate each, so the API
layer stays a thin serializer.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, AsyncIterator, Optional

from sqlalchemy import text

from ..core.db import AsyncSessionLocal

_INSERT_TRADE = text(
    """
    INSERT INTO algo_trades
        (trade_date, day, zone_id, index_symbol, side, token, strike, expiry,
         entry_ts, entry_price, lots, ledger, sub_scenario, broker_order_id)
    VALUES
        (:trade_date, :day, :zone_id, :index_symbol, :side, :token, :strike, :expiry,
         :entry_ts, :entry_price, :lots, :ledger, :sub_scenario, :broker_order_id)
    RETURNING id
    """
)

_CLOSE_TRADE = text(
    """
    UPDATE algo_trades
    SET exit_ts = :exit_ts,
        exit_price = :exit_price,
        pnl_rupees = :pnl_rupees,
        pnl_pct = :pnl_pct,
        exit_reason = :exit_reason,
        fees = CAST(:fees AS JSONB)
    WHERE id = :id AND exit_ts IS NULL
    """
)

_OPEN_TRADE_TODAY = text(
    """
    SELECT id, trade_date, day, zone_id, index_symbol, side, token, strike, expiry,
           entry_ts, entry_price, lots, ledger, sub_scenario, broker_order_id
    FROM algo_trades
    WHERE trade_date = :trade_date AND ledger = :ledger AND exit_ts IS NULL
    ORDER BY entry_ts DESC
    LIMIT 1
    """
)

# Overnight carry: the newest open row REGARDLESS of entry date — a carried
# position from a prior session must be re-adopted at the next startup.
_OPEN_TRADE_LATEST = text(
    """
    SELECT id, trade_date, day, zone_id, index_symbol, side, token, strike, expiry,
           entry_ts, entry_price, lots, ledger, sub_scenario, broker_order_id
    FROM algo_trades
    WHERE ledger = :ledger AND exit_ts IS NULL
    ORDER BY entry_ts DESC
    LIMIT 1
    """
)

# EXIT-day attribution (timestamps are stored as IST wall clock under a UTC
# label — the orchestrator inserts naive IST — so AT TIME ZONE 'UTC' recovers
# the IST calendar date). Realized P&L belongs to the day the position
# CLOSED: with overnight carry, a Monday entry closing Tuesday must hit
# Tuesday's loss cap / profit lock / streak, not retroactively rewrite Monday.
_TODAY_CLOSED = text(
    """
    SELECT zone_id, pnl_rupees
    FROM algo_trades
    WHERE (exit_ts AT TIME ZONE 'UTC')::date = :trade_date
      AND ledger = :ledger AND exit_ts IS NOT NULL
    ORDER BY exit_ts
    """
)

# ENTRY-day attribution for max_trades: a zone's trade allowance is consumed
# when the entry fires, open or closed — a carried trade closing today must
# not eat today's allowance.
_TODAY_ENTRIES = text(
    """
    SELECT zone_id
    FROM algo_trades
    WHERE trade_date = :trade_date AND ledger = :ledger
    ORDER BY entry_ts
    """
)

_TRADES_FILTERED = text(
    """
    SELECT id, trade_date, day, zone_id, index_symbol, side, strike, expiry,
           entry_ts, entry_price, exit_ts, exit_price, lots, pnl_rupees, pnl_pct,
           exit_reason, ledger, sub_scenario, fees
    FROM algo_trades
    WHERE (CAST(:trade_date AS DATE) IS NULL OR trade_date = CAST(:trade_date AS DATE))
      AND (:day = '' OR day = :day)
      AND (:zone = '' OR zone_id = :zone)
      AND (:ledger = '' OR ledger = :ledger)
      AND (CAST(:from_date AS DATE) IS NULL OR trade_date >= CAST(:from_date AS DATE))
      AND (CAST(:to_date AS DATE) IS NULL OR trade_date <= CAST(:to_date AS DATE))
    ORDER BY entry_ts DESC
    LIMIT :limit
    """
)

# Summaries: the RANGE filter and the calendar's date group use the EXIT day
# (where the realized P&L lands); the weekday/zone groups keep the stored
# entry-day columns — they describe which SETUP produced the trade. A carried
# trade's weekday row is therefore its entry weekday.
_SUMMARY_BY = """
    SELECT {group_col} AS grp,
           COUNT(*) AS trades,
           COUNT(*) FILTER (WHERE pnl_rupees > 0) AS wins,
           COUNT(*) FILTER (WHERE pnl_rupees <= 0) AS losses,
           COALESCE(SUM(pnl_rupees), 0) AS pnl,
           COALESCE(SUM(pnl_rupees) FILTER (WHERE pnl_rupees > 0), 0) AS gross_win,
           COALESCE(SUM(pnl_rupees) FILTER (WHERE pnl_rupees <= 0), 0) AS gross_loss
    FROM algo_trades
    WHERE exit_ts IS NOT NULL
      AND ledger = :ledger
      AND (exit_ts AT TIME ZONE 'UTC')::date >= CAST(:from_date AS DATE)
      AND (exit_ts AT TIME ZONE 'UTC')::date <= CAST(:to_date AS DATE)
    GROUP BY 1
    ORDER BY 1
"""

_SUMMARY_GROUP_COLS = {
    "trade_date": "(exit_ts AT TIME ZONE 'UTC')::date",   # calendar = exit day
    "day": "day",
    "zone_id": "zone_id",
}

_INSERT_SIGNAL = text(
    """
    INSERT INTO algo_signals (ts, trade_date, day, zone_id, indicator, reading, payload)
    VALUES (:ts, :trade_date, :day, :zone_id, :indicator, :reading, CAST(:payload AS JSONB))
    """
)

_LAST_SIGNAL = text(
    """
    SELECT reading FROM algo_signals
    WHERE trade_date = :trade_date AND zone_id = :zone_id AND indicator = :indicator
    ORDER BY ts DESC
    LIMIT 1
    """
)

_DELETE_PAPER = text("DELETE FROM algo_trades WHERE ledger = 'paper'")

_PAPER_REALIZED = text(
    """
    SELECT COALESCE(SUM(pnl_rupees), 0)
    FROM algo_trades
    WHERE ledger = 'paper' AND exit_ts IS NOT NULL
      AND (exit_ts AT TIME ZONE 'UTC')::date >= CAST(:since AS DATE)
    """
)


@dataclass
class OpenTrade:
    id: int
    trade_date: date
    day: str
    zone_id: str
    index_symbol: str
    side: str
    token: str
    strike: Optional[int]
    expiry: Optional[date]
    entry_ts: datetime
    entry_price: float
    lots: int
    ledger: str
    sub_scenario: str
    broker_order_id: str = ""


async def insert_trade(
    *,
    trade_date: date,
    day: str,
    zone_id: str,
    index_symbol: str,
    side: str,
    token: str,
    strike: Optional[int],
    expiry: Optional[date],
    entry_ts: datetime,
    entry_price: float,
    lots: int,
    ledger: str,
    sub_scenario: str,
    broker_order_id: str = "",
) -> int:
    async with AsyncSessionLocal() as s:
        row = (
            await s.execute(
                _INSERT_TRADE,
                {
                    "trade_date": trade_date,
                    "day": day,
                    "zone_id": zone_id,
                    "index_symbol": index_symbol,
                    "side": side,
                    "token": token,
                    "strike": strike,
                    "expiry": expiry,
                    "entry_ts": entry_ts,
                    "entry_price": entry_price,
                    "lots": lots,
                    "ledger": ledger,
                    "sub_scenario": sub_scenario,
                    "broker_order_id": broker_order_id,
                },
            )
        ).scalar_one()
        await s.commit()
        return int(row)


async def close_trade(
    trade_id: int,
    *,
    exit_ts: datetime,
    exit_price: float,
    pnl_rupees: float,
    pnl_pct: Optional[float],
    exit_reason: str,
    fees: dict[str, float],
) -> None:
    async with AsyncSessionLocal() as s:
        await s.execute(
            _CLOSE_TRADE,
            {
                "id": trade_id,
                "exit_ts": exit_ts,
                "exit_price": exit_price,
                "pnl_rupees": pnl_rupees,
                "pnl_pct": pnl_pct,
                "exit_reason": exit_reason,
                "fees": json.dumps(fees),
            },
        )
        await s.commit()


def _row_to_open_trade(row: Any) -> OpenTrade:
    return OpenTrade(
        id=int(row["id"]),
        trade_date=row["trade_date"],
        day=row["day"],
        zone_id=row["zone_id"],
        index_symbol=row["index_symbol"],
        side=row["side"],
        token=row["token"],
        strike=row["strike"],
        expiry=row["expiry"],
        entry_ts=row["entry_ts"],
        entry_price=float(row["entry_price"]),
        lots=int(row["lots"]),
        ledger=row["ledger"],
        sub_scenario=row["sub_scenario"],
        broker_order_id=str(row["broker_order_id"] or ""),
    )


async def open_trade_today(trade_date: date, ledger: str) -> Optional[OpenTrade]:
    async with AsyncSessionLocal() as s:
        row = (
            await s.execute(
                _OPEN_TRADE_TODAY, {"trade_date": trade_date, "ledger": ledger}
            )
        ).mappings().first()
    return _row_to_open_trade(row) if row is not None else None


async def open_trade_latest(ledger: str) -> Optional[OpenTrade]:
    """The newest open row of ANY entry date — the overnight-carry adoption
    source. A carried position is never abandoned to its entry day."""
    async with AsyncSessionLocal() as s:
        row = (
            await s.execute(_OPEN_TRADE_LATEST, {"ledger": ledger})
        ).mappings().first()
    return _row_to_open_trade(row) if row is not None else None


async def today_closed(trade_date: date, ledger: str) -> list[tuple[str, float]]:
    """(zone_id, pnl) of the trades that CLOSED on ``trade_date`` (exit-day
    attribution — any entry day), in exit order — feeds the risk counters
    (loss cap, profit lock, consecutive-loss streak)."""
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(_TODAY_CLOSED, {"trade_date": trade_date, "ledger": ledger})
        ).mappings().all()
    return [(r["zone_id"], float(r["pnl_rupees"] or 0)) for r in rows]


_CLOSED_ON = text(
    """
    SELECT id, trade_date, day, zone_id, index_symbol, side, strike, expiry,
           entry_ts, entry_price, exit_ts, exit_price, lots, ledger,
           sub_scenario, pnl_rupees, pnl_pct, exit_reason, fees
    FROM algo_trades
    WHERE (exit_ts AT TIME ZONE 'UTC')::date = :trade_date
      AND ledger = :ledger AND exit_ts IS NOT NULL
    ORDER BY exit_ts, id
    """
)


async def closed_on(trade_date: date, ledger: str) -> list[dict]:
    """Full rows of the trades that CLOSED on ``trade_date`` (exit-day
    attribution, exit order) — the daily Telegram summary's input."""
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(_CLOSED_ON, {"trade_date": trade_date, "ledger": ledger})).mappings().all()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        for k in ("entry_ts", "exit_ts"):
            if d.get(k) is not None and hasattr(d[k], "isoformat"):
                d[k] = d[k].isoformat()
        for k in ("trade_date", "expiry"):
            if d.get(k) is not None and hasattr(d[k], "isoformat"):
                d[k] = d[k].isoformat()
        if isinstance(d.get("fees"), str):
            try:
                d["fees"] = json.loads(d["fees"])
            except Exception:
                pass
        out.append(d)
    return out


async def today_entries(trade_date: date, ledger: str) -> list[str]:
    """zone_ids of the trades ENTERED on ``trade_date`` (open or closed) —
    the max_trades-per-zone consumption list."""
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(_TODAY_ENTRIES, {"trade_date": trade_date, "ledger": ledger})
        ).mappings().all()
    return [r["zone_id"] for r in rows]


async def trades_filtered(
    *,
    trade_date: Optional[str] = None,
    day: str = "",
    zone: str = "",
    ledger: str = "",
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(
                _TRADES_FILTERED,
                {
                    "trade_date": trade_date,
                    "day": day,
                    "zone": zone,
                    "ledger": ledger,
                    "from_date": from_date,
                    "to_date": to_date,
                    "limit": max(1, min(limit, 2000)),
                },
            )
        ).mappings().all()
    return [_trade_out(r) for r in rows]


def _trade_out(r: Any) -> dict[str, Any]:
    """ISO/float serialisation of one algo_trades row (shared by the P&L
    reads and the §6 export stream)."""
    d = dict(r)
    d["trade_date"] = d["trade_date"].isoformat()
    d["expiry"] = d["expiry"].isoformat() if d["expiry"] else None
    d["entry_ts"] = d["entry_ts"].isoformat()
    d["exit_ts"] = d["exit_ts"].isoformat() if d["exit_ts"] else None
    for k in ("entry_price", "exit_price", "pnl_rupees", "pnl_pct"):
        d[k] = float(d[k]) if d[k] is not None else None
    return d


async def summary_by(
    group: str, *, ledger: str, from_date: str, to_date: str
) -> list[dict[str, Any]]:
    """group ∈ {'trade_date', 'day', 'zone_id'} — one aggregate each. The
    'trade_date' group is the CALENDAR group and resolves to the exit day."""
    assert group in _SUMMARY_GROUP_COLS
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(
                text(_SUMMARY_BY.format(group_col=_SUMMARY_GROUP_COLS[group])),
                {"ledger": ledger, "from_date": from_date, "to_date": to_date},
            )
        ).mappings().all()
    out = []
    for r in rows:
        grp = r["grp"]
        out.append(
            {
                "group": grp.isoformat() if isinstance(grp, date) else grp,
                "trades": int(r["trades"]),
                "wins": int(r["wins"]),
                "losses": int(r["losses"]),
                "pnl": float(r["pnl"]),
                "gross_win": float(r["gross_win"]),
                "gross_loss": float(r["gross_loss"]),
            }
        )
    return out


async def record_signal_transition(
    *,
    ts: datetime,
    trade_date: date,
    day: str,
    zone_id: str,
    indicator: str,
    reading: str,
    payload: Optional[dict[str, Any]] = None,
) -> bool:
    """Insert only when the reading CHANGED since the last stored row for
    (date, zone, indicator) — the log records transitions, not every tick."""
    async with AsyncSessionLocal() as s:
        last = (
            await s.execute(
                _LAST_SIGNAL,
                {"trade_date": trade_date, "zone_id": zone_id, "indicator": indicator},
            )
        ).scalar()
        if last == reading:
            return False
        await s.execute(
            _INSERT_SIGNAL,
            {
                "ts": ts,
                "trade_date": trade_date,
                "day": day,
                "zone_id": zone_id,
                "indicator": indicator,
                "reading": reading,
                "payload": json.dumps(payload, default=str) if payload else None,
            },
        )
        await s.commit()
        return True


async def reset_paper_ledger() -> int:
    """§8.3 Reset controls — clears PAPER rows only. The caller audits."""
    async with AsyncSessionLocal() as s:
        res = await s.execute(_DELETE_PAPER)
        await s.commit()
        return res.rowcount or 0


async def paper_realized_since(since: str) -> float:
    async with AsyncSessionLocal() as s:
        val = (await s.execute(_PAPER_REALIZED, {"since": since})).scalar()
    return float(val or 0)


# §7 drawdown feed: every closed trade in the summary's EXIT-day range (the
# same filter as _SUMMARY_BY), in exit order — (exit_ts ISO, pnl_rupees).
_CLOSED_POINTS = text(
    """
    SELECT id, exit_ts, pnl_rupees
    FROM algo_trades
    WHERE exit_ts IS NOT NULL
      AND ledger = :ledger
      AND (exit_ts AT TIME ZONE 'UTC')::date >= CAST(:from_date AS DATE)
      AND (exit_ts AT TIME ZONE 'UTC')::date <= CAST(:to_date AS DATE)
    ORDER BY exit_ts, id
    """
)


async def closed_points(ledger: str, from_date: str, to_date: str) -> list[tuple[str, float]]:
    """(exit_ts ISO, pnl_rupees) of the closed trades whose EXIT day falls in
    [from_date, to_date], ordered by exit_ts then id — the equity-curve
    input for the P&L summary's max drawdown."""
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(
                _CLOSED_POINTS,
                {"ledger": ledger, "from_date": from_date, "to_date": to_date},
            )
        ).mappings().all()
    return [(r["exit_ts"].isoformat(), float(r["pnl_rupees"] or 0)) for r in rows]


# ── §6 export streams + signal reads ──────────────────────────────────────
# Server-side cursors (session.stream + yield_per) so a full-range export
# never materialises the table in memory. Chronological order (oldest first)
# — a spreadsheet reads top-down.

_ITER_TRADES = text(
    """
    SELECT id, trade_date, day, zone_id, index_symbol, side, strike, expiry,
           entry_ts, entry_price, exit_ts, exit_price, lots, pnl_rupees, pnl_pct,
           exit_reason, ledger, sub_scenario, fees
    FROM algo_trades
    WHERE ledger = :ledger
      AND trade_date >= CAST(:from_date AS DATE)
      AND trade_date <= CAST(:to_date AS DATE)
      AND (:zone = '' OR zone_id = :zone)
    ORDER BY entry_ts, id
    """
)

_SIGNALS_FILTERED = text(
    """
    SELECT id, ts, trade_date, day, zone_id, indicator, reading, payload
    FROM algo_signals
    WHERE trade_date = CAST(:trade_date AS DATE)
      AND (:zone = '' OR zone_id = :zone)
      AND (:indicator = '' OR indicator = :indicator)
    ORDER BY ts DESC, id DESC
    LIMIT :limit
    """
)

_ITER_SIGNALS = text(
    """
    SELECT id, ts, trade_date, day, zone_id, indicator, reading, payload
    FROM algo_signals
    WHERE trade_date >= CAST(:from_date AS DATE)
      AND trade_date <= CAST(:to_date AS DATE)
      AND (:zone = '' OR zone_id = :zone)
      AND (:indicator = '' OR indicator = :indicator)
    ORDER BY ts, id
    """
)


def _signal_out(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["id"] = int(d["id"])
    d["ts"] = d["ts"].isoformat()
    d["trade_date"] = d["trade_date"].isoformat()
    return d


async def iter_trades(
    *, ledger: str, from_date: str, to_date: str, zone: str = ""
) -> AsyncIterator[dict[str, Any]]:
    """Every algo_trades row of ``ledger`` entered in [from_date, to_date]
    (entry-day attribution, like ``trades_filtered``), oldest first."""
    async with AsyncSessionLocal() as s:
        result = await s.stream(
            _ITER_TRADES.execution_options(yield_per=500),
            {"ledger": ledger, "from_date": from_date, "to_date": to_date, "zone": zone},
        )
        async for r in result.mappings():
            yield _trade_out(r)


async def signals_filtered(
    *, trade_date: str, zone: str = "", indicator: str = "", limit: int = 400
) -> list[dict[str, Any]]:
    """Signal transitions of one date, newest first (JSON read endpoint)."""
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(
                _SIGNALS_FILTERED,
                {
                    "trade_date": trade_date,
                    "zone": zone,
                    "indicator": indicator,
                    "limit": max(1, min(int(limit), 2000)),
                },
            )
        ).mappings().all()
    return [_signal_out(r) for r in rows]


async def iter_signals(
    *, from_date: str, to_date: str, zone: str = "", indicator: str = ""
) -> AsyncIterator[dict[str, Any]]:
    async with AsyncSessionLocal() as s:
        result = await s.stream(
            _ITER_SIGNALS.execution_options(yield_per=500),
            {"from_date": from_date, "to_date": to_date, "zone": zone, "indicator": indicator},
        )
        async for r in result.mappings():
            yield _signal_out(r)


# ── per-day notes (algo_day_notes — the trader's journal line per date) ──

_NOTES_FOR_MONTH = text(
    """
    SELECT note_date, note FROM algo_day_notes
    WHERE note_date >= CAST(:first AS DATE) AND note_date <= CAST(:last AS DATE)
    ORDER BY note_date
    """
)

_UPSERT_NOTE = text(
    """
    INSERT INTO algo_day_notes (note_date, note, updated_by, updated_at)
    VALUES (CAST(:d AS DATE), :note, :by, now())
    ON CONFLICT (note_date)
    DO UPDATE SET note = :note, updated_by = :by, updated_at = now()
    """
)

_DELETE_NOTE = text("DELETE FROM algo_day_notes WHERE note_date = CAST(:d AS DATE)")


async def notes_for_range(first: str, last: str) -> dict[str, str]:
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(_NOTES_FOR_MONTH, {"first": first, "last": last})).all()
    return {r[0].isoformat(): r[1] for r in rows}


async def set_day_note(d: str, note: str, updated_by: str) -> str:
    """Upsert the note for a date; an EMPTY note deletes the row (the same
    sanctioned-delete pattern as the paper reset). Returns 'saved'/'deleted'."""
    async with AsyncSessionLocal() as s:
        if note.strip():
            await s.execute(_UPSERT_NOTE, {"d": d, "note": note, "by": updated_by})
            action = "saved"
        else:
            await s.execute(_DELETE_NOTE, {"d": d})
            action = "deleted"
        await s.commit()
    return action
