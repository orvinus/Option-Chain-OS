"""Read-only probe of TrueData's realtime WebSocket.

Answers, in ONE market session, every question the live-feed adapter's code shape
depends on. Nothing here writes to the database, subscribes to more than a
handful of symbols, or touches production state.

WHY THIS EXISTS
The vendor's own documentation contradicts itself on almost every wire detail
that matters: the trade frame is 15 fields on one page and 19 on the next, the
touchline rows are 17 fields in three samples and 18 in a fourth, `maxsymbols` is
250 and 900 on the same page, and the option-naming convention appears in no
document at all. Writing a parser against those docs would be writing against a
coin flip. Each answer below is a golden sample on disk that the adapter's unit
tests then pin.

BLOCKING probes — the adapter cannot be written without these:

  B0  proxy reach     does WARP forward to the push host's port at all?
  B1  touchline shape field count, and what index >= 11 actually means. Getting
                      this wrong writes Turnover into the OI column: a ~10^4x
                      corruption that looks like a plausible number.
  B2  trade shape     15 fields (bid/ask off) or 19 (on) on THIS account
  B3  seq semantics   is TickSeqNo per-symbol or per-connection? The only
                      observable for silent feed loss
  B4  expiry list     does getSymbolExpiryList include TODAY on expiry day?
  B5  index names     only "NIFTY BANK" is demonstrated anywhere. If the others
                      are wrong, those indices' spot goes dark with no error
  B6  entitlements    maxsymbols / subscription / segments on THIS port

NON-BLOCKING but needed before the shadow run: N1 ping support, N3 max addsymbol
batch, N5 whether the on-socket logout actually clears the session (this decides
whether the ~60s wedge is a routine cost or an exception path).

USAGE (on the VPS, where the proxy path exists; during market hours for B1-B3):

    ~/.venvs/oi/bin/python scripts/probe_truedata_ws.py
    ~/.venvs/oi/bin/python scripts/probe_truedata_ws.py --port 8084   # paid tier
    ~/.venvs/oi/bin/python scripts/probe_truedata_ws.py --skip-logout # keep session

Golden samples land in logs/probes/<date>/.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import websockets  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.time_utils import IST  # noqa: E402
from app.market.td_identity import INDEX_WS_NAMES, continuous_future_name  # noqa: E402
from app.market_data.truedata_rest import (  # noqa: E402
    TrueDataRest,
    parse_expiry_any,
    parse_option_tail,
)

OUT_DIR = ROOT / "logs" / "probes"

# How long to listen for trade frames. Long enough that a liquid ATM option will
# certainly trade, short enough to run between other work.
LISTEN_SECONDS = 120.0


class Probe:
    def __init__(self, args) -> None:
        self.args = args
        self.out = OUT_DIR / datetime.now(IST).strftime("%Y-%m-%d")
        self.out.mkdir(parents=True, exist_ok=True)
        self.notes: list[str] = []
        self.samples: dict[str, object] = {}
        self.verdicts: dict[str, str] = {}

    # ---------------- reporting ----------------
    def note(self, line: str) -> None:
        print(line, flush=True)
        self.notes.append(line)

    def record(self, probe: str, verdict: str, detail: str) -> None:
        self.verdicts[probe] = verdict
        self.note(f"{verdict:5s} {probe}: {detail}")

    def save(self, name: str, obj: object) -> None:
        path = self.out / f"{name}.json"
        path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
        self.samples[name] = str(path)

    # ---------------- B0: can we even reach the push host ----------------
    async def probe_reach(self) -> bool:
        """A TLS handshake against the push port through whatever proxy is set.

        WARP is proven for auth/history on 443 only. The push host uses a
        non-standard port, and nothing guarantees the vendor's edge treats
        Cloudflare's egress the same way there. If this fails, no amount of
        adapter code helps — the answer is a relay VM or vendor allowlisting.
        """
        uri = f"wss://{self.args.host}:{self.args.port}"
        try:
            async with await asyncio.wait_for(
                self._open(uri + "?user=probe&password=probe"), timeout=20
            ) as ws:
                await ws.close()
            self.record("B0 proxy_reach", "PASS", f"TLS handshake completed to {self.args.host}:{self.args.port}")
            return True
        except websockets.InvalidStatus as e:
            # The server answered — that IS reachability, whatever it said.
            self.record("B0 proxy_reach", "PASS", f"server responded ({e}); path is open")
            return True
        except Exception as e:
            # A rejected login still proves reach; a timeout/reset does not.
            msg = str(e)
            if "Already Connected" in msg or "invalid" in msg.lower():
                self.record("B0 proxy_reach", "PASS", f"vendor replied: {msg[:90]}")
                return True
            self.record("B0 proxy_reach", "FAIL", f"{type(e).__name__}: {msg[:120]}")
            self.note("      -> the push host is unreachable on this path. The live feed "
                      "CANNOT ship until this is solved (relay VM / vendor allowlisting).")
            return False

    def _open(self, uri: str):
        """websockets>=13 handles SOCKS natively; pass proxy explicitly.

        `proxy` defaults to True, which silently reads ALL_PROXY/HTTPS_PROXY from
        the environment — never leave it implicit on a production feed.
        """
        proxy = (settings.truedata_proxy or "").strip() or None
        return websockets.connect(
            uri,
            proxy=proxy,
            ssl=ssl.create_default_context(),
            ping_interval=None,   # the vendor sends its own 5-6s heartbeat
            open_timeout=20,
            close_timeout=5,
        )

    # ---------------- contract discovery (REST) ----------------
    async def discover(self) -> dict:
        """Pick a liquid and an illiquid contract, and capture the expiry list."""
        td = TrueDataRest()
        try:
            symbol = self.args.symbol
            raw = await td.get_symbol_expiry_list(symbol)
            expiries = sorted(e for e in (parse_expiry_any(v) for v in raw) if e)
            today = datetime.now(IST).date()

            # B4 — the sticky-expiry question. If today's expiry vanishes from the
            # list ON expiry day, a naive "nearest future expiry" selector jumps a
            # week mid-session and the live chain silently changes contract.
            includes_today = today in expiries
            is_expiry_day = bool(expiries) and expiries[0] == today
            if is_expiry_day:
                self.record("B4 expiry_today", "PASS" if includes_today else "FAIL",
                            f"expiry day; getSymbolExpiryList {'includes' if includes_today else 'OMITS'} {today}")
            else:
                self.record("B4 expiry_today", "SKIP",
                            f"not an expiry day (next = {expiries[0] if expiries else 'none'}); re-run on one")
            self.save("B4_expiry_list", {"raw": raw, "parsed": [str(e) for e in expiries]})

            near = next((e for e in expiries if e >= today), None)
            if near is None:
                self.record("discovery", "FAIL", "no future expiry from getSymbolExpiryList")
                return {}

            rows = await td.get_symbol_option_chain(symbol, near.strftime("%y%m%d"))
            self.save("B_chain_raw", rows[:20])

            contracts: list[tuple[str, int, str]] = []
            seen: set[str] = set()
            for r in rows:
                for v in r.values():
                    v = (v or "").strip()
                    if not v or v in seen:
                        continue
                    tail = parse_option_tail(v)
                    if tail and v.upper().startswith(symbol[:4].upper()):
                        seen.add(v)
                        contracts.append((v, int(round(tail[0])), tail[1]))
            if not contracts:
                self.record("discovery", "FAIL", "no parseable contracts in the chain response")
                return {}

            strikes = sorted({c[1] for c in contracts})
            mid = strikes[len(strikes) // 2]
            # Liquid ~= nearest the middle of the listed range; illiquid ~= the
            # far wing, which is exactly the contract that will NOT trade and so
            # demonstrates the no-periodic-push behaviour.
            liquid = min(contracts, key=lambda c: (abs(c[1] - mid), c[2] != "CE"))
            illiquid = max(contracts, key=lambda c: abs(c[1] - mid))
            self.note(f"      chain: {len(contracts)} contracts, expiry {near}, "
                      f"strikes {strikes[0]}..{strikes[-1]}")
            return {
                "expiry": near,
                "liquid": liquid[0],
                "illiquid": illiquid[0],
                "count": len(contracts),
            }
        finally:
            await td.aclose()

    # ---------------- the socket session ----------------
    async def run_socket(self, disc: dict) -> None:
        user = quote(settings.truedata_user, safe="")
        pwd = quote(settings.truedata_password, safe="")
        uri = f"wss://{self.args.host}:{self.args.port}?user={user}&password={pwd}"

        subs: list[str] = []
        index_names = [INDEX_WS_NAMES[s] for s in ("NIFTY", "BANKNIFTY", "SENSEX")
                       if s in INDEX_WS_NAMES]
        subs.extend(index_names)
        subs.append(continuous_future_name(self.args.symbol))
        if disc.get("liquid"):
            subs.append(disc["liquid"])
        if disc.get("illiquid"):
            subs.append(disc["illiquid"])

        async with await self._open(uri) as ws:
            # ---- B6: entitlements ----
            login_raw = await asyncio.wait_for(ws.recv(), timeout=20)
            login = json.loads(login_raw)
            self.save("B6_login_response", login)
            body = login.get("success", login)
            maxsym = body.get("maxsymbols")
            subscription = body.get("subscription")
            segments = body.get("segments")
            validity = body.get("validity")
            ok = bool(subscription and "tick" in str(subscription).lower())
            self.record("B6 entitlements", "PASS" if ok else "FAIL",
                        f"maxsymbols={maxsym} subscription={subscription!r} "
                        f"validity={validity} segments={segments}")
            if not ok:
                self.note("      -> the plan must include TICK-level subscription. A "
                          "1-min-bar-only plan cannot feed a 1-second product.")

            # ---- N1: does the server answer WS-level pings? ----
            try:
                await asyncio.wait_for(await ws.ping(), timeout=8)
                self.record("N1 ws_ping", "PASS", "server answered a protocol ping")
            except Exception as e:
                self.record("N1 ws_ping", "INFO",
                            f"no pong ({type(e).__name__}) — key liveness on the vendor heartbeat only")

            # ---- subscribe ----
            await ws.send(json.dumps({"method": "addsymbol", "symbols": subs}))
            self.note(f"      subscribed: {subs}")

            await self._listen(ws, subs)

            # ---- N5: does the on-socket logout clear the session? ----
            if not self.args.skip_logout:
                await ws.send(json.dumps({"method": "logout"}))
                await asyncio.sleep(1.0)

        if not self.args.skip_logout:
            await self._probe_relogin()

    async def _listen(self, ws, subs: list[str]) -> None:
        deadline = asyncio.get_running_loop().time() + self.args.seconds
        touchline_shapes: dict[int, list] = {}
        trade_shapes: dict[int, list] = {}
        seq_by_symbol: dict[str, list[int]] = defaultdict(list)
        seq_stream: list[int] = []
        heartbeats: list[str] = []
        trades_by_symbol: dict[str, int] = defaultdict(int)
        first_trade: dict = {}

        while asyncio.get_running_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=15)
            except asyncio.TimeoutError:
                self.note("      (15s silence — no heartbeat either; that is itself a finding)")
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue

            if "symbollist" in msg:
                for row in msg["symbollist"]:
                    touchline_shapes.setdefault(len(row), row)
                continue
            if "trade" in msg:
                arr = msg["trade"]
                trade_shapes.setdefault(len(arr), arr)
                if not first_trade:
                    first_trade = {"raw": arr, "len": len(arr)}
                sid = str(arr[0])
                trades_by_symbol[sid] += 1
                # TickSeqNo is documented at index 14 in the 15/19-field layouts.
                if len(arr) > 14:
                    try:
                        seq = int(arr[14])
                        seq_by_symbol[sid].append(seq)
                        seq_stream.append(seq)
                    except (TypeError, ValueError):
                        pass
                continue
            if "heartbeat" in msg or "Heartbeat" in msg:
                heartbeats.append(str(msg))
                continue

        # ---- B1: touchline shape ----
        if touchline_shapes:
            self.save("B1_touchline_rows", touchline_shapes)
            lens = sorted(touchline_shapes)
            self.record("B1 touchline_shape",
                        "PASS" if len(lens) == 1 else "FAIL",
                        f"field counts observed: {lens}")
            if len(lens) > 1:
                self.note("      -> rows are NOT a fixed width. Index >= 11 must be "
                          "resolved by width, or OI and Turnover WILL be swapped.")
            sample = touchline_shapes[lens[0]]
            self.note(f"      sample row: {sample}")
        else:
            self.record("B1 touchline_shape", "FAIL", "no symbollist ack received")

        # ---- B2: trade shape ----
        if trade_shapes:
            self.save("B2_trade_frames", trade_shapes)
            lens = sorted(trade_shapes)
            self.record("B2 trade_shape", "PASS",
                        f"field counts observed: {lens} "
                        f"({'bid/ask ON' if 19 in lens else 'bid/ask OFF' if 15 in lens else 'unexpected'})")
            self.note(f"      sample frame: {first_trade.get('raw')}")
        else:
            self.record("B2 trade_shape", "SKIP",
                        "no trade frames in the window (off-hours, or nothing traded)")

        # ---- B3: sequence-number semantics ----
        if seq_stream:
            per_symbol_gaps = sum(
                sum(1 for a, b in zip(v, v[1:]) if b != a + 1)
                for v in seq_by_symbol.values()
            )
            stream_gaps = sum(1 for a, b in zip(seq_stream, seq_stream[1:]) if b != a + 1)
            verdict = "per-symbol" if per_symbol_gaps <= stream_gaps else "per-connection"
            self.record("B3 seq_semantics", "PASS",
                        f"{verdict} (per-symbol anomalies={per_symbol_gaps}, "
                        f"stream anomalies={stream_gaps}, n={len(seq_stream)})")
            self.save("B3_sequences", {k: v[:50] for k, v in seq_by_symbol.items()})
        else:
            self.record("B3 seq_semantics", "SKIP", "no trade frames carrying a sequence number")

        # ---- B5: which subscriptions actually produced data ----
        acked = set()
        for row in touchline_shapes.values():
            if row:
                acked.add(str(row[0]).upper())
            self.save("B5_index_names", {"requested": subs, "acked_first_fields": sorted(acked)})
        missing = [s for s in subs if s.upper() not in acked]
        self.record("B5 index_names", "PASS" if not missing else "FAIL",
                    f"{len(subs) - len(missing)}/{len(subs)} subscriptions acknowledged"
                    + (f"; NO ack for {missing}" if missing else ""))

        # ---- heartbeat cadence ----
        self.record("heartbeat", "PASS" if heartbeats else "FAIL",
                    f"{len(heartbeats)} beats in {self.args.seconds:.0f}s "
                    f"(~{self.args.seconds / len(heartbeats):.1f}s apart)" if heartbeats
                    else "none received — liveness detection would have nothing to key on")

        # ---- A1 evidence: the no-periodic-push behaviour ----
        traded = {k: v for k, v in trades_by_symbol.items() if v}
        self.record("A1 push_model", "INFO",
                    f"{len(traded)}/{len(subs)} subscribed symbols produced ANY frame "
                    f"in {self.args.seconds:.0f}s")
        self.note("      -> TrueData sends a frame only when a TRADE occurs. Any symbol "
                  "above with zero frames would have had ZERO database rows for the "
                  "whole window; XTS pushed OI ~1/min regardless. This is why the "
                  "hold-last refresher is mandatory, not an optimisation.")

    async def _probe_relogin(self) -> None:
        """N5 — reconnect immediately after an on-socket logout.

        If this succeeds, a graceful shutdown costs nothing and the 60s
        logoutRequest wedge is an exception path. If it fails with 'User Already
        Connected', every restart pays the cooldown and the steward's wedge rung
        becomes the hot path.
        """
        user = quote(settings.truedata_user, safe="")
        pwd = quote(settings.truedata_password, safe="")
        uri = f"wss://{self.args.host}:{self.args.port}?user={user}&password={pwd}"
        await asyncio.sleep(2.0)
        try:
            async with await self._open(uri) as ws:
                raw = await asyncio.wait_for(ws.recv(), timeout=15)
                if "Already Connected" in str(raw):
                    self.record("N5 socket_logout", "FAIL",
                                "reconnect rejected — the on-socket logout does NOT clear "
                                "the session; every restart pays the ~60s wedge")
                else:
                    self.record("N5 socket_logout", "PASS",
                                "reconnected immediately — graceful shutdown avoids the wedge")
                await ws.send(json.dumps({"method": "logout"}))
        except Exception as e:
            self.record("N5 socket_logout", "FAIL", f"{type(e).__name__}: {str(e)[:110]}")

    # ---------------- driver ----------------
    async def main(self) -> int:
        self.note(f"== TrueData WS probe  {self.args.host}:{self.args.port}  "
                  f"proxy={settings.truedata_proxy or 'DIRECT'}")
        if not await self.probe_reach():
            self._write_report()
            return 2
        disc = await self.discover()
        try:
            await self.run_socket(disc)
        except Exception as e:
            self.record("socket_session", "FAIL", f"{type(e).__name__}: {str(e)[:160]}")
        self._write_report()
        return 1 if any(v == "FAIL" for v in self.verdicts.values()) else 0

    def _write_report(self) -> None:
        report = {
            "generated_at_ist": datetime.now(IST).isoformat(),
            "host": self.args.host,
            "port": self.args.port,
            "proxy": settings.truedata_proxy or None,
            "verdicts": self.verdicts,
            "samples": self.samples,
            "log": self.notes,
        }
        path = self.out / "report.json"
        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        blocking = [k for k in self.verdicts if k.startswith("B")]
        failed = [k for k in blocking if self.verdicts[k] == "FAIL"]
        print("\n== summary ==")
        for k in sorted(self.verdicts):
            print(f"   {self.verdicts[k]:5s} {k}")
        print(f"\n   blocking probes: {len(blocking) - len(failed)}/{len(blocking)} clear")
        if failed:
            print(f"   DO NOT start the adapter until these are resolved: {failed}")
        print(f"   -> {path}")


def build_args(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default=settings.truedata_ws_host)
    ap.add_argument("--port", type=int, default=settings.truedata_ws_port,
                    help="8086 sandbox/trial, 8084 production")
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--seconds", type=float, default=LISTEN_SECONDS)
    ap.add_argument("--skip-logout", action="store_true",
                    help="leave the session open (skips N5); use if something else needs it")
    return ap.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(Probe(build_args()).main()))
