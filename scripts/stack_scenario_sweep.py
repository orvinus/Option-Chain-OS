"""Total scenario-matrix sweep of the RUNNING stack — every page, every
sub-tab, happy paths, boundaries and error paths.

Run (repo root; backend deps on PYTHONPATH not required — pure HTTP/WS):

    $env:ALGO_SESSION  = '<admin session token>'   # minted via the login API
    $env:ALGO_SESSION2 = '<second token>'          # consumed by the logout test
    python scripts/stack_scenario_sweep.py

DELIBERATE EXCLUSIONS
  * POST /api/auth/login — in RUN_MODE=live this triggers a REAL XTS
    market-data login. A second session on the production appKey is a
    documented outage cause. NEVER call it from a test.
  * POST /api/active-symbol with a different symbol — it re-points the LIVE
    feed subscriptions. Only the no-op (current symbol) is exercised.
  * /api/verify/* NSE cross-checks — external calls, weekend-flaky.

Writes performed (all admin-audited, all benign): one no-change config save,
config-lock ON→OFF saves, a paper-session reset (ledger is append-only and
the reset is the sanctioned delete), two day-notes that are removed again.
Every §15-violation save must be REJECTED (nothing persisted).
"""
from __future__ import annotations

import copy
import json
import os
import sys

import httpx

API = "http://127.0.0.1:8000"
ALGO = f"{API}/api/algo"
HIST_DATE = "2026-08-13"


def hist_expiry(pub: "httpx.Client") -> str:
    """The nearest stored expiry at/after HIST_DATE — the contract that traded
    that day. The default (current) expiry rolls every week and, a month
    later, did not exist on HIST_DATE at all."""
    try:
        exps = pub.get(f"{API}/api/expiries", params={"symbol": "NIFTY"}, timeout=30).json()["expiries"]
        return sorted(e for e in exps if e >= HIST_DATE)[0]
    except Exception:  # noqa: BLE001
        return ""
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday"]
ZONES = ["Z1", "Z2", "Z3"]

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""), flush=True)


def expect_status(cl: httpx.Client, name: str, method: str, url: str,
                  want: int | tuple[int, ...], **kw) -> httpx.Response:
    r = cl.request(method, url, **kw)
    wants = want if isinstance(want, tuple) else (want,)
    check(name, r.status_code in wants, f"{r.status_code} (want {want})")
    return r


# ═══════════════════════════ S1 — ALGO CONFIG ═══════════════════════════

