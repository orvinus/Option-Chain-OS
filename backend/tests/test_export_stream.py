"""§6 Export — DB-free tests.

Part 1: the pure streamers/flatteners in ``core/export_stream.py`` (RFC-4180
escaping, NDJSON header line, fee columns).
Part 2: the ``/api/algo/export/*`` and ``/api/algo/signals`` routes through a
FastAPI TestClient with ``require_admin`` overridden and every store iterator
monkeypatched — the HTTP contract the frontend ExportDialog was written
against (status codes, media types, attachment filenames).
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
from datetime import date

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from app.algo import decisions as dec
from app.algo import trade_store as store
from app.algo.auth import AdminIdentity, require_admin
from app.algo.backtest import store as bt_store
from app.api import algo_export, algo_trades
from app.core.export_stream import (
    TRADE_HEADERS,
    aiter_list,
    flatten_decision,
    flatten_settings,
    flatten_trade,
    held_minutes,
    stream_csv,
    stream_ndjson,
)


def _collect(agen) -> str:
    async def go():
        return "".join([chunk async for chunk in agen])
    return asyncio.run(go())


# ── part 1: pure formatting ──────────────────────────────────────────────────

def test_csv_escapes_commas_quotes_and_line_breaks_and_renders_none_empty():
    rows = [
        {"a": "plain", "b": 'say "hi"', "c": None},
        {"a": "x,y", "b": "line1\r\nline2", "c": 1.5},
        {"a": "nl\nonly", "b": True, "c": {"k": [1, 2]}},
    ]
    text = _collect(stream_csv(["a", "b", "c"], aiter_list(rows)))
    assert text.startswith("a,b,c\r\n")
    assert '"say ""hi"""' in text                     # quotes doubled + wrapped
    assert '"x,y"' in text                            # comma wrapped
    assert '"line1\r\nline2"' in text                 # CR/LF kept inside quotes
    assert "plain,\"say \"\"hi\"\"\",\r\n" in text     # None → empty cell
    # Round-trip through the stdlib reader: three data rows, cells intact.
    parsed = list(csv.reader(io.StringIO(text)))
    assert parsed[0] == ["a", "b", "c"]
    assert parsed[1] == ["plain", 'say "hi"', ""]
    assert parsed[2] == ["x,y", "line1\r\nline2", "1.5"]
    assert parsed[3] == ["nl\nonly", "true", '{"k":[1,2]}']
    assert len(parsed) == 4


def test_csv_chunks_every_n_rows_and_has_no_row_cap():
    rows = [{"a": i} for i in range(1203)]

    async def go():
        return [c async for c in stream_csv(["a"], aiter_list(rows), chunk_rows=500)]

    chunks = asyncio.run(go())
    assert len(chunks) == 3                            # 500 / 500 / 203 (+header in 1st)
    joined = "".join(chunks)
    assert joined.count("\r\n") == 1204
    assert joined.endswith("1202\r\n")


def test_ndjson_header_line_then_one_object_per_row_in_header_order():
    rows = [{"b": 2, "a": "x", "extra": "dropped"}, {"a": None}]
    text = _collect(stream_ndjson(["a", "b"], aiter_list(rows)))
    lines = text.splitlines()
    assert json.loads(lines[0]) == {"__headers__": ["a", "b"]}
    assert json.loads(lines[1]) == {"a": "x", "b": 2}
    assert json.loads(lines[2]) == {"a": None, "b": None}
    assert len(lines) == 3
    # JSON columns are serialised as canonical strings, bools stay bools.
    text2 = _collect(
        stream_ndjson(["j", "f"], aiter_list([{"j": {"z": 1, "a": 2}, "f": False}]))
    )
    assert json.loads(text2.splitlines()[1]) == {"j": '{"a":2,"z":1}', "f": False}


def _trade(**over):
    base = {
        "id": 7, "trade_date": "2026-09-08", "day": "tuesday", "zone_id": "Z2",
        "index_symbol": "NIFTY", "side": "CALL", "strike": 24500, "expiry": "2026-09-09",
        "entry_ts": "2026-09-08T09:31:00", "entry_price": 101.5,
        "exit_ts": "2026-09-08T09:58:00", "exit_price": 110.0, "lots": 3,
        "pnl_rupees": 1595.0, "pnl_pct": 5.32, "exit_reason": "TARGET", "ledger": "paper",
        "sub_scenario": "S1A",
        "fees": {"brokerage": 40.0, "stt": 6.44, "exchange_txn": 6.9, "sebi": 0.02,
                 "ipft": 0.1, "gst": 8.45, "stamp_duty": 0.59, "total": 62.5},
    }
    base.update(over)
    return base


