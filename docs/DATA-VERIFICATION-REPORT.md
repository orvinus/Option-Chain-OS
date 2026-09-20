# Data Verification Report: Shrilakshmi XTS Broker vs NSE India

This document describes every data point received from the **Shrilakshmi Fintech / Symphony XTS** broker feed, what is compared against **NSE India** public data, how each comparison works, what tolerances are applied, and where the code lives.

---

## 1. Data Sources

### 1A. Shrilakshmi XTS Broker (Our Feed)

| Layer | Protocol | What it carries |
|-------|----------|-----------------|
| REST `POST /auth/login` | HTTPS | Auth token (appKey + secretKey) |
| REST `POST /instruments/master` | HTTPS | Full NSEFO instrument dump (pipe-delimited) |
| REST `POST /instruments/quotes` | HTTPS | One-shot LTP poll per instrument |
| Socket.IO event `1501-json-full/partial` | WebSocket | **Touchline** — LTP + volume per instrument |
| Socket.IO event `1510-json-full/partial` | WebSocket | **Open Interest** per instrument |

> XTS splits market data: price arrives on message code **1501**, OI arrives separately on **1510**. Both are merged per instrument in `ws_client.py` before being stored.

**Code:** `backend/app/market_data/xts_client.py`, `backend/app/ingest/ws_client.py`

---

### 1B. NSE India (Reference Source)

| What | URL | Auth required |
|------|-----|---------------|
| Index spot price | `https://www.nseindia.com/api/allIndices` | Cookie (homepage GET first) |
| Index option chain | `https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY` | Cookie |
| Equity option chain | `https://www.nseindia.com/api/option-chain-equities?symbol=SBIN` | Cookie |
| Fallback spot | `https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI` | None |

NSE requires a browser-like session — a GET to `https://www.nseindia.com/` must be made first in the same HTTP client to establish the session cookie. All fetches set `User-Agent` to a Chrome 122 UA string.

**Code:** `backend/app/services/nifty_public_quote.py`, `backend/app/services/nse_option_chain.py`

---

## 2. Comparisons Performed

### CHECK 1 — NIFTY Spot Price

**Endpoint:** `GET /api/verify/nifty-cross-check`
**Code:** `backend/app/api/verify_nifty.py` + `backend/app/services/nifty_public_quote.py`

| Field | XTS Source | NSE Source | Field name in NSE JSON |
|-------|-----------|-----------|------------------------|
| NIFTY 50 index last price | `runtime.latest_spot` (from 1501 touchline of the index token) | `allIndices.data[].last` where `index == "NIFTY 50"` | `last` / `lastPrice` |

**Fallback chain:**
1. NSE `allIndices` API (primary)
2. Yahoo Finance `^NSEI` chart metadata (if NSE is unreachable)

**Tolerance:** ±**30 points**

**Response fields:**

| Field | Description |
|-------|-------------|
| `our_spot` | LTP from XTS Socket.IO stream (rupees) |
| `reference_last` | Price from NSE India or Yahoo |
| `reference_source` | `nseindia_allIndices` \| `yahoo_nsei` \| `none` |
| `diff_points` | `our_spot − reference_last` |
| `aligned` | `true` if `|diff| ≤ 30` |
| `tolerance_points` | 30.0 |
| `fetch_detail` | Error/fallback diagnostics if NSE was skipped |

**Sample curl:**
```bash
curl http://localhost:8000/api/verify/nifty-cross-check
```

**Sample response (aligned):**
```json
{
  "our_spot": 24312.5,
  "reference_last": 24309.8,
  "reference_source": "nseindia_allIndices",
  "diff_points": 2.7,
  "aligned": true,
  "tolerance_points": 30.0,
  "fetch_detail": null
}
```

---

### CHECK 2 — Option Chain LTP + Open Interest (per strike)