def s1_algo(cl: httpx.Client, cookie2: str) -> None:
    # ---- auth scenarios ----
    me = cl.get(f"{ALGO}/auth/me")
    check("S1 auth/me", me.status_code == 200 and me.json()["role"] == "admin", str(me.json()))
    expect_status(cl, "S1 wrong password → 401", "POST", f"{ALGO}/auth/login",
                  401, json={"username": "ayush", "password": "definitely-wrong"})
    bad = httpx.get(f"{ALGO}/config", cookies={"algo_session": "1.999.deadbeef"}, timeout=10)
    check("S1 garbage token → 401", bad.status_code == 401, f"{bad.status_code}")
    lo = httpx.post(f"{ALGO}/auth/logout", cookies={"algo_session": cookie2}, timeout=10)
    check("S1 logout (2nd session)", lo.status_code == 200 and lo.json()["status"] == "ok", "")

    # ---- config CRUD + §15 violation classes (every one must be REJECTED) ----
    env = cl.get(f"{ALGO}/config").json()
    base_doc = env["config"]
    v_start = env["version"]
    check("S1 config GET", v_start >= 7, f"v{v_start}")

    sv = cl.post(f"{ALGO}/config", json={"config": base_doc, "note": "sweep: no-change save"})
    check("S1 no-change save allowed", sv.status_code == 200, f"v{sv.json().get('version')}")

    def violate(name: str, needle: str, mutate) -> None:
        doc = copy.deepcopy(base_doc)
        mutate(doc)
        r = cl.post(f"{ALGO}/config", json={"config": doc, "note": f"sweep-violation {name}"})
        errs = []
        if r.status_code == 422:
            detail = r.json().get("detail", {})
            errs = detail.get("errors", []) if isinstance(detail, dict) else []
        ok = r.status_code == 422 and any(needle in e for e in errs)
        check(f"S1 §15 reject: {name}", ok,
              f"{r.status_code} {errs[:1] if errs else r.text[:60]}")

    violate("zone overlap", "overlap",
            lambda d: d["days"]["monday"]["zones"]["Z2"].__setitem__("start", "10:00"))
    violate("start after end", "before end",
            lambda d: d["days"]["monday"]["zones"]["Z1"].__setitem__("end", "09:00"))
    violate("outside exchange window", "outside 09:15",
            lambda d: d["days"]["monday"]["zones"]["Z1"].__setitem__("start", "09:00"))
    violate("inverted premium band", "premium",
            lambda d: d["days"]["monday"]["zones"]["Z1"].__setitem__("premium_min", 500.0))
    violate("max trades zero", "max trades",
            lambda d: d["days"]["monday"]["zones"]["Z1"].__setitem__("max_trades", 0))
    violate("bad holiday date", "not a valid",
            lambda d: d["global"]["holidays"].append({"date": "garbage", "occasion": "x"}))
    violate("negative brokerage", "negative",
            lambda d: d["global"]["fees"].__setitem__("brokerage_per_order", -1.0))
    violate("allocation 150%", "allocation",
            lambda d: d["days"]["monday"].__setitem__("allocation_pct", 150.0))
    violate("paper balance zero", "virtual balance",
            lambda d: d["global"]["paper"].__setitem__("virtual_balance", 0.0))

    expect_status(cl, "S1 restore nonexistent version", "POST", f"{ALGO}/config/restore",
                  (404, 422, 500), json={"version": 99999})
    expect_status(cl, "S1 fetch nonexistent version", "GET", f"{ALGO}/config/versions/99999", 404)
    d1 = cl.get(f"{ALGO}/config/defaults").json()
    d2 = cl.get(f"{ALGO}/config/defaults").json()
    d1.get("global", {}).get("paper", {}).pop("session_started", None)
    d2.get("global", {}).get("paper", {}).pop("session_started", None)
    check("S1 defaults deterministic", json.dumps(d1, sort_keys=True) == json.dumps(d2, sort_keys=True), "")

    # ---- Config Lock scenario (market is CLOSED — lock must not bite) ----
    doc = copy.deepcopy(cl.get(f"{ALGO}/config").json()["config"])
    doc["global"]["config_lock"]["lock_market_hours"] = True
    r1 = cl.post(f"{ALGO}/config", json={"config": doc, "note": "sweep: lock ON"})
    check("S1 lock ON saves", r1.status_code == 200, f"v{r1.json().get('version')}")
    doc2 = copy.deepcopy(cl.get(f"{ALGO}/config").json()["config"])
    doc2["global"]["demat_balance"] = doc2["global"]["demat_balance"]  # no-op field touch
    doc2["global"]["config_lock"]["lock_market_hours"] = False
    r2 = cl.post(f"{ALGO}/config", json={"config": doc2, "note": "sweep: lock OFF"})
    check("S1 lock OFF saves (off-hours)", r2.status_code == 200, f"v{r2.json().get('version')}")

    # ---- engines: FULL 15-zone × 4-engine matrix on the historical day ----
    # (pinned to the expiry that traded on HIST_DATE — the default "current"
    # expiry rolls weekly and did not exist on that date a month later)
    hx = hist_expiry(cl)
    for eng in ("oi-structure", "mtf-ratio", "mqae", "ump"):
        bad_zone = 0
        for day in DAYS:
            for z in ZONES:
                r = cl.get(f"{ALGO}/engines/{eng}",
                           params={"day": day, "zone": z, "date": HIST_DATE,
                                   **({"expiry": hx} if hx else {})})
                if r.status_code != 200:
                    bad_zone += 1
        check(f"S1 {eng} × 15 zones ({HIST_DATE})", bad_zone == 0,
              f"{15 - bad_zone}/15 OK")

    # ---- engine error paths ----
    e = f"{ALGO}/engines/oi-structure"
    expect_status(cl, "S1 day=sunday → 400", "GET", e, 400, params={"day": "sunday", "zone": "Z1"})
    expect_status(cl, "S1 zone=Z9 → 400", "GET", e, 400, params={"day": "friday", "zone": "Z9"})
    expect_status(cl, "S1 date=13-08-2026 → 400", "GET", e, 400,
                  params={"day": "friday", "zone": "Z1", "date": "13-08-2026"})
    expect_status(cl, "S1 date=2026-01-01 → 404 honest", "GET", e, 404,
                  params={"day": "friday", "zone": "Z1", "date": "2026-01-01"})
    expect_status(cl, "S1 expiry=garbage → 400", "GET", e, 400,
                  params={"day": "friday", "zone": "Z1", "expiry": "garbage"})
    m = f"{ALGO}/engines/mqae"
    expect_status(cl, "S1 mqae at=25:99 → 422", "GET", m, 422,
                  params={"day": "friday", "zone": "Z1", "at": "25:99"})
    expect_status(cl, "S1 mqae at=08:45 → 404", "GET", m, 404,
                  params={"day": "wednesday", "zone": "Z1", "date": HIST_DATE, "at": "08:45"})
    # The historical window starts at the 09:15 session open (the old
    # "pre-open bars at 08:52" observation was the pytz-LMT +05:53 window
    # shift, fixed in _window_for_date) — a 09:00 cutoff has NO session data
    # and must refuse honestly; 09:20 replays the true opening minutes.
    expect_status(cl, "S1 mqae at=09:00 pre-session → 404", "GET", m, 404,
                  params={"day": "wednesday", "zone": "Z1", "date": HIST_DATE, "at": "09:00"})
    pre = cl.get(m, params={"day": "wednesday", "zone": "Z1", "date": HIST_DATE, "at": "09:20"})
    check("S1 mqae at=09:20 opening minutes honest",
          pre.status_code == 200 and pre.json()["timestamps"][-1][11:16] <= "09:20",
          f"{pre.status_code} last={pre.json()['timestamps'][-1][11:16] if pre.status_code == 200 else '-'}")
    u = f"{ALGO}/engines/ump"
    expect_status(cl, "S1 ump option_type=XX → 422", "GET", u, 422,
                  params={"day": "friday", "zone": "Z1", "option_type": "XX"})
    expect_status(cl, "S1 ump strike=90000 → 404 honest", "GET", u, 404,
                  params={"day": "friday", "zone": "Z1", "strike": 90000})

    # ---- notes scenarios ----
    uni = "टेस्ट नोट 📝 — बहुत बढ़िया " + "x" * 2000
    cl.put(f"{ALGO}/notes/2026-08-12", json={"note": uni})
    got = cl.get(f"{ALGO}/notes", params={"month": "2026-08"}).json()
    check("S1 unicode+2000-char note", got.get("2026-08-12") == uni, f"{len(got.get('2026-08-12', ''))} chars")
    cl.put(f"{ALGO}/notes/2026-08-12", json={"note": "overwritten"})
    check("S1 note overwrite",
          cl.get(f"{ALGO}/notes", params={"month": "2026-08"}).json().get("2026-08-12") == "overwritten", "")
    cl.put(f"{ALGO}/notes/2026-08-12", json={"note": ""})
    check("S1 note delete",
          "2026-08-12" not in cl.get(f"{ALGO}/notes", params={"month": "2026-08"}).json(), "")
    expect_status(cl, "S1 note bad date → 400", "PUT", f"{ALGO}/notes/garbage", 400, json={"note": "x"})
    expect_status(cl, "S1 notes bad month → 400", "GET", f"{ALGO}/notes", 400, params={"month": "garbage"})

    # ---- P&L / trades scenarios ----
    expect_status(cl, "S1 ledger=bogus → 400", "GET", f"{ALGO}/trades", 400, params={"ledger": "bogus"})
    cal = cl.get(f"{ALGO}/pnl/calendar", params={"month": "2026-01"}).json()
    check("S1 empty-month calendar", cal["days"] == {}, "")
    expect_status(cl, "S1 calendar bad month → 400", "GET", f"{ALGO}/pnl/calendar", 400,
                  params={"month": "Aug-2026"})
    inv = cl.get(f"{ALGO}/trades", params={"from_date": "2026-08-20", "to_date": "2026-08-01"}).json()
    check("S1 inverted range → empty", inv == [], "")
    expect_status(cl, "S1 limit=0 → 422", "GET", f"{ALGO}/trades", 422, params={"limit": "0"})
    expect_status(cl, "S1 limit=2000 OK", "GET", f"{ALGO}/trades", 200, params={"limit": "2000"})
    for combo in ({"date": "2026-08-14"}, {"day": "friday"}, {"zone": "Z1"},
                  {"ledger": "paper", "day": "friday", "zone": "Z2"}):
        r = cl.get(f"{ALGO}/trades", params=combo)
        check(f"S1 trades filter {list(combo.keys())}", r.status_code == 200, f"rows={len(r.json())}")
    for led in ("paper", "live"):
        expect_status(cl, f"S1 pnl summary {led}", "GET", f"{ALGO}/pnl/summary", 200,
                      params={"ledger": led})

    # ---- paper reset scenario ----
    before = cl.get(f"{ALGO}/config").json()["version"]
    pr = cl.post(f"{ALGO}/paper/reset")
    ps = cl.get(f"{ALGO}/paper/session").json()
    check("S1 paper reset", pr.status_code == 200 and pr.json()["deleted_rows"] >= 0
          and ps["session_started"] >= "2026-08-14"
          and cl.get(f"{ALGO}/config").json()["version"] == before + 1,
          f"deleted={pr.json().get('deleted_rows')} session={ps['session_started']}")

    # ---- broker + orchestrator ----
    bs = cl.get(f"{ALGO}/broker/status").json()
    check("S1 broker status", bs["configured"] is True, bs["detail"])
    # The order book is TODAY-scoped (empty on weekends/quiet days) and the
    # gateway itself may refuse logins off-hours — assert the CONTRACT, and
    # accept the gateway's honest 5xx outside sessions.
    acct = cl.get(f"{ALGO}/broker/account").json()
    check("S1 broker account contract",
          {"connected", "balance", "positions", "orders"} <= set(acct)
          and isinstance(acct["orders"], list),
          f"connected={acct.get('connected')} orders={len(acct.get('orders', []))}")
    rc = cl.get(f"{ALGO}/broker/reconcile").json()
    check("S1 broker reconcile", rc["match"] is None, rc["detail"][:50])
    tl = cl.post(f"{ALGO}/broker/test-login")
    check("S1 broker test-login answers honestly (200, or 5xx off-hours)",
          tl.status_code == 200 or (500 <= tl.status_code < 600 and tl.text),
          f"{tl.status_code}")
    st = cl.get(f"{ALGO}/status").json()
    check("S1 orchestrator status", st["running"] is True, f"state={st['state']}")
    expect_status(cl, "S1 resume", "POST", f"{ALGO}/resume", 200)

    # ---- audit boundaries + coverage of this sweep's writes ----
    a1 = cl.get(f"{ALGO}/audit", params={"limit": "1"}).json()
    a200 = cl.get(f"{ALGO}/audit", params={"limit": "200"}).json()
    kinds = {a["event_type"] for a in a200}
    check("S1 audit limits", len(a1) == 1 and len(a200) <= 200, f"{len(a200)} rows")
    need = {"config_change", "day_note", "paper_reset", "login_failed",
            "broker_test_login", "engine_resume", "logout"}
    check("S1 audit covers sweep writes", need.issubset(kinds),
          f"missing={sorted(need - kinds)}" if not need.issubset(kinds) else "all present")