def test_flatten_trade_breaks_fees_into_columns_and_adds_held_min():
    row = flatten_trade(_trade())
    assert "fees" not in row
    assert row["fee_brokerage"] == 40.0 and row["fee_gst"] == 8.45
    assert row["fee_stamp_duty"] == 0.59 and row["fee_total"] == 62.5
    assert row["held_min"] == 27.0
    assert set(TRADE_HEADERS) <= set(row)              # every export column present
    # open trade: no fees yet, no held time
    open_row = flatten_trade(_trade(exit_ts=None, exit_price=None, fees=None))
    assert open_row["fee_total"] is None and open_row["fee_brokerage"] is None
    assert open_row["held_min"] is None
    # fees arriving as a JSON string (raw driver value) still break out
    assert flatten_trade(_trade(fees=json.dumps({"total": 1.25})))["fee_total"] == 1.25


def test_held_minutes_mixed_tz_awareness_and_bad_input():
    assert held_minutes("2026-09-08T09:31:00+05:30", "2026-09-08T09:41:30") == 10.5
    assert held_minutes("garbage", "2026-09-08T09:41:30") is None


def test_flatten_decision_serialises_json_columns_with_sorted_keys():
    row = flatten_decision({"id": 1, "readings": {"z": 1, "a": {"y": 2, "b": 3}},
                            "candidates": None, "gate_blocks": '{"b":1,"a":2}', "reason": "x"})
    assert row["readings"] == '{"a":{"b":3,"y":2},"z":1}'
    assert row["gate_blocks"] == '{"a":2,"b":1}'
    assert row["candidates"] is None and row["reason"] == "x"


def test_flatten_settings_dotted_keys():
    rows = flatten_settings({"b": {"y": 1, "x": [1, 2]}, "a": "s"})
    assert rows == [
        {"key": "a", "value": "s"},
        {"key": "b.x", "value": [1, 2]},
        {"key": "b.y", "value": 1},
    ]


