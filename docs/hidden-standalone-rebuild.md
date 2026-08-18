# Rebuilding the "hidden" dashboard as a standalone TrueData application

**Why this document exists.** On 2026-08-18 the `/hidden` dashboard was removed from this
platform (frontend `src/hidden/**`, the `/api/auth/hidden-login` endpoint, and the
`HIDDEN_USER`/`HIDDEN_PASSWORD` env pair). This document is the complete blueprint for
recreating it as a **new standalone application** with **TrueData** as the market-data vendor —
every feature, every formula, every endpoint contract, and the operational lessons this
platform already paid for. The last in-repo copy of the original code is the git commit
immediately before the removal commit (`git log -- frontend/src/hidden` shows it).

---

## 1 · What the hidden dashboard was

A single-page, multi-symbol **option-chain intelligence dashboard**: live OI-change bar
charts, an absolute-OI view, a net-OI strike ladder with bullish/bearish decomposition, a
full greeks chain, and an IV scanner with watchlists — for any of ~230 F&O underlyings
(indices, NSE/BSE stocks, MCX commodities), gated behind one fixed username/password.

Five tabs: **OI Change · OI Absolute · Net OI Calculator · Greeks · IV Scan.**
Global controls: symbol picker (sector-grouped), expiry picker, 10 preset timeframes
(1m/3m/5m/10m/15m/30m/1h/2h/3h/full-day), a custom session time-range slider, and a
"Strikes ATM ±N" window selector (All / ATM-only / ±N).

## 2 · Standalone architecture

```
TrueData realtime WS (push.truedata.in) ──┐
TrueData history REST (backfill)         ──┤
                                           ▼
                       ingest service (Python/FastAPI recommended)
                       • 1-min bucket aggregator per contract
                       • hold-last re-emitter (see §8 — NOT optional)
                       • session-floored baselines for OI change
                                           ▼
                       TimescaleDB  (option_oi_snapshots hypertable)
                                           ▼
                       API service (same process is fine)
                       • REST endpoints (§4)  • WS push /ws/oi-stream (§5)
                       • fixed-credential gate (§7)
                                           ▼
                       React SPA (Vite + Tailwind + echarts-for-react)
                       • the 13 components in §6
```

Minimum tables:
- `option_oi_snapshots(ts, symbol, expiry, strike, option_type, oi, ltp, volume, iv, underlying)` —
  1-minute buckets, Timescale hypertable, compress after 7 days.
- `spot_snapshots(ts, symbol, spot)`.
- `iv_history(date, symbol, expiry_slot, atm_iv)` — feeds IVR/IVP/1-yr IV range (needs ~1 year
  of daily rows before those columns stop showing "warming…").

Frontend deps: `react@18`, `react-dom@18`, `echarts@5`, `echarts-for-react@3`, Tailwind.
Theme tokens: `bg #0b0f17 · panel #111827 · border #1f2937 · ce #ef4444 · pe #22c55e ·
accent #3b82f6 · muted #6b7280`; fonts Inter / JetBrains Mono. Component classes to
re-create: `.panel` (bg-panel/80, border, rounded-2xl, backdrop-blur), `.pill` /
`.pill-active`, `.kpi`, `.range-input` (dual-thumb range slider: two stacked transparent
`<input type=range>`, thumbs 16×16 `#3b82f6`). Also define `foreground` and `surface`
tokens — the original used them without defining them.

## 3 · Frontend shell & state

- One page, no router. `App.tsx` → `Dashboard.tsx`.
- **Login gate first**: until unlocked, render only the login card (§7). The unlock flag is
  in-memory only — refresh re-prompts.
- Poll `/api/health` every **3 s** (drives spot, feed status, IST clock, session-open flag).
- Poll `/api/expiries?symbol=` every **30 s**; keep the selected expiry if still listed, else
  fall back to the first.
- Fetch `/api/symbols` once (retry every 3 s until non-empty).
- Session clock: open 09:15 (minute 555), close 15:40 (minute 940), span 385 min.
  `parseNowIst` reads `health.now_ist` ("YYYY-MM-DDTHH:MM:SS+05:30") → minutes since open.