# ═══════════════════════ S2 — MAIN DASHBOARD PAGES ═══════════════════════

def s2_pages(pub: httpx.Client) -> None:
    h = pub.get(f"{API}/api/health").json()
    check("S2 health contract", all(k in h for k in
          ("status", "run_mode", "feed_connected", "db_ok", "poller_mode")), "")
    hs = pub.get(f"{API}/api/health/strict")
    check("S2 health/strict off-session", hs.status_code == 200,
          f"{hs.status_code} {hs.json().get('reasons', '')[:60] if hs.status_code != 200 else ''}")
    expect_status(pub, "S2 health/feed", "GET", f"{API}/api/health/feed", 200)

    sy = pub.get(f"{API}/api/symbols").json()
    check("S2 symbols registry", sy.get("active_symbol") == "NIFTY", f"active={sy.get('active_symbol')}")
    expect_status(pub, "S2 spot NIFTY", "GET", f"{API}/api/spot", 200, params={"symbol": "NIFTY"})
    expect_status(pub, "S2 spot SENSEX", "GET", f"{API}/api/spot", 200, params={"symbol": "SENSEX"})
    expect_status(pub, "S2 spot FOO → 404", "GET", f"{API}/api/spot", 404, params={"symbol": "FOO"})
    ex = pub.get(f"{API}/api/expiries", params={"symbol": "NIFTY"}).json()["expiries"]
    check("S2 expiries NIFTY", "2026-08-18" in ex, f"{len(ex)}")
    expect_status(pub, "S2 expiries SENSEX", "GET", f"{API}/api/expiries", 200, params={"symbol": "SENSEX"})
    expect_status(pub, "S2 expiries FOO → 404", "GET", f"{API}/api/expiries", 404, params={"symbol": "FOO"})
    expect_status(pub, "S2 active-symbol no-op", "POST", f"{API}/api/active-symbol", 200,
                  json={"symbol": "NIFTY"})
    expect_status(pub, "S2 active-symbol FOO → 404", "POST", f"{API}/api/active-symbol", 404,
                  json={"symbol": "FOO"})

    # ---- OI Change page ----
    oc = f"{API}/api/oi-change"
    for tf in ("1m", "5m", "15m", "full_day"):
        expect_status(pub, f"S2 oi-change {tf}", "GET", oc, 200, params={"timeframe": tf})
    expect_status(pub, "S2 oi-change bad tf → 400", "GET", oc, 400, params={"timeframe": "7m"})
    ao = pub.get(oc, params={"as_of": f"{HIST_DATE}T14:00:00+05:30"})
    ok = ao.status_code == 200 and ao.json().get("asof", "") <= f"{HIST_DATE}T14:00:00+05:30"
    check("S2 oi-change as_of history (no future data)", ok,
          f"asof={ao.json().get('asof') if ao.status_code == 200 else ao.status_code}")
    expect_status(pub, "S2 oi-change window", "GET", oc, 200,
                  params={"from_ts": f"{HIST_DATE}T09:15:00+05:30", "to_ts": f"{HIST_DATE}T11:00:00+05:30"})
    expect_status(pub, "S2 oi-change open-ended past window → 400", "GET", oc, 400,
                  params={"from_ts": f"{HIST_DATE}T09:15:00+05:30"})
    expect_status(pub, "S2 oi-change SENSEX", "GET", oc, 200, params={"symbol": "SENSEX"})

    # ---- Charts page ----
    ot = f"{API}/api/oi-timeseries"
    expect_status(pub, "S2 oi-timeseries missing strikes → 422", "GET", ot, 422)
    expect_status(pub, "S2 oi-timeseries 1m", "GET", ot, 200,
                  params={"strike_min": "22000", "strike_max": "27200", "bucket": "1m"})
    expect_status(pub, "S2 oi-timeseries bad bucket → 400", "GET", ot, 400,
                  params={"strike_min": "22000", "strike_max": "27200", "bucket": "2m"})
    expect_status(pub, "S2 oi-timeseries window", "GET", ot, 200,
                  params={"strike_min": "22000", "strike_max": "27200",
                          "from_ts": f"{HIST_DATE}T09:15:00+05:30", "to_ts": f"{HIST_DATE}T15:30:00+05:30"})

    # ---- Ratio page ----
    rt = f"{API}/api/ratio-timeseries"
    expect_status(pub, "S2 ratio-timeseries default", "GET", rt, 200)
    expect_status(pub, "S2 ratio-timeseries 5m+bounds", "GET", rt, 200,
                  params={"bucket": "5m", "strike_min": "23000", "strike_max": "26000"})
    expect_status(pub, "S2 ratio-timeseries window", "GET", rt, 200,
                  params={"from_ts": f"{HIST_DATE}T09:15:00+05:30", "to_ts": f"{HIST_DATE}T15:30:00+05:30"})

    # ---- Multi-TF page ----
    mt = f"{API}/api/multi-timeframe"
    full = pub.get(mt).json()
    check("S2 multi-timeframe default (10 TFs)", len(full.get("rows", [])) == 10,
          f"{len(full.get('rows', []))} rows")
    sub = pub.get(mt, params={"timeframes": "5m,1h"}).json()
    check("S2 multi-timeframe subset", [r["timeframe"] for r in sub["rows"]] == ["5m", "1h"], "")
    unk = pub.get(mt, params={"timeframes": "5m,7x"})
    check("S2 multi-timeframe unknown token behavior",
          unk.status_code in (200, 400),
          f"{unk.status_code} rows={len(unk.json().get('rows', [])) if unk.status_code == 200 else '-'}")
    expect_status(pub, "S2 multi-timeframe as_of", "GET", mt, 200,
                  params={"as_of": f"{HIST_DATE}T14:00:00+05:30"})
    expect_status(pub, "S2 multi-timeframe atm_window", "GET", mt, 200, params={"atm_window": "5"})

    # ---- history-dates ----
    hx = hist_expiry(pub)
    hd = pub.get(f"{API}/api/history-dates", params={"expiry": hx} if hx else None).json()
    dates = hd.get("dates", hd if isinstance(hd, list) else [])
    have = {d for d in ("2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14") if d in dates}
    check("S2 history-dates backfilled days", len(have) == 4, f"present={sorted(have)}")

    # ---- option chain ----
    expect_status(pub, "S2 option-chain", "GET", f"{API}/api/option-chain", 200)
    ocf = pub.get(f"{API}/api/option-chain-full", params={"timeframe": "5m"}).json()
    check("S2 option-chain-full contract",
          all(k in ocf for k in ("lot_size", "synthetic_future", "atm_iv", "rows")),
          f"lot={ocf.get('lot_size')}")
    expect_status(pub, "S2 option-chain SENSEX", "GET", f"{API}/api/option-chain", 200,
                  params={"symbol": "SENSEX"})

    # ---- Replay page ----
    rp = f"{API}/api/replay"
    fr = pub.get(rp, params={"start": f"{HIST_DATE}T09:15:00+05:30",
                             "end": f"{HIST_DATE}T10:15:00+05:30", "step": "5m",
                             **({"expiry": hx} if hx else {})})
    frames = fr.json().get("frames", []) if fr.status_code == 200 else []
    check("S2 replay 13 frames", fr.status_code == 200 and len(frames) == 13, f"{len(frames)} frames")
    expect_status(pub, "S2 replay summary", "GET", rp, 200,
                  params={"start": f"{HIST_DATE}T09:15:00+05:30",
                          "end": f"{HIST_DATE}T10:15:00+05:30", "step": "5m", "summary": "true"})
    expect_status(pub, "S2 replay with_greeks", "GET", rp, 200,
                  params={"start": f"{HIST_DATE}T09:15:00+05:30",
                          "end": f"{HIST_DATE}T09:45:00+05:30", "step": "5m", "with_greeks": "true"})
    expect_status(pub, "S2 replay start>=end → 400", "GET", rp, 400,
                  params={"start": f"{HIST_DATE}T10:15:00+05:30", "end": f"{HIST_DATE}T09:15:00+05:30"})
    expect_status(pub, "S2 replay bad step → 400", "GET", rp, 400,
                  params={"start": f"{HIST_DATE}T09:15:00+05:30",
                          "end": f"{HIST_DATE}T10:15:00+05:30", "step": "7m"})
    expect_status(pub, "S2 replay >5000 frames → 400", "GET", rp, 400,
                  params={"start": f"{HIST_DATE}T09:15:00+05:30",
                          "end": f"{HIST_DATE}T15:30:00+05:30", "step": "1s"})

    # ---- misc pages/services ----
    expect_status(pub, "S2 interpretation", "GET", f"{API}/api/interpretation", 200)
    expect_status(pub, "S2 iv-scanner", "GET", f"{API}/api/iv-scanner", 200,
                  params={"symbols": "NIFTY"})
    expect_status(pub, "S2 iv-scanner missing symbols → 422", "GET", f"{API}/api/iv-scanner", 422)
    expect_status(pub, "S2 verify/ping", "GET", f"{API}/api/verify/ping", 200)
    expect_status(pub, "S2 main-login wrong creds → 401", "POST", f"{API}/api/auth/main-login",
                  401, json={"username": "x", "password": "y"})
    fe = httpx.get("http://127.0.0.1/", timeout=10)
    check("S2 frontend SPA serves", fe.status_code == 200 and "NIFTY OI Analytics" in fe.text, "")