# ── part 2: the HTTP contract ────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    api = APIRouter(prefix="/api")
    api.include_router(algo_trades.router)
    api.include_router(algo_export.router)
    app.include_router(api)

    async def fake_admin():
        return AdminIdentity(user_id=1, username="tester", role="admin")

    app.dependency_overrides[require_admin] = fake_admin

    calls: dict[str, dict] = {}

    async def iter_trades(**kw):
        calls["iter_trades"] = kw
        yield _trade()
        yield _trade(id=8, exit_reason='SL, "manual"', exit_ts=None, fees=None)

    async def iter_signals(**kw):
        calls["iter_signals"] = kw
        yield {"id": 1, "ts": "2026-09-08T09:20:00", "trade_date": "2026-09-08",
               "day": "tuesday", "zone_id": "Z1", "indicator": "ratio", "reading": "CALL",
               "payload": {"pcr": 0.9}}

    async def signals_filtered(**kw):
        calls["signals_filtered"] = kw
        return [{"id": 1, "ts": "2026-09-08T09:20:00", "trade_date": "2026-09-08",
                 "day": "tuesday", "zone_id": "Z1", "indicator": "ratio",
                 "reading": "CALL", "payload": None}]

    async def summary_by(group, **kw):
        calls.setdefault("summary_by", []).append((group, kw))
        grp = {"trade_date": "2026-09-08", "day": "tuesday"}.get(group, "Z2")
        return [{"group": grp, "trades": 2, "wins": 1, "losses": 1, "pnl": 100.0,
                 "gross_win": 300.0, "gross_loss": -200.0}]

    async def allocated_for(day_key, ledger):
        return 15000.0

    async def iter_live(frm, to, zone_id=""):
        calls["iter_live"] = {"from": frm, "to": to, "zone": zone_id}
        yield {"id": 1, "ts": "2026-09-08T09:20:00", "trade_date": "2026-09-08",
               "day": "tuesday", "zone_id": "Z1", "symbol": "NIFTY", "expiry": None,
               "ledger": "paper", "config_version": 3, "stage": "hunting", "state": "",
               "direction": "CALL", "unanimous": True, "decision": "reject", "reason": "r",
               "gate_blocks": None, "readings": {"b": 1, "a": 2}, "candidates": [],
               "sizing": None, "position": None, "zone_snapshot": None,
               "data_age_s": 1.0, "trade_id": None}

    runs = {
        5: {"id": 5, "label": "L", "created_by": "t", "created_at": "2026-09-01T10:00:00",
            "from_date": "2026-08-01", "to_date": "2026-08-31", "config_version": 7,
            "settings": {"starting_balance": 30000, "sim_effective": {"slippage_pct": 0.1}},
            "status": "done", "days_total": 20, "days_done": 20, "cursor_date": None,
            "started_at": None, "finished_at": None, "summary": None, "error": ""},
    }

    async def get_run(run_id, include_config=False):
        return runs.get(run_id)

    async def iter_run_trades(run_id, **kw):
        calls["iter_run_trades"] = {"run_id": run_id, **kw}
        yield _trade(id=1, trade_date="2026-08-05")

    async def iter_run_decisions(run_id, **kw):
        calls["iter_run_decisions"] = {"run_id": run_id, **kw}
        if False:
            yield  # pragma: no cover

    async def iter_run_signals(run_id, **kw):
        calls["iter_run_signals"] = {"run_id": run_id, **kw}
        if False:
            yield  # pragma: no cover

    async def run_days(run_id):
        return [
            {"trade_date": "2026-08-05", "status": "done", "skip_reason": "",
             "detail": {"net": 100.0, "fees": 10.0, "gross": 110.0, "equity_after": 30100.0,
                        "trades": 1, "symbol": "NIFTY"}},
            {"trade_date": "2026-08-06", "status": "skipped", "skip_reason": "holiday",
             "detail": None},
        ]

    async def summarize(run_id):
        return {"trades": 1, "wins": 1, "net_pnl": 100.0, "max_drawdown": 0.0,
                "by_zone": [{"group": "Z1", "trades": 1, "pnl": 100.0}],
                "equity": [{"date": "2026-08-05", "equity": 30100.0, "day_pnl": 100.0},
                           {"date": "2026-08-20", "equity": 30050.0, "day_pnl": -50.0}]}

    monkeypatch.setattr(store, "iter_trades", iter_trades)
    monkeypatch.setattr(store, "iter_signals", iter_signals)
    monkeypatch.setattr(store, "signals_filtered", signals_filtered)
    monkeypatch.setattr(store, "summary_by", summary_by)
    async def drawdown_fields(led, start, end):
        return {"max_drawdown": 0.0}

    monkeypatch.setattr(algo_trades, "_allocated_for", allocated_for)
    monkeypatch.setattr(algo_export, "_allocated_for", allocated_for)
    # §7 adds drawdown fields (ledger balance + closed_points) into the same
    # summary computation — stub the seam so this stays DB-free.
    monkeypatch.setattr(algo_trades, "_drawdown_fields", drawdown_fields, raising=False)
    monkeypatch.setattr(dec, "iter_live", iter_live)
    monkeypatch.setattr(bt_store, "get_run", get_run)
    monkeypatch.setattr(bt_store, "iter_run_trades", iter_run_trades)
    monkeypatch.setattr(bt_store, "iter_run_decisions", iter_run_decisions)
    monkeypatch.setattr(bt_store, "iter_run_signals", iter_run_signals)
    monkeypatch.setattr(bt_store, "run_days", run_days)
    monkeypatch.setattr(bt_store, "summarize", summarize)

    c = TestClient(app)
    c.calls = calls  # type: ignore[attr-defined]
    return c


def test_export_trades_csv_contract(client):
    r = client.get("/api/algo/export/trades", params={
        "ledger": "paper", "from_date": "2026-09-01", "to_date": "2026-09-08", "format": "csv",
    })
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"] == \
        'attachment; filename="algo-paper-trades-2026-09-01_2026-09-08.csv"'
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == list(TRADE_HEADERS)
    assert len(rows) == 3
    by = dict(zip(rows[0], rows[1], strict=True))
    assert by["fee_total"] == "62.5" and by["held_min"] == "27.0"
    second = dict(zip(rows[0], rows[2], strict=True))
    assert second["exit_reason"] == 'SL, "manual"'     # escaped + restored
    assert client.calls["iter_trades"] == {
        "ledger": "paper", "from_date": "2026-09-01", "to_date": "2026-09-08", "zone": "",
    }