**Endpoint:** `GET /api/verify/option-chain?symbol=NIFTY&expiry=YYYY-MM-DD`
**Code:** `backend/app/api/verify_option_chain.py` + `backend/app/services/nse_option_chain.py`

#### 2A. What XTS Sends (per option contract)

| Field | XTS Message Code | XTS JSON key | Stored as |
|-------|-----------------|--------------|-----------|
| Last Traded Price | **1501** (touchline) | `LastTradedPrice` | `option_oi_snapshots.ltp` |
| Total Traded Volume | **1501** (touchline) | `TotalTradedQuantity` | `option_oi_snapshots.volume` |
| Open Interest | **1510** (OI) | `OpenInterest` | `option_oi_snapshots.oi` |

XTS prices are already in **rupees** (not paise — no division needed).

#### 2B. What NSE Publishes (per option contract)

NSE option chain JSON structure (from `records.data[]`):

| NSE JSON key | Type | Mapped to |
|--------------|------|-----------|
| `strikePrice` | number | `strike` |
| `expiryDate` | string `"26-Dec-2024"` | `expiry` (parsed to `date`) |
| `CE.lastPrice` | float | `ce_ltp` |
| `PE.lastPrice` | float | `pe_ltp` |
| `CE.openInterest` | int | `ce_oi` |
| `PE.openInterest` | int | `pe_oi` |

NSE expiry dates arrive as `"26-Dec-2024"` strings — the service parses them with `strptime("%d-%b-%Y")` to produce `datetime.date` objects for join with our DB data.

#### 2C. NSE Endpoint Selection by Symbol

| Our symbol | NSE API endpoint | NSE symbol sent |
|-----------|-----------------|-----------------|
| `NIFTY` | `option-chain-indices` | `NIFTY` |
| `BANKNIFTY` | `option-chain-indices` | `BANKNIFTY` |
| `FINNIFTY` | `option-chain-indices` | `FINNIFTY` |
| `MIDCPNIFTY` | `option-chain-indices` | `NIFTYMIDCPSELECT` |
| `NIFTYNXT50` | `option-chain-indices` | `NIFTYNXT50` |
| Any stock (SBIN, HDFCBANK …) | `option-chain-equities` | symbol unchanged |

#### 2D. Comparison Logic

Both datasets are joined on **(strike, expiry)**. For each matched strike:

**LTP comparison:**
```
diff = our_ltp − nse_ltp
tolerance = max(5.0 pts,  nse_ltp × 10%)
aligned = |diff| ≤ tolerance
```
The tolerance is the *more lenient* of the two — 5 rupees flat (covers low-priced deep OTM options) or 10% of the NSE price (covers high-priced ITM options).

**OI comparison:**
```
oi_diff_pct = |our_oi − nse_oi| / max(nse_oi, 1) × 100
aligned = oi_diff_pct ≤ 10%
```
OI uses percentage-only tolerance because raw OI values vary enormously across strikes (10 contracts to 5 million contracts).

#### 2E. Response Fields

**Per-strike row (`rows[]`):**

| Field | Meaning |
|-------|---------|
| `strike` | Strike price (integer, rupees) |
| `expiry` | Expiry date (ISO format) |
| `ce_ltp_ours` | CE last traded price from XTS stream |
| `ce_ltp_nse` | CE last traded price from NSE |
| `ce_ltp_diff` | `ce_ltp_ours − ce_ltp_nse` |
| `ce_ltp_aligned` | `true` if within tolerance |
| `ce_oi_ours` | CE open interest from XTS stream |
| `ce_oi_nse` | CE open interest from NSE |
| `ce_oi_diff_pct` | Percentage difference in OI |
| `ce_oi_aligned` | `true` if within 10% |
| `pe_ltp_ours` | PE last traded price from XTS |
| `pe_ltp_nse` | PE last traded price from NSE |
| `pe_ltp_diff` | `pe_ltp_ours − pe_ltp_nse` |
| `pe_ltp_aligned` | `true` if within tolerance |
| `pe_oi_ours` | PE open interest from XTS |
| `pe_oi_nse` | PE open interest from NSE |
| `pe_oi_diff_pct` | Percentage difference in OI |
| `pe_oi_aligned` | `true` if within 10% |