# ═══════════════════════ S3 — CROSS-PAGE CONSISTENCY ═════════════════════

def s3_consistency(cl: httpx.Client, pub: httpx.Client) -> None:
    current = pub.get(f"{API}/api/expiries", params={"symbol": "NIFTY"}).json()["expiries"]
    today = __import__("datetime").date.today().isoformat()
    upcoming = sorted(e for e in current if e >= today)[0]
    engines_exp = {
        eng: cl.get(f"{ALGO}/engines/{eng}", params={"day": "friday", "zone": "Z1"}).json()["expiry"]
        for eng in ("oi-structure", "mtf-ratio", "mqae", "ump")
    }
    check("S3 expiry agreement across pages",
          all(v == upcoming for v in engines_exp.values()),
          f"current={upcoming} engines={set(engines_exp.values())}")

    fr = pub.get(f"{API}/api/replay",
                 params={"start": f"{HIST_DATE}T11:00:00+05:30",
                         "end": f"{HIST_DATE}T12:00:00+05:30", "step": "15m"}).json()
    check("S3 replay frame-count law", len(fr.get("frames", [])) == 5,
          f"{len(fr.get('frames', []))} (want 5)")


# ═══════════════════════ S4 — WEBSOCKET SCENARIOS ════════════════════════

def s4_websocket() -> None:
    try:
        import asyncio

        import websockets
    except ImportError:
        check("S4 websockets lib", False, "websockets not importable on host")
        return

    async def run() -> None:
        uri = "ws://127.0.0.1:8000/ws/oi-stream?timeframe=5m"
        async with websockets.connect(uri, open_timeout=10) as ws:
            kinds = set()
            for _ in range(3):
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                kinds.add(msg.get("type"))
                if {"oi_change", "option_chain_full"} <= kinds:
                    break
            check("S4 WS initial snapshots", {"oi_change", "option_chain_full"} <= kinds,
                  f"got {sorted(kinds)}")
            await ws.send("set:tf=15m")
            kinds2 = set()
            for _ in range(3):
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                kinds2.add(msg.get("type"))
                if "oi_change" in kinds2:
                    break
            check("S4 WS set: swap re-pushes", "oi_change" in kinds2, f"got {sorted(kinds2)}")
        # bad timeframe → error frame + close
        async with websockets.connect(
            "ws://127.0.0.1:8000/ws/oi-stream?timeframe=7m", open_timeout=10
        ) as ws:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            check("S4 WS bad timeframe rejected", msg.get("type") == "error", str(msg)[:60])
        async with websockets.connect(
            "ws://127.0.0.1:8000/ws/oi-stream?symbol=FOO", open_timeout=10
        ) as ws:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            check("S4 WS unknown symbol rejected", msg.get("type") == "error", str(msg)[:60])

    import asyncio
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run())