def test_export_trades_ndjson_contract(client):
    r = client.get("/api/algo/export/trades", params={
        "ledger": "live", "from_date": "2026-09-08", "to_date": "2026-09-08", "format": "ndjson",
    })
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    cd = r.headers["content-disposition"]
    assert cd.endswith('algo-live-trades-2026-09-08_2026-09-08.ndjson"')
    lines = [json.loads(line) for line in r.text.splitlines()]
    assert lines[0] == {"__headers__": list(TRADE_HEADERS)}
    assert lines[1]["fee_total"] == 62.5 and lines[1]["held_min"] == 27.0
    assert list(lines[1].keys()) == list(TRADE_HEADERS)


def test_export_range_validation(client):
    bad = client.get("/api/algo/export/trades", params={
        "ledger": "paper", "from_date": "2026-09-08", "to_date": "2026-09-01",
    })
    assert bad.status_code == 400 and "after" in bad.json()["detail"]
    long = client.get("/api/algo/export/trades", params={
        "ledger": "paper", "from_date": "2025-01-01", "to_date": "2026-03-01",
    })
    assert long.status_code == 400 and "400 days" in long.json()["detail"]
    ok_edge = client.get("/api/algo/export/trades", params={
        "ledger": "paper", "from_date": "2025-01-01", "to_date": "2026-02-04",   # exactly 400
    })
    assert ok_edge.status_code == 200
    assert client.get("/api/algo/export/trades", params={
        "ledger": "nope", "from_date": "2026-09-01", "to_date": "2026-09-02"}).status_code == 400
    assert client.get("/api/algo/export/trades", params={
        "ledger": "paper", "from_date": "2026-09-01"}).status_code == 422      # to_date required
    assert client.get("/api/algo/export/trades", params={
        "ledger": "paper", "from_date": "09/01/2026", "to_date": "2026-09-02"}).status_code == 400


def test_export_decisions_signals_calendar_summary(client):
    d = client.get("/api/algo/export/decisions", params={
        "from_date": "2026-09-08", "to_date": "2026-09-08", "zone": "z1", "format": "ndjson",
    })
    assert d.status_code == 200
    body = [json.loads(line) for line in d.text.splitlines()]
    assert body[1]["readings"] == '{"a":2,"b":1}'    # canonical JSON cell
    assert body[1]["unanimous"] is True
    assert client.calls["iter_live"] == {
        "from": date(2026, 9, 8), "to": date(2026, 9, 8), "zone": "Z1",
    }

    s = client.get("/api/algo/export/signals", params={
        "from_date": "2026-09-08", "to_date": "2026-09-08", "indicator": "Ratio",
    })
    assert s.status_code == 200
    rows = list(csv.reader(io.StringIO(s.text)))
    assert rows[0][:3] == ["id", "ts", "trade_date"] and rows[1][-1] == '{"pcr":0.9}'
    assert client.calls["iter_signals"]["indicator"] == "ratio"

    cal = client.get("/api/algo/export/calendar", params={
        "ledger": "paper", "from_date": "2026-09-01", "to_date": "2026-09-08",
    })
    assert cal.status_code == 200
    rows = list(csv.reader(io.StringIO(cal.text)))
    assert rows[0] == ["date", "weekday", "trades", "wins", "losses", "pnl", "allocated", "pnl_pct"]
    assert rows[1] == ["2026-09-08", "tuesday", "2", "1", "1", "100.0", "15000.0", "0.67"]

    summ = client.get("/api/algo/export/summary", params={
        "ledger": "paper", "from_date": "2026-09-01", "to_date": "2026-09-08", "format": "ndjson",
    })
    assert summ.status_code == 200
    body = [json.loads(line) for line in summ.text.splitlines()]
    assert body[0] == {"__headers__": ["section", "key", "metric", "value"]}
    metrics = {(r["section"], r["key"], r["metric"]): r["value"] for r in body[1:]}
    assert metrics[("summary", "", "trades")] == 2
    assert metrics[("summary", "", "ledger")] == "paper"
    assert metrics[("by_zone", "Z2", "pnl")] == 100.0
    assert metrics[("by_day", "tuesday", "allocated")] == 15000.0


