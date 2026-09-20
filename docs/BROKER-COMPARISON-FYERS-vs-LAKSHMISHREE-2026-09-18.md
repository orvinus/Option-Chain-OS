# Fyers API v3 vs Lakshmishree (Symphony XTS) — latency, features, integration cost

**Date:** 18 Sep 2026 · **Author:** platform team · **Status:** research only — **no code was changed**

This compares the broker we already run live (Lakshmishree Broking on the Symphony
XTS Interactive API) with **Fyers API v3** (<https://myapi.fyers.in/docsv3>,
app dashboard <https://fyers.in/web/api-dashboard/user-apps>).

Everything below is either **measured on this machine today**, taken from our own
**live burn-in of 15 Sep 2026**, or quoted from **Fyers' published documentation**.
Nothing is estimated where a measurement was possible, and every estimate is labelled.

---

## 1. Summary

| | Lakshmishree (XTS) — live today | Fyers API v3 — candidate |
|---|---|---|
| Network round trip (warm, median) | **18.5 ms** | **56.0 ms** (~3× slower) |
| Order-endpoint round trip (warm, median) | **50.2 ms** | **53.5 ms** (comparable) |
| Hosting | own server, single India IP | behind **Cloudflare** (shared edge IP) |
| Brokerage on our real trade (13 lots) | **₹1,040** per round trip | **₹40** per round trip |
| Fill confirmation | poll order status (our adapter) | **order WebSocket** push, or poll |
| Access token life | per session, re-login on expiry | **expires daily**, re-login every morning |
| Instrument identity | exchange token (+ Kite dump fallback) | symbol string + `exToken` = same exchange token |
| Documentation / SDK | Symphony XTS spec, no official Python SDK | full public docs + official `fyers-apiv3` SDK |
| Estimated integration effort | — (done) | **44–60 hours** (~2 weeks part-time) |

**The honest headline:** Fyers is **slower on the wire** (Cloudflare adds a hop) but
**dramatically cheaper** (₹20/order flat vs ₹40/lot). On the trade we actually placed
on 15 Sep, Fyers would have cost **₹40 instead of ₹1,040** — a ₹1,000 saving on one
round trip, which dwarfs a 35 ms latency difference for a strategy that evaluates
**once per minute**.

---

## 2. How the latency was measured

* Both brokers probed **from the same two places**, minutes apart, on the same internet
  connection: the **backend container** (where our live orders are actually sent) and the
  Windows host.
* **Unauthenticated** requests only — we have no Fyers account, and nothing could be
  ordered. Each call still travels the full path: DNS → TCP → TLS → their edge → their
  application (both return a proper JSON auth error, not a generic gateway page).
* 12 samples per metric, 150 ms apart. "Cold" = new TCP+TLS per request. "Warm" =
  keep-alive on one client, which is what our adapter does in production.
* Probe scripts were temporary files in the container's `/tmp`; **no repository code was
  touched**.

**What this does and does not prove.** It measures the network and edge, which is the
part an integration cannot change. It does **not** measure Fyers' order-matching time —
that needs a funded account (see §4).

---

## 3. Measured latency

### 3.1 From the backend container (the production path)

| Metric | Lakshmishree XTS | Fyers v3 REST | Winner |
|---|---|---|---|
| DNS (median) | 5.1 ms | 5.8 ms | — |
| TCP connect (median) | 17.9 ms | 20.6 ms | XTS |
| TLS handshake (median) | 23.7 ms | 26.4 ms | XTS |
| **HTTP warm (median)** | **18.5 ms** | **56.0 ms** | **XTS by 37 ms** |
| HTTP warm (min / p90) | 15.9 / 23.3 ms | 49.8 / 60.0 ms | XTS |
| HTTP cold (median) | 67.7 ms | 121.8 ms | XTS |
| Server IP | 202.149.222.131 (own host) | 104.18.5.135 (Cloudflare) | — |

### 3.2 From the Windows host (second opinion, same result)

| Metric | Lakshmishree XTS | Fyers v3 REST |
|---|---|---|
| HTTP warm (median) | 14.8 ms | 62.9 ms |
| HTTP cold (median) | 446.5 ms | 797.0 ms |

### 3.3 The order endpoints themselves

Unauthenticated POSTs to the real order paths (`/interactive/orders` and
`/api/v3/orders/sync`), warm connection, 8 samples:

| | Lakshmishree XTS | Fyers v3 |
|---|---|---|
| Median | **50.2 ms** | **53.5 ms** |
| Min | 19.5 ms | 32.5 ms |
| Max | 86.7 ms | 314.3 ms |

On the **order path the two are effectively equal**. The 37 ms gap in §3.1 appears on
simple GETs; on the POST path Lakshmishree's own auth check costs it the advantage.

### 3.4 WebSocket hosts (connection setup)

| Socket | Host | TCP (median) | TLS (median) |
|---|---|---|---|
| Fyers order updates | socket.fyers.in | 80.9 ms | 98.9 ms |
| Fyers tick-by-tick depth | rtsocket-api.fyers.in | 41.8 ms | 80.8 ms |
| Lakshmishree (REST host) | trades.lakshmishree.com | 14.2 ms | 26.2 ms |

### 3.5 Daily symbol master (Fyers only — an integration must fetch this every morning)

| File | Size | Instruments | Download |
|---|---|---|---|
| `NSE_FO_sym_master.json` | **83.9 MB** | 80,787 | **5.5 s** |
| `BSE_FO_sym_master.json` | 7.8 MB | 7,339 | 0.4 s |

A real record, confirming the identity mapping we already rely on:

```json
"NSE:NIFTY2692229450CE": { "fyToken": "101126092236078", "exToken": 36078,
  "exSymbol": "NIFTY", "underSym": "NIFTY", "expiryDate": "1790071800" }
```

`exToken` **is the NSE exchange token** — the same number our XTS adapter uses as
`exchangeInstrumentID`. So our existing instrument resolution maps to Fyers with no new
lookup service.

---

## 4. Real order latency — what we actually know

From our **live burn-in on 15 Sep 2026** (Lakshmishree, real money, NIFTY 23600 CE):

| Step | Measured |
|---|---|
| Login | 65–81 ms |
| Balance | ~71 ms median |
| Positions / order book | 55–65 ms |
| **BUY placed → fill confirmed** | **1,908 ms** (click → result 1,944 ms) |
| **SELL placed → fill confirmed** | **315 ms** (click → result 367 ms) |
| Position reconcile | 247 ms |

The 1,908 ms is **not network time** — it is our adapter polling order status until the
exchange reports the fill. Fyers has **no published order-latency figure**, and it cannot
be measured without an account. What can be said from the documentation:

* Fyers' **sync** order endpoint returns the order id immediately in the same response
  (`{"s":"ok","code":1101,"id":"..."}`), like XTS.
* Fyers offers an **order WebSocket** (`wss://socket.fyers.in/trade/v3`) that pushes
  order and trade updates. Using it, fill confirmation arrives **as a push instead of a
  poll**, which would likely make the 1,908 ms step *faster* than today, not slower.
* Expect the fill itself (exchange matching) to be identical — the same exchange matches
  both brokers' orders.

**Conclusion:** the wire is ~35 ms slower; the fill path could be faster. For a system
that decides once per minute, neither changes trading outcomes.

---

## 5. Feature comparison

| Area | Lakshmishree (XTS Interactive) | Fyers API v3 |
|---|---|---|
| Auth model | appKey + secretKey → session token | OAuth: app id + secret → auth code → access token |
| Token life | until invalidated; **one session per appKey** (a second login kicks the first) | **expires end of day**; refresh-token + PIN flow available |
| Dead-token signal | HTTP **200** with `{"type":"error"}` body (our known trap) | HTTP 401 / codes −8, −15, −16, −17 |
| Base host | `trades.lakshmishree.com` | `api-t1.fyers.in` |
| Place order | `POST /interactive/orders` | `POST /api/v3/orders/sync` (also `/async`) |
| Modify / cancel | PUT / DELETE `/interactive/orders` | PATCH / DELETE `/api/v3/orders/sync` |
| Basket orders | not used | `POST /api/v3/multi-order/sync` (≤10) |
| Exit positions | per-order sell | `DELETE /api/v3/positions` (`exit_all`, by id, or by filter) |
| Order types | MARKET/LIMIT/SL | 1 Limit · 2 Market · 3 SL-M · 4 SL-L |
| Product types | NRML / MIS (we use **NRML** for overnight carry) | `MARGIN` (carry-forward) · `INTRADAY` · `CNC` · `CO` · `BO` · `MTF` |
| Instrument identity | exchangeInstrumentID (+ Kite dump fallback) | symbol string, e.g. `NSE:NIFTY2692229450CE`; master carries `exToken` |
| Symbol master | our own scripmaster / Kite dump | `public.fyers.in/sym_details/*.json`, refreshed daily |
| Order updates push | none used (we poll) | **order WebSocket** (orders, trades, positions) |
| Market data | separate XTS MD product (we use TrueData) | data WebSocket, 5,000 symbols/connection |
| Rate limits | not published | **10/sec, 200/min, 100,000/day**; 429 with `Retry-After`; **3 per-minute breaches = blocked for the day** |
| Official Python SDK | none | `fyers-apiv3` (we would still write our own thin adapter) |
| Documentation | Symphony spec PDF | public docs + community + skills repo |
| Sandbox / paper | none | none for orders — test with 1-lot live orders |

---

## 6. Cost comparison (the strongest argument)

Measured from **contract note 322782** (15 Sep 2026) vs Fyers' published rate card:

| On our real trade: NIFTY 23600 CE, 13 lots (845 qty), buy + sell | Lakshmishree | Fyers |
|---|---|---|
| Brokerage | **₹1,040.00** (₹40 per lot per side) | **₹40.00** (₹20 per order × 2) |
| GST 18% on brokerage + txn | ₹187.32 | ₹7.31 |
| STT, exchange, SEBI, stamp | ₹1.63 | ₹1.63 |
| **Total cost** | **₹1,228.95** | **₹48.94** |
| Cost per lot, round trip | ₹94.40 | ₹3.76 |
| Break-even move needed (65-qty lot) | ₹1.45 per unit | ₹0.06 per unit |

Fyers charges **flat ₹20 per executed order (or 0.03%, whichever is lower)** with free
account opening, zero AMC and a **free API**. At our trade sizes this is a **~96%
reduction in trading cost**, and it changes which strategies are viable — cheap options
(₹1–₹20 premium) become tradeable, which they are not today.

> Caveat: Lakshmishree's ₹40/lot is what the contract note shows for account MM31147.
> If they agree to a lower rate card, the gap narrows. Fyers' ₹20/order is public.

---

## 7. What integrating Fyers would involve

### 7.1 What already exists (nothing to rebuild)

Our code already separates "which broker" from "the engine":

* `backend/app/algo/broker/xts_interactive.py` (350 lines) — the Lakshmishree adapter.
* `backend/app/algo/broker/execution.py` (362 lines) — the router: paper simulator vs
  live adapter, instrument resolution, entry/exit, position reconcile.
* The orchestrator calls only `execute_entry` / `execute_exit` / `reconcile_live_position`
  and never knows which broker answered.

So a Fyers integration is **one new adapter plus a switch**, not a rewrite.

### 7.2 Work breakdown

| # | Task | Detail | Hours |
|---|---|---|---|
| 1 | App registration + credentials | create app on the Fyers dashboard, set redirect URI, store `FYERS_APP_ID` / `FYERS_SECRET_ID` / `FYERS_REDIRECT_URI` in `.env` (never in code) | 1 |
| 2 | Daily login flow | auth-code URL → access token (SHA-256 of `app_id:secret`), token store, **daily expiry** handling, refresh-token + PIN path, admin "connect" button in Integrations & Broker | 8 |
| 3 | `fyers_interactive.py` adapter | place/modify/cancel, order status, positions, funds, logout; error-code mapping (−8/−15/−16/−17 → re-login), retry/backoff, 429 honouring `Retry-After` | 10 |
| 4 | Symbol mapping | daily `NSE_FO` / `BSE_FO` master (84 MB) → cache by `exToken`, so an existing contract maps to `NSE:NIFTY2692229450CE`; weekly-vs-monthly code rules (Oct=`O`, Nov=`N`, Dec=`D`) | 6 |
| 5 | Execution router | `broker` setting (`lakshmishree` \| `fyers`), route entry/exit/reconcile, keep the "never silently fall back to paper" rule | 4 |
| 6 | Order WebSocket (fills) | `wss://socket.fyers.in/trade/v3`, ping every 10 s, map order/trade updates to our fill confirmation so exits stop polling | 8 |
| 7 | Config + UI | broker picker, connection status, test-login button, lot-size / product-type mapping (`MARGIN` = our NRML) | 5 |
| 8 | Tests | adapter unit tests with a fake transport (mirroring the 16 XTS tests), router tests, symbol-mapping tests, dead-token recovery | 8 |
| 9 | Burn-in | one 1-lot live round trip with screenshots and latency table, exactly like 15 Sep | 4 |
| 10 | Docs + rollback switch | runbook, revert path to Lakshmishree in one setting | 2 |
| | **Total** | | **56 h** |

**Range: 44–60 hours** (~2 weeks part-time, or 7–8 working days focused). The low end
assumes the Fyers account and API app already exist and the order WebSocket is deferred.

At the rate basis used for the Signal Console quote (30% of a ₹1,20,000 contract for
~281 h), this is roughly **₹7,000–₹7,500** of change-request budget.

### 7.3 Order of work (each step independently testable)

1. Account + app + `.env` → **can we log in at all?**
2. Adapter read-only (profile, funds, positions) → compare latency with the numbers above.
3. Symbol mapping → resolve today's traded contract both ways, assert `exToken` matches.
4. One manual 1-lot BUY/SELL through the existing "manual test entry / square-off" card.
5. Order WebSocket for fills.
6. Switch the router, keep Lakshmishree as fallback.

### 7.4 Risks and gotchas

| Risk | Impact | Handling |
|---|---|---|
| **Token expires daily** | engine can't trade from 09:15 if nobody logged in | scheduled pre-market login + loud alert; refresh-token/PIN flow if permitted |
| **Rate limit 10/s, 200/min; 3 breaches = blocked all day** | a retry storm could lock the account out mid-session | client-side token bucket ~8/s, exponential backoff, honour `Retry-After` |
| Cloudflare in front | occasional edge hiccups (we saw one 429 and a 1.5 s outlier) | timeouts + retry already standard in our adapter |
| Weekly symbol encoding | wrong symbol = error −300, no order | always resolve from the daily master, never construct by hand |
| 84 MB master daily | slow/expensive at boot | fetch once per morning, cache to disk, filter to our underlyings |
| No sandbox | first test is real money | 1-lot burn-in with the ≤₹1,000 cap we already enforce |
| Two brokers live at once | double orders | single `broker` setting; only one adapter may be active |

---

## 8. Recommendation

1. **Cost decides this, not latency.** ₹48.94 vs ₹1,228.95 on an identical trade. The
   35 ms extra network time is irrelevant to a once-per-minute engine.
2. Before building: **ask Lakshmishree to match ₹20/order**. If they do, staying put is
   cheaper than a 56-hour integration.
3. If they don't, integrate Fyers as a **second adapter** and keep Lakshmishree as the
   fallback — the router already supports exactly that shape.
4. Measure again **with a funded account** before switching the live engine: the numbers
   in §3 are network-only.

---

## 9. Open questions for Fyers

1. Is there a published order round-trip (place → exchange ack) figure, or a colocation
   option?
2. Does the order WebSocket deliver the fill before the order book shows it?
3. Any per-account order-rate limit beyond 10/s (some brokers cap orders/second lower)?
4. Is the refresh-token + PIN flow allowed for unattended algo logins on a personal
   account?
5. Confirm ₹20/order applies to **both** legs of an options round trip, with no per-lot
   component at our volumes.

---

## 10. Sources

* Fyers API v3 docs — <https://myapi.fyers.in/docsv3> · app dashboard —
  <https://fyers.in/web/api-dashboard/user-apps>
* Endpoint/auth/order/symbol/websocket references —
  <https://github.com/FyersDev/fyers-skills> (`skills/fyers-trading/references/`)
* Rate limits — <https://fyers.in/community/t/rate-limit-v3/13127>
* Charges — <https://fyers.in/charges-list> ·
  <https://www.chittorgarh.com/brokerage_charges/fyers_securities/32/>
* Lakshmishree costs — contract note **322782**, 15 Sep 2026 (account MM31147)
* Lakshmishree latencies — `docs/burn-in-2026-09-15/LIVE-BROKER-BURN-IN-2026-09-15.md`
* Measurements in §3 — probes run 18 Sep 2026 from the backend container and the Windows
  host; unauthenticated, read-only.