**Summary block:**

| Field | Meaning |
|-------|---------|
| `total_strikes` | All strikes in the union of our DB + NSE |
| `ce_ltp_aligned` | Count of CE strikes where LTP is within tolerance |
| `pe_ltp_aligned` | Count of PE strikes where LTP is within tolerance |
| `ce_oi_aligned` | Count of CE strikes where OI is within 10% |
| `pe_oi_aligned` | Count of PE strikes where OI is within 10% |
| `missing_in_ours` | Strikes in NSE but not in our TimescaleDB |
| `missing_in_nse` | Strikes in our DB but not in NSE response |

**Sample curl:**
```bash
curl "http://localhost:8000/api/verify/option-chain?symbol=NIFTY"
curl "http://localhost:8000/api/verify/option-chain?symbol=NIFTY&expiry=2025-05-29"
```

**Sample response (truncated):**
```json
{
  "symbol": "NIFTY",
  "expiry": "2025-05-29",
  "checked_at": "2025-05-29T10:32:01.123456+00:00",
  "nse_source": "option-chain-indices",
  "ltp_tolerance_pts": 5.0,
  "ltp_tolerance_pct": 10.0,
  "oi_tolerance_pct": 10.0,
  "summary": {
    "total_strikes": 82,
    "ce_ltp_aligned": 78,
    "pe_ltp_aligned": 77,
    "ce_oi_aligned": 80,
    "pe_oi_aligned": 79,
    "missing_in_ours": 0,
    "missing_in_nse": 2
  },
  "rows": [
    {
      "strike": 24000,
      "expiry": "2025-05-29",
      "ce_ltp_ours": 312.5,
      "ce_ltp_nse": 311.8,
      "ce_ltp_diff": 0.7,
      "ce_ltp_aligned": true,
      "ce_oi_ours": 1234500,
      "ce_oi_nse": 1230000,
      "ce_oi_diff_pct": 0.37,
      "ce_oi_aligned": true,
      "pe_ltp_ours": 45.2,
      "pe_ltp_nse": 44.9,
      "pe_ltp_diff": 0.3,
      "pe_ltp_aligned": true,
      "pe_oi_ours": 987600,
      "pe_oi_nse": 985000,
      "pe_oi_diff_pct": 0.26,
      "pe_oi_aligned": true
    }
  ],
  "note": "NSE option chain data is published in batches and may lag the live XTS feed by several minutes. Run this check during active market hours (09:15–15:30 IST) for meaningful results."
}
```

---

### CHECK 3 — Standalone CLI Script

For batch/offline verification without a running backend:

```bash
# from the project root
python scripts/verify_option_chain.py --symbol NIFTY
python scripts/verify_option_chain.py --symbol NIFTY --expiry 2025-05-29
python scripts/verify_option_chain.py --symbol BANKNIFTY
```

The script:
1. Reads our data directly from TimescaleDB (uses `DATABASE_URL` from `.env`)
2. Fetches NSE option chain live
3. Prints a tabular diff to stdout with ✓/✗ per strike
4. Prints a summary at the end

**Code:** `scripts/verify_option_chain.py`

---

## 3. Data Flow Diagram

```
Shrilakshmi XTS Broker
  └─ Socket.IO stream
       ├─ 1501 (touchline) ──► LTP, Volume
       └─ 1510 (OI)        ──► Open Interest
            │
            ▼ merged per instrument in ws_client.py
       Tick object (ltp, oi, volume, underlying, ts)
            │
            ▼
       MinuteAggregator (1-min buckets)
            │
            ▼
       TimescaleDB: option_oi_snapshots
            │
            ▼
  GET /api/verify/option-chain ──► compare ──► NSE allIndices / option-chain-*
```