# ═══════════════════════════ S5 — BACKTESTING ═══════════════════════════

def s5_backtest(cl: httpx.Client) -> None:
    """Full backtest lifecycle on a tiny 2-day window + every error path.
    Creates ONE run and deletes it at the end (the sanctioned cleanup —
    dedicated tables, cascade delete, never touches live/paper ledgers).
    Skipped politely when another run is active (single-flight)."""
    import time as _time

    body = {
        "label": "sweep-s5", "from_date": "2026-08-12", "to_date": "2026-08-13",
        "config": {"source": "live"}, "settings": {"balance_mode": "compounding"},
    }

    # -- error paths first (no run created) --
    expect_status(cl, "S5 bad range → 400", "POST", f"{ALGO}/backtest/runs",
                  400, json={**body, "from_date": "2026-08-14", "to_date": "2026-08-12"})
    expect_status(cl, "S5 garbage date → 400", "POST", f"{ALGO}/backtest/runs",
                  400, json={**body, "from_date": "garbage"})
    expect_status(cl, "S5 unknown config version → 404", "POST", f"{ALGO}/backtest/runs",
                  404, json={**body, "config": {"source": "version", "version": 999999}})
    expect_status(cl, "S5 unknown run → 404", "GET", f"{ALGO}/backtest/runs/999999", 404)
    expect_status(cl, "S5 cancel of unknown run → 404", "POST",
                  f"{ALGO}/backtest/runs/999999/cancel", 404)

    # -- preflight contract --
    pf = expect_status(cl, "S5 preflight → 200", "POST", f"{ALGO}/backtest/preflight",
                       200, json=body).json()
    check("S5 preflight day verdicts", isinstance(pf.get("days"), list) and pf.get("planned", 0) >= 1,
          f"planned={pf.get('planned')} skipped={pf.get('skipped')}")
    check("S5 preflight carries default exclusions",
          "NIFTY" in (pf.get("default_exclusions") or {}), str(list((pf.get("default_exclusions") or {}))))

    # -- lifecycle (skipped if a real run is active) --
    r = cl.post(f"{ALGO}/backtest/runs", json=body)
    if r.status_code == 409:
        check("S5 single-flight honoured (another run active — lifecycle skipped)", True,
              r.json().get("detail", "")[:60])
        return
    check("S5 create run → 200", r.status_code == 200, str(r.status_code))
    run_id = r.json()["id"]
    expect_status(cl, "S5 double-run → 409", "POST", f"{ALGO}/backtest/runs", 409, json=body)
    expect_status(cl, "S5 delete-while-running → 409", "POST",
                  f"{ALGO}/backtest/runs/{run_id}/delete", 409)

    for _ in range(120):
        st = cl.get(f"{ALGO}/backtest/runs/{run_id}").json()
        if st["status"] in ("done", "error", "cancelled"):
            break
        _time.sleep(2)
    check("S5 run completes", st["status"] == "done", f"{st['status']} {st.get('error','')[:80]}")
    check("S5 progress complete", st["days_done"] == st["days_total"] and st["days_total"] >= 1,
          f"{st['days_done']}/{st['days_total']}")
    s = st.get("summary") or {}
    check("S5 summary contract", all(k in s for k in
          ("trades", "net_pnl", "final_equity", "max_drawdown", "equity", "preflight")),
          str(sorted(s.keys()))[:100])

    days = cl.get(f"{ALGO}/backtest/runs/{run_id}/days").json()
    done_days = [d for d in days if d["status"] == "done"]
    check("S5 day rows present", len(done_days) >= 1, f"{len(done_days)} done of {len(days)}")

    trades = cl.get(f"{ALGO}/backtest/runs/{run_id}/trades").json()
    check("S5 trades TradeRow-shaped", isinstance(trades, list) and all(
        {"id", "trade_date", "zone_id", "entry_ts", "ledger"} <= set(t) for t in trades),
        f"{len(trades)} trades")

    ps = cl.get(f"{ALGO}/backtest/runs/{run_id}/pnl/summary").json()
    check("S5 pnl summary PnlSummary-shaped",
          all(k in ps for k in ("trades", "pnl", "by_day", "by_zone", "by_date")), "")
    pc = cl.get(f"{ALGO}/backtest/runs/{run_id}/pnl/calendar", params={"month": "2026-08"}).json()
    check("S5 pnl calendar PnlCalendar-shaped", "days" in pc and pc.get("month") == "2026-08", "")
    expect_status(cl, "S5 calendar bad month → 400", "GET",
                  f"{ALGO}/backtest/runs/{run_id}/pnl/calendar", 400, params={"month": "garbage"})

    eq = cl.get(f"{ALGO}/backtest/runs/{run_id}/equity").json()
    check("S5 equity curve", isinstance(eq.get("points"), list) and len(eq["points"]) == len(done_days), "")

    if done_days:
        day = done_days[0]["trade_date"]
        b = cl.get(f"{ALGO}/backtest/runs/{run_id}/day/{day}").json()
        check("S5 day replay bundle contract", all(k in b for k in
              ("zones", "status_timeline", "signals", "alerts", "trades", "equity")),
              f"{len(b.get('status_timeline', []))} status transitions, {len(b.get('signals', []))} signals")
        check("S5 bundle has real engine activity",
              len(b.get("signals", [])) > 0 and len(b.get("status_timeline", [])) > 0, "")
        expect_status(cl, "S5 bundle for unknown day → 404", "GET",
                      f"{ALGO}/backtest/runs/{run_id}/day/2026-01-01", 404)

    # engine-dashboard pins (the day-replay deep links)
    ev = cl.get(f"{ALGO}/engines/mtf-ratio", params={
        "day": "wednesday", "zone": "Z1", "date": "2026-08-12", "at": "11:30",
        "config_version": st.get("config_version"),
    })
    check("S5 engine deep-link (date+at+config_version) → 200",
          ev.status_code == 200 and ev.json().get("replay_at") == "11:30",
          f"{ev.status_code} as_of={ev.json().get('as_of', '')[:16]}")

    expect_status(cl, "S5 cancel of finished run → 409", "POST",
                  f"{ALGO}/backtest/runs/{run_id}/cancel", 409)
    expect_status(cl, "S5 resume of finished run → 409", "POST",
                  f"{ALGO}/backtest/runs/{run_id}/resume", 409)
    expect_status(cl, "S5 delete run → 200", "POST", f"{ALGO}/backtest/runs/{run_id}/delete", 200)
    expect_status(cl, "S5 deleted run gone → 404", "GET", f"{ALGO}/backtest/runs/{run_id}", 404)