- Custom range mode: `[fromMin, toMin]` on the slider; right handle at "now" ⇒ open-ended
  live window polled every **3 s**; picking a preset timeframe exits range mode. Default
  lookback when entering range mode: 30 min.
- Data merge rule per tab: prefer the freshest **WS frame**, fall back to the REST
  bootstrap, and accept a frame only if its `timeframe` AND `expiry` match the current
  selection (prevents stale-frame flashes).
- localStorage: one key, `hidden.iv_watchlists.v1` →
  `{ lists: [{id, name, symbols[]}], activeId }`, default list `["NIFTY","BANKNIFTY","FINNIFTY"]`,
  cap **50 symbols** per list.

## 4 · REST API contract (re-implement verbatim)

| Endpoint | Params | Returns |
|---|---|---|
| `GET /api/health` | — | `{status, authenticated, latest_spot, tokens_subscribed, last_flush_at, expiries[], run_mode, now_ist, nse_session_open, feed_connected, active_symbol}` |
| `GET /api/symbols` | — | `{active_symbol, groups: [{sector, symbols: [{symbol, display, kind, sector, fno_eligible, lot_size, strike_step}]}]}` |
| `GET /api/expiries` | `symbol` | `{expiries: [ISO dates]}` |
| `GET /api/spot` | `symbol` | `{symbol, spot, asof}` |
| `GET /api/oi-change` | `timeframe` \| (`from_ts`[, `to_ts`]), `expiry`, `symbol` | `{timeframe\|"range", expiry, spot, asof, computed_at, total_call_oi_change, total_put_oi_change, rows: [{strike, call_oi, put_oi, call_oi_change, put_oi_change, call_ltp, put_ltp, call_ltp_change, put_ltp_change}]}` |
| `GET /api/option-chain-full` | `timeframe`, `expiry`, `symbol` | adds per-row `call_volume, put_volume, call_iv, put_iv, call_trend, put_trend, pcr_oi, pcr_volume, pe_ce_oi, pe_ce_oi_change` + greeks `{call,put}_{delta,gamma,theta,vega}`; top-level `lot_size, synthetic_future, atm_iv, ivp` |
| `GET /api/iv-scanner` | `symbols` (CSV ≤50), `expiry` (near/next/far), `mode` (latest/historical), `timeframe` | `{mode, expiry_slot, expiry, asof, max_symbols, rows: [{symbol, display, price, price_chg, price_chg_pct, total_oi, total_oi_chg, total_oi_chg_pct, pcr, iv, iv_chg_pct, iv_range_1y_low/high, hv_10/20/30, ivr, ivp, iv_hv10/20/30, history_ready}], note}` |
| `POST /api/active-symbol` | body `{symbol}` (45 s client timeout) | `{symbol, display, fno_eligible, spot, expiries[]}` — switches the live subscription set |
| `POST /api/auth/gate-login` | body `{username, password}` (55 s timeout) | `{status, message, authenticated}` — see §7 |

Conventions: `asof` = latest exchange/feed timestamp (may freeze after close);
`computed_at` = server wall-clock when the snapshot was built (use it for "last updated");
`ltp` **NULL means "no quote", never 0**; OI-change semantics in §9.

## 5 · WebSocket push contract

- Path `/ws/oi-stream?timeframe=&expiry=&symbol=`.
- Server → client frames: `{type:"oi_change", data:<OIChangeResponse>}`,
  `{type:"option_chain_full", data:<OptionChainFullResponse>}`, `{type:"ping"}` (~every 15 s),
  `{type:"error", message, detail?}`.
- Client → server: `"pong"` for every ping; control frame `set:tf=5m,exp=2026-08-28,sym=NIFTY`
  to retune without reconnecting.