def test_export_backtest_datasets_and_clamp(client):
    t = client.get("/api/algo/export/backtest/5/trades")
    assert t.status_code == 200
    assert t.headers["content-disposition"] == \
        'attachment; filename="backtest-5-trades-2026-08-01_2026-08-31.csv"'
    assert client.calls["iter_run_trades"] == {
        "run_id": 5, "from_date": "2026-08-01", "to_date": "2026-08-31",
    }

    # Custom range is clamped to the run.
    t2 = client.get("/api/algo/export/backtest/5/decisions",
                    params={"from_date": "2026-07-01", "to_date": "2026-08-10", "zone": "Z2"})
    assert t2.status_code == 200
    assert "backtest-5-decisions-2026-08-01_2026-08-10.csv" in t2.headers["content-disposition"]
    assert client.calls["iter_run_decisions"] == {
        "run_id": 5, "from_date": "2026-08-01", "to_date": "2026-08-10", "zone": "Z2"}

    assert client.get("/api/algo/export/backtest/5/signals").status_code == 200
    assert client.calls["iter_run_signals"]["indicator"] == ""

    days = client.get("/api/algo/export/backtest/5/days", params={"format": "ndjson"})
    body = [json.loads(line) for line in days.text.splitlines()]
    assert body[1]["trade_date"] == "2026-08-05" and body[1]["net"] == 100.0
    assert body[2]["status"] == "skipped" and body[2]["detail"] is None

    eq = client.get("/api/algo/export/backtest/5/equity", params={"to_date": "2026-08-10"})
    rows = list(csv.reader(io.StringIO(eq.text)))
    assert rows == [["date", "equity", "day_pnl"], ["2026-08-05", "30100.0", "100.0"]]

    summ = client.get("/api/algo/export/backtest/5/summary", params={"format": "ndjson"})
    body = [json.loads(line) for line in summ.text.splitlines()]
    metrics = {(r["section"], r["key"], r["metric"]): r["value"] for r in body[1:]}
    assert metrics[("summary", "", "run_id")] == 5
    assert metrics[("summary", "", "max_drawdown")] == 0.0
    assert metrics[("by_zone", "Z1", "pnl")] == 100.0
    assert ("summary", "", "equity") not in metrics

    st = client.get("/api/algo/export/backtest/5/settings", params={"format": "ndjson"})
    body = [json.loads(line) for line in st.text.splitlines()]
    kv = {r["key"]: r["value"] for r in body[1:]}
    assert kv["settings.sim_effective.slippage_pct"] == 0.1
    assert kv["config_version"] == 7 and kv["settings.starting_balance"] == 30000


def test_export_backtest_errors(client):
    assert client.get("/api/algo/export/backtest/999/trades").status_code == 404
    assert client.get("/api/algo/export/backtest/5/bogus").status_code == 422
    r = client.get("/api/algo/export/backtest/5/trades",
                   params={"from_date": "2026-09-01", "to_date": "2026-09-05"})
    assert r.status_code == 400 and "overlap" in r.json()["detail"]
    r = client.get("/api/algo/export/backtest/5/trades",
                   params={"from_date": "2026-08-20", "to_date": "2026-08-10"})
    assert r.status_code == 400


def test_signals_json_endpoint(client):
    r = client.get("/api/algo/signals", params={"date": "2026-09-08", "zone": "z1",
                                                "indicator": "ratio", "limit": 50})
    assert r.status_code == 200
    body = r.json()
    assert body["date"] == "2026-09-08" and body["zone"] == "Z1"
    assert body["rows"][0]["indicator"] == "ratio" and body["rows"][0]["reading"] == "CALL"
    assert client.calls["signals_filtered"] == {
        "trade_date": "2026-09-08", "zone": "Z1", "indicator": "ratio", "limit": 50}
    assert client.get("/api/algo/signals", params={"date": "bad"}).status_code == 400
    assert client.get("/api/algo/signals", params={"limit": 5000}).status_code == 422
    assert client.get("/api/algo/signals", params={"limit": 0}).status_code == 422