# ═══════════════════════ S6 — DATA HEALTH + DECISION TRACE (2026-09-09) ═════

def s6_data_and_decisions(cl: httpx.Client, pub: httpx.Client) -> None:
    r = pub.get(f"{API}/api/health/data", timeout=60)
    check("S6 /api/health/data → 200 with coverage/gapfill/catchup/integrity",
          r.status_code == 200 and all(k in r.json() for k in ("coverage", "gapfill", "catchup", "integrity")),
          str(r.status_code))
    r = pub.get(f"{API}/api/health/feed", timeout=30)
    check("S6 /api/health/feed carries gapfill + aggregator keys",
          r.status_code == 200 and "gapfill" in r.json() and "aggregator" in r.json(), str(r.status_code))
    r = pub.get(f"{API}/api/health/data/integrity", params={"symbol": "NIFTY", "day": "2026-09-01"}, timeout=120)
    ok = r.status_code == 200 and len((r.json().get("categories") or {})) == 10
    check("S6 integrity report has the 10 categories", ok, str(r.status_code))
    r = pub.get(f"{API}/api/health/data/integrity", params={"symbol": "NIFTY", "day": "garbage"})
    check("S6 integrity bad day → 400", r.status_code == 400, str(r.status_code))
    r = cl.get(f"{ALGO}/decisions", params={"limit": "5"})
    check("S6 /api/algo/decisions → 200 rows[]", r.status_code == 200 and "rows" in r.json(), str(r.status_code))
    r = cl.get(f"{ALGO}/decisions", params={"date": "bad"})
    check("S6 decisions bad date → 400", r.status_code == 400, str(r.status_code))
    r = cl.get(f"{ALGO}/decisions/latest")
    check("S6 decisions/latest → 200", r.status_code == 200 and "row" in r.json(), str(r.status_code))
    r = pub.get(f"{ALGO}/decisions")
    check("S6 decisions anonymous → 401", r.status_code == 401, str(r.status_code))
    # stale-draft guard: a save with an old base_version must be refused
    live = cl.get(f"{ALGO}/config").json()
    r = cl.post(f"{ALGO}/config", json={"config": live["config"], "note": "sweep: stale base",
                                        "base_version": max(1, live["version"] - 1)})
    check("S6 config save with stale base_version → 409", r.status_code == 409, str(r.status_code))
    r = cl.post(f"{ALGO}/config", json={"config": live["config"], "note": "sweep: current base",
                                        "base_version": live["version"]})
    check("S6 config save with current base_version → 200", r.status_code == 200, str(r.status_code))
    runs = cl.get(f"{ALGO}/backtest/runs").json()
    done = [x for x in runs if x.get("status") == "done"]
    if done:
        rid = done[0]["id"]
        r = cl.get(f"{ALGO}/backtest/runs/{rid}/decisions", params={"compact": "true"})
        check("S6 run decisions (compact) → 200 list", r.status_code == 200 and isinstance(r.json(), list), str(r.status_code))
        days = cl.get(f"{ALGO}/backtest/runs/{rid}/days").json()
        dd = next((d for d in days if d.get("status") == "done"), None)
        if dd:
            b = cl.get(f"{ALGO}/backtest/runs/{rid}/day/{dd['trade_date']}")
            check("S6 day bundle carries minutes_per_day + decisions_index",
                  b.status_code == 200 and "minutes_per_day" in b.json() and "decisions_index" in b.json(),
                  str(b.status_code))