```
XTS REST quote (1501 poll)
  └─ /instruments/quotes ──► index LTP (startup snapshot)
            │
            ▼
       runtime.latest_spot
            │
            ▼
  GET /api/verify/nifty-cross-check ──► compare ──► NSE allIndices / Yahoo ^NSEI
```

---

## 4. What Is NOT Currently Compared

The following XTS data fields are **received but not cross-checked** against NSE:

| Field | XTS source | Reason not verified |
|-------|-----------|---------------------|
| OHLC (open/high/low/close) | 1501 touchline | NSE EOD bhavcopy only; not useful intraday |
| Best bid/ask (market depth) | 1502 market depth (not subscribed) | Not subscribed; XTS level-2 is a paid feature |
| Lot size | NSEFO instrument master | Could be verified vs NSE FO bhavcopy; not implemented |
| Expiry date list | NSEFO instrument master | Visually consistent; automated check not implemented |
| Strike step / price band | NSEFO instrument master | Not compared |

---

## 5. Known Limitations

| Limitation | Detail |
|-----------|--------|
| NSE data lag | NSE option chain API is updated in batches (typically every few minutes). The XTS feed is real-time. LTP and OI diffs will look larger if checked at the exact moment between NSE updates. |
| NSE cookie expiry | NSE's AJAX endpoints require a session cookie that expires. If the homepage GET fails (network issue, NSE downtime), the option chain fetch will fail with HTTP 403. |
| XTS socket drops | XTS drops and reconnects the Socket.IO connection every ~83 seconds (server-side). During the reconnect window (~1–2s), no ticks are received. The OI carry-forward logic prevents false zeros but LTP may momentarily be stale. |
| OI=0 artifact | XTS sends `OI=0` on the first 1510 frame after a reconnect. This is suppressed — the last known good OI is carried forward. This means our OI in the verify report will always reflect the last real value, not a post-reconnect zero. |
| After-hours checks | NSE stops updating its option chain after market close (15:30 IST). Running `verify/option-chain` after hours will compare our stored data against NSE's last EOD snapshot — differences may be larger and are not meaningful. |
| Symbol availability | NSE `option-chain-equities` only returns data for the active F&O stock expiry. If the symbol has no active options (expired or not yet listed), NSE returns an empty `data` array. |

---

## 6. All Verify Endpoints — Quick Reference

| Route | Compares | Tolerance | Code |
|-------|---------|-----------|------|
| `GET /api/verify/ping` | None (health check) | — | `verify_nifty.py` |
| `GET /api/verify/nifty-cross-check` | NIFTY spot: XTS vs NSE/Yahoo | ±30 pts | `verify_nifty.py` |
| `GET /api/verify/option-chain` | CE+PE LTP and OI per strike: XTS DB vs NSE | LTP: ±5pts or ±10%; OI: ±10% | `verify_option_chain.py` |

---

## 7. File Index

| File | Purpose |
|------|---------|
| `backend/app/market_data/xts_client.py` | XTS REST client (login, master, quotes, subscribe) |
| `backend/app/ingest/ws_client.py` | XTS Socket.IO client (1501+1510 merge → Tick) |
| `backend/app/ingest/aggregator.py` | Buckets ticks into 1-min windows → TimescaleDB |
| `backend/app/services/nifty_public_quote.py` | Fetches public NIFTY spot from NSE / Yahoo |
| `backend/app/services/nse_option_chain.py` | Fetches NSE option chain (indices + equities) |
| `backend/app/api/verify_nifty.py` | `/api/verify/nifty-cross-check` endpoint |
| `backend/app/api/verify_option_chain.py` | `/api/verify/option-chain` endpoint |
| `backend/app/api/schemas.py` | Pydantic response schemas for all verify endpoints |
| `scripts/verify_option_chain.py` | Standalone CLI script (no backend required) |
