"""Append-only audit log (§11.3) — logins and changes, both.

Every successful login, every FAILED login attempt (a run of failures against
one account is the clearest signal of a guessed credential), every config
change as old→new per field, kills, safety-gate blocks and trade events. No
code path updates or deletes a row — a correction is a new row.

Writes are best-effort: an audit INSERT failure is logged loudly but never
takes down the action it was recording (the action already happened; losing
the process too doubles the damage).
"""
from __future__ import annotations

import json
from typing import Any, Optional

import structlog
from sqlalchemy import text

from ..core.db import AsyncSessionLocal

log = structlog.get_logger(__name__)

_INSERT_SQL = text(
    """
    INSERT INTO algo_audit_log
        (user_id, username, event_type, scope, field, old_value, new_value, detail)
    VALUES
        (:user_id, :username, :event_type, :scope, :field, :old_value, :new_value,
         CAST(:detail AS JSONB))
    """
)

_SELECT_SQL = text(
    """
    SELECT id, ts, user_id, username, event_type, scope, field,
           old_value, new_value, detail
    FROM algo_audit_log
    WHERE (:username = '' OR username = :username)
      AND (CAST(:since AS TIMESTAMPTZ) IS NULL OR ts >= CAST(:since AS TIMESTAMPTZ))
      AND (CAST(:until AS TIMESTAMPTZ) IS NULL OR ts <= CAST(:until AS TIMESTAMPTZ))
      AND (CAST(:before_id AS BIGINT) IS NULL
           OR (ts, id) < (CAST(:before_ts AS TIMESTAMPTZ), CAST(:before_id AS BIGINT)))
    ORDER BY ts DESC, id DESC
    LIMIT :limit
    """
)


def _as_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)


async def audit(
    event_type: str,
    *,
    user_id: Optional[int] = None,
    username: str = "",
    scope: str = "",
    field: str = "",
    old_value: Any = None,
    new_value: Any = None,
    detail: Optional[dict[str, Any]] = None,
) -> None:
    """Record one audit event. Never raises."""
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(
                _INSERT_SQL,
                {
                    "user_id": user_id,
                    "username": username,
                    "event_type": event_type,
                    "scope": scope,
                    "field": field,
                    "old_value": _as_text(old_value),
                    "new_value": _as_text(new_value),
                    "detail": json.dumps(detail, default=str) if detail is not None else None,
                },
            )
            await session.commit()
    except Exception:
        log.exception("algo.audit.write_failed", event_type=event_type, scope=scope)


_LAST_EVENT_SQL = text(
    """
    SELECT detail->>'date' AS d, ts
    FROM algo_audit_log
    WHERE event_type = :event_type
    ORDER BY ts DESC
    LIMIT 1
    """
)


async def last_event_date(event_type: str):
    """The IST date recorded in the newest ``event_type`` row's
    ``detail.date`` (falls back to the row's own date). None when never
    fired. Used as the restart-safe 'already sent today' marker."""
    from datetime import date as _d

    from ..core.time_utils import IST

    async with AsyncSessionLocal() as session:
        row = (await session.execute(_LAST_EVENT_SQL, {"event_type": event_type})).mappings().first()
    if row is None:
        return None
    if row["d"]:
        try:
            return _d.fromisoformat(row["d"])
        except ValueError:
            pass
    ts = row["ts"]
    return ts.astimezone(IST).date() if ts is not None else None


async def fetch_audit(
    *, username: str = "", since: Optional[str] = None, until: Optional[str] = None,
    limit: int = 200, before_ts: Optional[str] = None, before_id: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Filtered read for the dashboard's audit view (§11.3: filter by user and
    date range at minimum)."""
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                _SELECT_SQL,
                {
                    "username": username,
                    "since": since,
                    "until": until,
                    "limit": max(1, min(limit, 1000)),
                    # Keyset paging (older than the last row the UI holds):
                    # both halves or neither.
                    "before_ts": before_ts if before_id is not None else None,
                    "before_id": before_id if before_ts is not None else None,
                },
            )
        ).mappings().all()
    out: list[dict[str, Any]] = []
    for r in rows:
        item = dict(r)
        item["ts"] = item["ts"].isoformat() if item["ts"] is not None else None
        out.append(item)
    return out


def flatten_config(doc: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten a config document to dotted-path leaves so a save can be audited
    as exact per-field old→new rows (§11.3: 'what changed, old value → new
    value, not just settings updated')."""
    flat: dict[str, Any] = {}
    for key, value in doc.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(flatten_config(value, path))
        elif isinstance(value, list):
            # Lists (MTF rules, holidays, enabled indicators) are compared as a
            # unit — a JSON blob per path keeps the diff readable instead of
            # exploding into index-keyed rows that shuffle on reorder.
            flat[path] = json.dumps(value, default=str, sort_keys=True)
        else:
            flat[path] = value
    return flat


def config_diff(old: dict[str, Any], new: dict[str, Any]) -> list[tuple[str, Any, Any]]:
    """(path, old, new) for every changed leaf between two config documents."""
    flat_old = flatten_config(old)
    flat_new = flatten_config(new)
    changes: list[tuple[str, Any, Any]] = []
    for path in sorted(set(flat_old) | set(flat_new)):
        before = flat_old.get(path)
        after = flat_new.get(path)
        if before != after:
            changes.append((path, before, after))
    return changes
