"""The shadow run must be unobservable to production. Proven, not asserted.

During the migration a TrueData feed runs beside the live one. The tempting
design — both into ``option_oi_snapshots`` under different token namespaces —
is UNSAFE in this codebase, and the reason is worth stating precisely because
the migration report claims the opposite:

    Every live hot-path query keys on ``(symbol, expiry, strike, option_type)``
    and takes ``DISTINCT ON (strike, option_type) ... ORDER BY ts DESC``.
    ``token`` appears in no WHERE clause and no DISTINCT ON in any of them.

So a shadow row for a strike becomes the answer to a user's query the instant it
is the freshest row for that strike — and flows onward into ``iv_daily``, which
is keyed ``(symbol, trade_date)``, has no token, no vendor column and no
retention policy. A poisoned day there is unrecoverable and skews IVR/IVP for a
year.

Hence a separate table, and hence these tests: isolation that depends on a
future refactor never widening a WHERE clause is not isolation. What follows is
checkable by a machine on every run.

Runnable without pytest:  PYTHONPATH=. python tests/test_shadow_isolation.py
"""
from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app.ingest.aggregator import LIVE_TABLE, SHADOW_TABLE, MinuteAggregator
from app.ingest.types import Tick

REPO = Path(__file__).resolve().parents[2]
APP = REPO / "backend" / "app"

# The shadow table may be named ONLY by the ingest plumbing that writes it and
# the health route that reports it. Anything else — a service, an api handler, a
# query builder — reading it means shadow data can reach a user.
_ALLOWED = {
    "ingest/aggregator.py",       # the writer's allow-list constant
    "ingest/feed_factory.py",     # constructs the shadow aggregator
}


def _sql_literals(tree) -> list[str]:
    """Every string literal that is not a docstring.

    SQL lives in string literals, so that is where a read of the shadow table
    would have to appear. Comments and docstrings are excluded deliberately —
    they necessarily NAME the table in order to explain the isolation rule, and
    a test that cannot tell documentation from a query would force the design to
    go unexplained to stay green.
    """
    import ast

    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    return [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
    ]


def test_no_production_code_queries_the_shadow_table() -> None:
    import ast

    offenders: list[str] = []
    for path in APP.rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        rel = path.relative_to(APP).as_posix()
        if rel in _ALLOWED:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        if any(SHADOW_TABLE in lit for lit in _sql_literals(tree)):
            offenders.append(rel)
    assert not offenders, (
        f"{SHADOW_TABLE} appears in executable strings outside the shadow "
        f"plumbing: {offenders}. Any read path makes shadow data user-visible."
    )


def test_unified_view_excludes_the_shadow_table() -> None:
    """Replay/timeseries read the view; the shadow must not be an arm of it."""
    migrations = (REPO / "backend" / "alembic" / "versions").glob("*.py")
    for m in migrations:
        text = m.read_text(encoding="utf-8", errors="replace")
        for stmt in re.findall(r"CREATE OR REPLACE VIEW oi_snapshots_unified.*?;", text, re.S):
            assert SHADOW_TABLE not in stmt, f"{m.name} unions the shadow table into the view"


def test_aggregator_rejects_arbitrary_table_names() -> None:
    """The table name is interpolated into SQL — it must be an allow-list."""
    q: asyncio.Queue = asyncio.Queue()
    MinuteAggregator(q, table=LIVE_TABLE)
    MinuteAggregator(q, table=SHADOW_TABLE)
    for bad in ("option_oi_snapshots; DROP TABLE users", "users", ""):
        try:
            MinuteAggregator(q, table=bad)
        except ValueError:
            continue
        raise AssertionError(f"aggregator accepted an unlisted table: {bad!r}")


def test_shadow_aggregator_targets_the_shadow_table_only() -> None:
    q: asyncio.Queue = asyncio.Queue()
    live = MinuteAggregator(q, table=LIVE_TABLE)
    shadow = MinuteAggregator(q, table=SHADOW_TABLE)
    assert live._table == LIVE_TABLE and not live._shadow
    assert shadow._table == SHADOW_TABLE and shadow._shadow


def test_reference_instruments_never_become_rows() -> None:
    """option_type is CHAR(2); 'IDX'/'FUT' RAISE on insert and kill the batch.

    The aggregator guarded 'IDX' only, because under XTS that was the only
    reachable value. TrueData uses continuous futures as the MCX reference
    price, which made 'FUT' reachable for the first time.
    """
    q: asyncio.Queue = asyncio.Queue()
    agg = MinuteAggregator(q, table=LIVE_TABLE)
    now = datetime.now(timezone.utc)
    for ot in ("IDX", "FUT"):
        agg._absorb(Tick(
            ts=now, token=f"td:NIFTY:{ot}", symbol="NIFTY", expiry=date(1970, 1, 1),
            strike=0, option_type=ot, ltp=100.0, oi=1, volume=0,
        ))
    assert not agg._open_buckets, "a reference instrument was buffered for insert"

    agg._absorb(Tick(
        ts=now, token="td:NIFTY:260828:24500:CE", symbol="NIFTY",
        expiry=date(2026, 8, 28), strike=24500, option_type="CE",
        ltp=120.5, oi=500000, volume=900,
    ))
    assert len(agg._open_buckets) == 1, "a real contract must still be buffered"


def test_vendor_field_is_separate_from_origin() -> None:
    """origin drives the steward's health rule; vendor must not disturb it."""
    t = Tick(
        ts=datetime.now(timezone.utc), token="td:x", symbol="NIFTY",
        expiry=date(2026, 8, 28), strike=24500, option_type="CE",
        ltp=1.0, oi=1, volume=0, vendor="truedata",
    )
    assert t.origin == "ws", "the default transport tag must stay exactly 'ws'"
    assert t.vendor == "truedata"


def test_ws_rows_count_is_blind_to_vendor() -> None:
    """rt.last_ws_flush_at must advance for TrueData rows exactly as for XTS.

    If a vendor-tagged origin made TrueData rows uncountable, the steward would
    see a permanently stale feed and rebuild forever. This pins the invariant
    that ws_rows counts TRANSPORT, not vendor.
    """
    q: asyncio.Queue = asyncio.Queue()
    agg = MinuteAggregator(q, table=LIVE_TABLE)
    now = datetime.now(timezone.utc) - timedelta(minutes=5)
    for i, vendor in enumerate(("xts", "truedata")):
        agg._absorb(Tick(
            ts=now, token=f"tok{i}", symbol="NIFTY", expiry=date(2026, 8, 28),
            strike=24500 + i, option_type="CE", ltp=1.0, oi=1, volume=0,
            origin="ws", vendor=vendor,
        ))
    ws_rows = sum(1 for t in agg._open_buckets.values() if t.origin == "ws")
    assert ws_rows == 2, "both vendors' socket rows must count as WS-origin"


def test_shadow_hook_does_not_touch_live_freshness() -> None:
    """The shadow flush hook must not advance the steward's health signal."""
    src = (APP / "ingest" / "feed_factory.py").read_text(encoding="utf-8")
    hook = src.split("async def _shadow_flush")[1].split("\n\n")[0]
    assert "last_ws_flush_at" not in hook, (
        "the shadow hook advances the WS freshness timestamp — a dead live "
        "socket would look healthy and the recovery ladder would never fire"
    )
    assert "publish_flush" not in hook, "the shadow hook broadcasts to browsers"
    assert "shadow_last_flush_at" in hook


def _run_all() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


if __name__ == "__main__":
    _run_all()
    print("\nall shadow isolation tests passed")