# ═══════ S7 — UMP REPLAY/INTERVAL, TELEGRAM, EXPORT, DRAWDOWN, PAPER (2026-09-09) ═════

def s7_feature_bundle(cl: httpx.Client, pub: httpx.Client) -> None:
    # UMP: display interval + replay histories (the engine still evaluates its entry TF)
    r = cl.get(f"{ALGO}/engines/ump", params={"day": "monday", "zone": "Z1", "interval": "5m", "history": "true"}, timeout=120)
    ok = r.status_code == 200 and all(
        k in r.json() for k in ("entry_candles", "candles", "candles_interval", "candles_source",
                                "entry_timeframe_min", "levels_history", "state_history")
    )
    check("S7 ump interval=5m&history=true → entry_candles/candles/histories", ok, str(r.status_code))
    if r.status_code == 200:
        j = r.json()
        check("S7 ump candles_interval=300 for 5m", j.get("candles_interval") == 300, str(j.get("candles_interval")))
        check("S7 ump levels_history is a list", isinstance(j.get("levels_history"), list), "")
    r = cl.get(f"{ALGO}/engines/ump", params={"day": "monday", "zone": "Z1", "interval": "1s",
                                              "date": "2026-02-10", "expiry": hist_expiry(pub)}, timeout=120)
    if r.status_code == 200:
        j = r.json()
        check("S7 ump 1s on an archive-only day falls back to 1m with a note",
              j.get("candles_source") == "minutes" and j.get("candles_interval") == 60 and bool(j.get("candles_note")),
              f"{j.get('candles_source')}/{j.get('candles_interval')}")
    else:
        check("S7 ump 1s historical request answered (200/404)", r.status_code in (200, 404), str(r.status_code))
    r = cl.get(f"{ALGO}/engines/ump", params={"day": "monday", "zone": "Z1", "interval": "2m"})
    check("S7 ump bad interval → 422", r.status_code == 422, str(r.status_code))
    for iv, sec in (("10m", 600), ("30m", 1800), ("1h", 3600), ("1d", 86400)):
        r = cl.get(f"{ALGO}/engines/ump", params={"day": "monday", "zone": "Z1", "interval": iv}, timeout=120)
        if r.status_code == 200:
            j = r.json()
            c = j.get("candles") or []
            # Anchored ON the 09:15 grid — NOT necessarily starting at 09:15:
            # a contract's stored life usually begins mid-session, so the first
            # candle is 09:15 + k × interval for some k ≥ 0.
            ok = j.get("candles_interval") == sec and bool(c)
            if ok:
                hh, mm = int(c[0]["ts"][11:13]), int(c[0]["ts"][14:16])
                off = hh * 60 + mm - (9 * 60 + 15)
                ok = off >= 0 and (sec >= 86400 or off % (sec // 60) == 0)
            check(f"S7 ump interval={iv} → {sec}s candles on the 09:15 grid", ok,
                  f"{j.get('candles_interval')} {c[0]['ts'] if c else 'no candles'}")
        else:
            check(f"S7 ump interval={iv} answered", r.status_code in (200, 404), str(r.status_code))
    r = cl.get(f"{ALGO}/engines/ump", params={"day": "monday", "zone": "Z1", "interval": "15s"}, timeout=120)
    if r.status_code == 200:
        j = r.json()
        check("S7 ump interval=15s → live_1s@15s or 1m fallback with note",
              (j.get("candles_source") == "live_1s" and j.get("candles_interval") == 15)
              or (j.get("candles_interval") == 60 and bool(j.get("candles_note"))),
              f"{j.get('candles_source')}/{j.get('candles_interval')}")
    # Engine panel chart extras
    r = cl.get(f"{ALGO}/engines/oi-structure", params={"day": "monday", "zone": "Z1"}, timeout=120)
    if r.status_code == 200:
        j = r.json()
        bs = j.get("by_strike")
        if bs and j.get("call_series_cr"):
            tot = sum(float(x.get("call_oi_change") or 0) for x in bs) / 1e7
            check("S7 oi-structure by_strike Σ CE ≈ call_series_cr[-1] (KNOWN DEFECT 2026-09-10)",
                  abs(tot - float(j["call_series_cr"][-1])) <= 0.02, f"{tot:.3f} vs {j['call_series_cr'][-1]}")
        else:
            check("S7 oi-structure carries by_strike key", "by_strike" in j, "")
    r = cl.get(f"{ALGO}/engines/mqae", params={"day": "monday", "zone": "Z1"}, timeout=120)
    if r.status_code == 200:
        j = r.json()
        check("S7 mqae total_call_oi aligned with timestamps",
              len(j.get("total_call_oi") or []) == len(j.get("timestamps") or []), "")
    # Telegram
    r = cl.get(f"{ALGO}/telegram/status")
    check("S7 telegram/status → 200 with configured/ok/error keys",
          r.status_code == 200 and all(k in r.json() for k in ("configured", "ok", "error", "min_interval_s")), str(r.status_code))
    r = cl.post(f"{ALGO}/telegram/test")
    check("S7 telegram/test → 409 when unset (or 200/502 when configured)", r.status_code in (409, 200, 502), str(r.status_code))
    r = pub.get(f"{ALGO}/telegram/status")
    check("S7 telegram/status anonymous → 401", r.status_code == 401, str(r.status_code))
    # Export
    r = cl.get(f"{ALGO}/export/trades", params={"ledger": "paper", "from_date": "2026-09-01", "to_date": "2026-09-09", "format": "csv"}, timeout=120)
    check("S7 export/trades csv → 200 text/csv attachment",
          r.status_code == 200 and "text/csv" in r.headers.get("content-type", "")
          and "attachment" in r.headers.get("content-disposition", ""), str(r.status_code))
    r = cl.get(f"{ALGO}/export/trades", params={"ledger": "paper", "from_date": "2026-09-01", "to_date": "2026-09-09", "format": "ndjson"}, timeout=120)
    check("S7 export/trades ndjson first line is __headers__",
          r.status_code == 200 and r.text.splitlines()[:1] and "__headers__" in r.text.splitlines()[0], str(r.status_code))
    r = cl.get(f"{ALGO}/export/trades", params={"ledger": "paper", "from_date": "2026-09-09", "to_date": "2026-09-01"})
    check("S7 export from>to → 400", r.status_code == 400, str(r.status_code))
    r = cl.get(f"{ALGO}/export/backtest/999999/trades")
    check("S7 export unknown run → 404", r.status_code == 404, str(r.status_code))
    r = cl.get(f"{ALGO}/signals", params={"limit": "5"})
    check("S7 /api/algo/signals → 200 rows[]", r.status_code == 200 and "rows" in r.json(), str(r.status_code))
    # Drawdown + paper
    r = cl.get(f"{ALGO}/pnl/summary", params={"ledger": "paper"})
    check("S7 pnl/summary carries max_drawdown keys",
          r.status_code == 200 and all(k in r.json() for k in ("max_drawdown", "max_drawdown_pct", "drawdown_basis")), str(r.status_code))
    r = cl.get(f"{ALGO}/paper/session")
    check("S7 paper/session carries open_position/equity",
          r.status_code == 200 and "open_position" in r.json() and "equity" in r.json(), str(r.status_code))
    runs = cl.get(f"{ALGO}/backtest/runs").json()
    done = [x for x in runs if x.get("status") == "done"]
    if done:
        rid = done[0]["id"]
        r = cl.get(f"{ALGO}/export/backtest/{rid}/summary", params={"format": "csv"}, timeout=120)
        check("S7 export backtest summary csv → 200", r.status_code == 200, str(r.status_code))
        r = cl.get(f"{ALGO}/backtest/runs/{rid}/pnl/summary")
        check("S7 backtest pnl/summary passes max_drawdown through",
              r.status_code == 200 and "max_drawdown" in r.json(), str(r.status_code))


def main() -> int:
    cookie = os.environ.get("ALGO_SESSION", "")
    cookie2 = os.environ.get("ALGO_SESSION2", "")
    if not cookie:
        print("set ALGO_SESSION (and ALGO_SESSION2) first")
        return 2
    with httpx.Client(cookies={"algo_session": cookie}, timeout=120.0) as cl, \
         httpx.Client(timeout=60.0) as pub:
        s1_algo(cl, cookie2)
        s2_pages(pub)
        s3_consistency(cl, pub)
        s5_backtest(cl)
        s6_data_and_decisions(cl, pub)
        s7_feature_bundle(cl, pub)
    s4_websocket()

    failed = [r for r in results if not r[1]]
    print(f"\n══ {len(results) - len(failed)}/{len(results)} scenario checks passed ══")
    if failed:
        print("FAILURES:")
        for name, _, detail in failed:
            print(f"  ✗ {name} — {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