- Client resilience (copy exactly): idle-close after **55 s** of silence; reconnect with
  exponential backoff `min(30s, 0.5s·2^retry) + jitter(≤250 ms)`; teardown detaches handlers
  before `close()` so intentional closes never trigger reconnect; a queued `set:` payload is
  flushed on open; a **symbol** change clears displayed data (expiry/timeframe changes don't).
- Status states for the header badge: connecting / open / reconnecting / closed / idle, with
  the special override **"NO MARKET DATA"** when the socket is open but the vendor feed is
  disconnected (`health.feed_connected === false`).

## 6 · Feature spec — the 13 components

1. **HiddenLogin** — centered card, username + password, posts the gate login; errors inline;
   button disabled while pending ("Signing in…").
2. **SymbolSelect** — `<select>` with one `<optgroup>` per sector; non-F&O entries suffixed
   "· spot only"; disabled + spinner while a switch is in flight.
3. **ExpirySelect** — dates formatted `dd Mon yyyy` (en-IN); disabled with "Warming up…" /
   "Error loading expiries" placeholder when empty.
4. **TimeframeBar** — pills: 1/3/5/10/15/30 Min, 1/2/3 Hrs, Full Day; none highlighted while
   a custom range is active.
5. **TimeRangeSlider** — dual-thumb, 1-minute step, min gap 1; header shows
   `h:MM AM/PM → h:MM AM/PM (live)`; Clear button exits range mode.
6. **AtmWindowSelect** — `[−] [input] [+] [All]`; sentinel −1 = All, 0 = ATM only, max 50;
   Enter commits, Escape cancels, arrows step.
7. **KPIBar** (OI Change tab only) — five cards over the windowed rows:
   Call OI Chg (signed compact), Put OI Chg, **Ratio** (`callPutRatio` text + dominant side,
   §9), **PCR** = ΣputOI/ΣcallOI (≥1.2 green "Bullish bias", ≤0.8 red "Bearish", else yellow),
   Spot/ATM (spot + `atmRound`).
8. **SpotHeader** — title "OI Change on {date}", spot (live-health preferred), snapshot
   updated time + "Feed quote time" when they differ, WS status badge, health strip
   (server IST · NSE session · vendor feed · last DB flush · run mode), and (NIFTY only) a
   "Verify vs public NIFTY 50" cross-check button showing Δ vs a public reference within a
   tolerance.
9. **OIChangeChart** — ECharts canvas, 500 px; two bar series **PE first** then CE, raw
   contract values; colors CE `#f75c5c` / PE `#4da6ff`, ATM bars `#ff8c00`/`#00d4b4`,
   negative-change bars muted `#7f3a3a`/`#2a4a7f`; dashed white ATM markLine labelled
   `{SYMBOL} {spot}`; x-labels rotate 45° beyond 30 strikes; tooltip with ATM badge
   (|strike−spot|<30); "all changes zero" state dims the chart to 0.3 and overlays an
   explainer card; empty-DB state shows an ingestion checklist.
10. **NetOICalculator** — strike ladder (calls block · strike · PE−CE/PCR block · puts
    block), ATM row highlighted yellow with ring; four summary panels: **Totals**
    (OI/OI-chg/volume, Net = puts − calls), **Totals-Stats** (12 rows: bullish/bearish OI and
    chg via trend classes §9, premium sums, PE−CE OI chg, PCR OI/chg/vol), **OTM** and
    **ITM** partitions (strict inequalities, ATM strike in neither) each with their PCR
    sub-table.
11. **GreeksChainTable** — 15 columns mirrored around the strike (gamma 4dp, vega 2dp,
    theta 2dp, delta 3dp, OI-lakh, OI bar, LTP), per-lot toggle multiplies LTPs by
    `lot_size`; ATM row yellow; ITM cells amber-tinted; header shows Synth Fut, IVP, ATM IV.
12. **IvScanner** — watchlist-driven table with 19 columns (price/OI/PCR/IV/HV10-20-30/
    IVR/IVP/IV-HV ratios), per-column text filters, click-to-sort, "Advanced" column
    toggle, 45 s auto-refresh while the tab is open; IVR/IVP ≥80 red, ≤20 green;
    "warming…" until 1-yr history exists.
13. **WatchlistManager** — named lists in localStorage (§3), pool = all F&O-eligible
    symbols, search-add chips (first 80 matches), remove chips, create/delete lists,
    50-symbol cap.

## 7 · Auth — the fixed-credential gate

- Env pair (e.g. `GATE_USER` / `GATE_PASSWORD`), verified **server-side only**:
  reject with 500 if either is blank (empty-vs-empty would compare equal!), then
  constant-time compare both fields with `secrets.compare_digest` on **bytes**, combined
  with `&` (not `and`) so both comparisons always run. 401 on mismatch.
- The gate is display-only: it unlocks the UI, it does not protect `/api/*`. If the
  standalone app is internet-facing, add real session auth (the PBKDF2 + signed-cookie
  pattern from this repo's `backend/app/algo/auth.py` is a ready template).

## 8 · The TrueData layer (what replaces XTS — hard-won lessons)

**Realtime WS** (`push.truedata.in`): port **8084 production, 8086 sandbox/trial** — the
trial answers ONLY on 8086 (8082/8084 return "User Subscription Expired"). Login response
declares `maxsymbols`; treat it as the subscription budget and keep a **safety margin of ~2
slots** so an ATM re-centre (old + new edge strikes briefly coexist) never trips "symbol
limit reached". Subscribe in batches (~100). Re-confirm every sandbox measurement on the
production port after upgrading.

**Frames only on trades.** TrueData pushes a frame only when a trade occurs (XTS pushed OI
~1/min regardless). A **hold-last re-emitter** — re-emit each contract's last state once per
persist bucket, gated on the vendor heartbeat and on market hours — is mandatory, or
untraded deep-OTM strikes produce holes that break wing analytics. Never let it run outside
session hours (it will manufacture rows all night).

**Watchdog**: count ANY inbound frame (including heartbeats) as liveness; a
data-frames-only watchdog false-flaps every ~25 s in quiet moments.

**History REST**: `getbars` 1-min bars for backfill; bearer tokens die at a fixed wall
clock (~04:00 IST), not a rolling TTL; pace requests ~4 rps (vendor docs contradict
themselves — measure); the vendor serves only ~6 months back, so the archive DB is the only
long-term copy.

**Egress**: TrueData's edge silently drops connections from hosting-ASN IPs. On a VPS,
route ALL TrueData traffic (REST + WS) through Cloudflare WARP in SOCKS5 proxy mode
(`socks5://<host-resolvable-name>:40000`); from a Docker container `127.0.0.1` is the
container's own loopback, so use an `extra_hosts` alias to the bridge gateway. Direct
connection is fine from a home PC.

**Symbols**: contract identity comes from the TrueData scripmaster (their symbol format,
e.g. `NIFTY2582124500CE`-style names differ per vendor — build a `symbol/expiry/strike/type
→ vendor-symbol` map from the master file, refreshed daily). Lot size and strike step per
underlying also come from the master. **OI units**: verify whether the vendor reports
contracts (units) or lots per exchange — measure once against NSE's public option-chain and
keep the multiplier configurable per exchange (this platform measured NSE/BSE = units).

**Multi-symbol reality**: the WS budget covers roughly one underlying's ATM window
(±11 ⇒ 47 instruments). To serve all ~230 F&O symbols, do what this platform does: the live
WS follows the actively viewed symbol; a background REST snapshotter sweeps every other
symbol's chain on a tiered cadence (indices ~20 s, commodities ~60 s, stocks ~180 s) into
the same table. `POST /api/active-symbol` = re-point the WS subscription set.

**IV & greeks**: compute locally (Black-Scholes on LTP with spot/synthetic future, T from
the real expiry-day 15:30 settlement — a coarser T floor inflates expiry-day IV multiples).
HV10/20/30 from daily closes; IVR/IVP need a stored ~1-yr ATM-IV history (rows flagged
`history_ready=false` render "warming…" until then).

## 9 · The exact math (do not "improve" any of this)

**OI change** = current OI − session baseline, per strike/side, where the baseline is the
**first value seen at-or-after the window start** (per-strike first-seen clamp), and every
query is floored to the session open so yesterday's rows never leak in. Timeframe presets
map to "window = last N minutes"; `full_day` = since 09:15; custom range = `[from_ts, to_ts]`
with open-ended `to_ts` meaning "up to latest".

**Signed ratio** (`callPutRatio(callChange, putChange)`, EPS = 1 contract):
- Dominant side = the side with the **SMALLER signed** OI change (counterintuitive but
  final — smaller Δ = less new writing = weaker resistance): tie band
  `|call−put| < 1` ⇒ NEUTRAL, else `call < put ? CALL : PUT`.
- Display text is magnitude-normalized: `a=|call|, b=|put|`; both <1 ⇒ "1 : 1"; only puts
  moved ⇒ "0 : 1"; only calls ⇒ "1 : 0"; else smaller side pinned to 1, parts formatted to
  ≤2 dp with trailing zeros trimmed.

**Strike window** (`filterOiRowsByAtmWindow(rows, spot, atmWindow)`):
negative window ⇒ all rows; else `w = atmWindow>0 ? atmWindow−1 : 0` (picked N shows
**ATM ± (N−1)**, i.e. 2N−1 strikes; default 5 ⇒ 9 strikes); center =
`atmRound(spot, step) = round(spot/step)·step` with step inferred from the first two rows
(fallback 50); keep `atm−w·step ≤ strike ≤ atm+w·step`. Note three ATM notions coexist:
`atmRound` (window center), nearest **existing** strike (row highlighting), nearest strike
index (chart markLine) — identical on a regular ladder.

**Trend classes** (from OI Δ + price Δ): `LB` long build-up, `SC` short covering,
`SB` short build-up, `LU` long unwinding. Bullish OI = call OI where trend ∈ {LB,SC} + put
OI where trend ∈ {SB,LU}; Bearish = the mirror. OTM: calls strike>spot, puts strike<spot;
ITM: mirror; strike==spot in neither (strict).

**Totals row**: sums of OI/chg/volume per side; premium totals treat NULL LTP as 0.
**PCR (levels)** = Σput_oi/Σcall_oi; **Net** columns are always `put − call`.

**Formatters**: Indian compact — ≥1e7 ⇒ `x.xxCr`, ≥1e5 ⇒ `x.xxL`, ≥1e3 ⇒ `x.xk`;
`signedCompact` prefixes `+`; OI 0 renders "—" ("no market on that side"); IV stored as
decimal, rendered `(iv*100).toFixed(2)%`; OI-lakh = `oi/1e5` at 2 dp; greeks
gamma 4dp / delta 3dp / theta,vega 2dp. Δ-coloring: >0 emerald, <0 red, 0 muted (same rule
for CE and PE columns everywhere except the Net-OI ladder's call OI-chg column, which is
deliberately inverted: rising call OI = red).

## 10 · Bring-up checklist

1. Scaffold: Vite + React + Tailwind (tokens in §2), FastAPI + TimescaleDB via
   docker-compose (`db`, `api`, `nginx` for the SPA + `/api` + `/ws` proxy with `ws: true`).
2. TrueData ingest: scripmaster sync → WS login (budget from `maxsymbols`) → subscribe the
   active symbol's ATM window + spot → 1-min aggregator → hold-last → DB. Backfill with
   `getbars` on first run.
3. API endpoints (§4) + WS hub (§5) + gate login (§7).
4. Frontend shell (§3), then components in this order: SpotHeader + SymbolSelect +
   ExpirySelect + TimeframeBar → OIChangeChart + KPIBar → AtmWindowSelect →
   TimeRangeSlider → NetOICalculator → GreeksChainTable → IvScanner + WatchlistManager.
5. Validate against NSE's public option chain (OI in **lots** there — multiply by lot
   size before comparing; their v3 API needs brotli) before trusting any number.
6. Ops: session-floored queries everywhere, NULL≠0 for LTP, hold-last gated to market
   hours, watchdog counts heartbeats, WS budget margin ≥2. These five invariants are where
   this platform found real bugs — regress none of them.
