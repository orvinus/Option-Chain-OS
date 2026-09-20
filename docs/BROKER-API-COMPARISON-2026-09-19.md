# Broker API comparison — documentation vs. real behaviour

**19 September 2026 · research only — no code was changed.**

Covers every broker Ayush sent, plus the two we already know first-hand:
Lakshmishree (live since 15 Sep) and Fyers (compared on 18 Sep).

Three kinds of evidence are kept apart on purpose:

| Marking | Meaning |
|---|---|
| **Measured** | Numbers taken on our own machines today, or during our live burn-in |
| **Documented** | Stated in the broker's own API documentation |
| **Not stated** | The documentation is silent — never guessed here |

---

## 1. What we actually need

The engine is not a high-frequency system. It evaluates **once per minute** and
places at most a few orders a day. So the ranking below weighs **cost, reliable
daily login and honest order feedback** far above raw speed.

| # | Requirement | Why |
|---|---|---|
| 1 | NSE **and** BSE options (NIFTY + SENSEX weeklies) | Thursday trades SENSEX |
| 2 | Market order, carry-forward product (NRML/MARGIN) | overnight carry is on |
| 3 | Order id back immediately + a way to confirm the fill | the engine sizes and books from the fill price |
| 4 | Positions + funds read, for reconcile and sizing | we compare broker vs engine every minute |
| 5 | **Unattended daily login** | nobody is at a keyboard at 09:10 |
| 6 | Low, predictable **brokerage** | we trade small premiums; costs dominate |
| 7 | ~10 API calls/minute is plenty | every broker below clears this easily |
| 8 | Market data **not** required from the broker | we run on TrueData |

---

## 2. Latency — measured today, all brokers, same method

Unauthenticated requests from **our backend container** (the machine that sends the
live orders), 19 Sep 2026 07:19 IST. 8 samples warm (keep-alive, which is what our
adapter uses), 8 cold, 6 TLS handshakes. Nothing was ordered; no credentials used.

| Broker | Warm median | Warm best | Cold median | TLS | Host IP | Note |
|---|---|---|---|---|---|---|
| **Shoonya (Finvasia)** | **23.7 ms** | 15.3 ms | 95.5 ms | 36.3 ms | 13.202.119.185 | AWS Mumbai |
| **Choice FinX** | **24.5 ms** | 18.5 ms | 82.4 ms | 46.0 ms | 13.203.137.96 | AWS Mumbai |
| **Lakshmishree (ours)** | **29.5 ms** | 14.6 ms | 61.9 ms | 29.5 ms | 202.149.222.131 | own server, best cold time |
| Alice Blue | 29.6 ms | 27.8 ms | 143.2 ms | 36.5 ms | 104.18.17.227 | Cloudflare |
| Zerodha Kite | 36.3 ms | 31.0 ms | 88.7 ms | 39.7 ms | 104.16.34.50 | Cloudflare |
| Fyers v3 | 42.3 ms | 40.1 ms | 111.3 ms | 32.8 ms | 104.18.5.135 | Cloudflare |
| Groww | 45.1 ms | 40.9 ms | 180.0 ms | 35.8 ms | 104.18.37.214 | Cloudflare |
| Upstox *(reference)* | 45.2 ms | 36.0 ms | 164.6 ms | 41.6 ms | 172.64.147.179 | Cloudflare |
| Dhan | 52.9 ms | 40.1 ms | 135.7 ms | 92.4 ms | 108.158.46.79 | AWS CloudFront |
| 5paisa | 70.1 ms | 48.2 ms | 155.1 ms | 40.0 ms | 23.55.244.130 | Akamai |
| Angel One SmartAPI | 71.2 ms | 22.9 ms | 93.0 ms | 41.4 ms | 103.82.178.77 | own range, jittery |
| Sharekhan | 101.2 ms | 86.5 ms | 403.1 ms | 170.5 ms | 159.60.135.114 | slowest by far |
| Kotak Neo | — | — | — | — | **does not resolve** | see §4 |

**How to read this.** Anything inside ~20 ms of another is noise — a repeat run put
Lakshmishree at 18.5 ms and Fyers at 56 ms. What is *not* noise: Sharekhan is
3–4× slower than the field on every metric, and Kotak's documented gateway hostname
(`gw-napi.kotaksecurities.com`) **does not exist in DNS** at all.

None of this measures order matching at the exchange — that is the same exchange for
everyone. It measures the pipe our adapter talks through.

---

## 3. Lakshmishree (Symphony XTS) — documentation vs. what we found

This is the section Ayush asked for specifically. Left column = the published XTS
spec. Right column = what our own system did, with the evidence.

| Topic | XTS documentation says | What we measured / hit in production |
|---|---|---|
| **Error signalling** | Errors return HTTP **400 / 404 / 429 / 500** with `{"type":"error",...}` | **Mismatch** — **A dead token returns HTTP 200** with `{"type":"error","description":"Invalid Token"}`. Never 401/403. Anything keying on status code treats a dead session as success. We rewrote `_auth_rejected()` to match on the body; 16/16 regression tests |
| **Session model** | "Only **one session** is allowed per appKey–secretKey" — documented | **Matches** — true, and harsher than it reads: a second login **silently kills** the first. Recovery needed our own re-login-on-body-error path (~230–410 ms) |
| **Token life** | 24 hours | Consistent with what we see; we re-login on rejection rather than on a timer |
| **Login latency** | not stated | **Measured 65–81 ms** |
| **Funds / balance** | `BalanceList[0].limitObject…` | **Matches** — shape correct — but **Adhoc margin fields can be the literal string `"NaN"`**, undocumented. Balance read **~71 ms median** |
| **Order placement** | `POST /interactive/orders`, returns `AppOrderID` synchronously | **Matches** — correct. **Measured place → fill confirmed: 1,908 ms** (BUY) and **315 ms** (SELL) |
| **Fill confirmation** | socket.io pushes `order` / `trade` / `position` events | **Caveat** — available but **we don't use it** — we poll order status 6× at 0.5 s. That polling, not the network, is most of the 1,908 ms |
| **Rate limits** | Orders 10/s shared; balance, profile, order book, trade book **1/s each**; 429 on breach | Never hit — we use ~1 order/minute. The **1/s** limits on reads are tighter than most rivals, and worth knowing before adding dashboards |
| **Positions / reconcile** | `/interactive/portfolio/positions` | **Matches** — works. **Measured 247 ms**; broker held 845 qty = engine's 845 |
| **Broker MTM / margin** | fields exist | **Mismatch** — **MTM and RealizedMTM stayed 0.00**, and **margin utilised lagged minutes** after the fill (showed ₹0 used right after a ₹971.75 buy). We now compute and display our own figures next to theirs |
| **Cold start** | not stated | First account refresh **timed out at 13.7 s** before the session was warm; now three reads run in parallel with one retry — **refresh 499 ms / fetched in 261 ms** |
| **Instrument identity** | `exchangeInstrumentID`, master via `POST /apimarketdata/instruments/master` | **Caveat** — That master needs an authenticated **market-data** session, which we do not have (we run TrueData). We fall back to **Zerodha's public instrument dump**, whose `exchange_token` equals the XTS id — an undocumented dependency we verified contract-by-contract |
| **Market-data product** | separate XTS MD API | **Caveat** — Its ~**50-instrument cap** wedged subscription swaps for us — one reason we moved the feed to TrueData |
| **Brokerage** | **nothing in the API** | **Mismatch** — The worst gap. The API reports no charges at all. The **contract note** showed **₹40 per lot per side**, hidden inside the trade rates (bought 1.7654 vs our fill 1.15). Our cost model assumed ₹17/order → it under-counted a real trade **30×** (₹210 modelled vs **₹1,397.95 actually charged**) |
| **Latency claims** | none published | our own numbers above are the only figures that exist |

**Verdict on Lakshmishree:** the API itself is **fast and correct** — the fastest cold
connection of the whole field, order round trips under 2 s, reconcile that matched to
the share. Its problems are **commercial and observational**, not technical: brokerage
30× what we modelled and invisible to the API, no cost or MTM reporting, and one
undocumented behaviour (HTTP 200 errors) that could have silently disabled live
trading.

---

## 4. The brokers Ayush sent — what the documentation says

Everything in this section is **documented**, not measured, unless marked.

### 4.1 Zerodha Kite Connect 3
* REST `api.kite.trade`, one socket `wss://ws.kite.trade` carrying **both** market data and **order updates**.
* Token dies **06:00 next day**; account must have TOTP 2FA; Zerodha's own position is that the daily login is **manual** — automating it is not permitted. Everything after that login is automatable.
* Orders: `POST /orders/:variety`, order id immediate; "placement does not imply execution".
* Limits: 10 orders/s, **400/min, 5,000/day**, 25 modifications per order; 3 sockets, 3,000 instruments each.
* Instruments: daily gzipped CSV; docs warn **tokens are reused after expiry** — key on `exchange:tradingsymbol`.
* **Cost: ₹500/month** (data included). Brokerage **₹20/order**.
* Static IP: not required.

### 4.2 DhanHQ v2
* REST `api.dhan.co/v2`, **separate order-update socket** `wss://api-order-update.dhan.co`, feed `wss://api-feed.dhan.co`.
* Token **24 h**, and — unusually — a **documented programmatic re-auth** when TOTP is enabled. API key/secret valid 12 months.
* Limits: orders 10/s, 250/min, 1,000/hr, **7,000/day**; feed 5,000 instruments/connection, 5 connections.
* **Static IP whitelisting is mandatory for place/modify/cancel.**
* **Cost: trading APIs free; market data ₹499/month.** Brokerage **₹20/order**.

### 4.3 Angel One SmartAPI
* REST `apiconnect.angelone.in`; feed `wss://smartapisocket.angelone.in`; **order updates** `wss://tns.angelone.in/smart-order-update`.
* Login takes **TOTP as a field** → fully headless daily login. Tokens die at **midnight**.
* Limits: order APIs **9/s cumulative**, 500/min, 1,000/hr; feed **1,000 token-subscriptions** per session (and a token counts once *per mode*); 3 connections.
* **Static IP mandatory at app registration** (max 5, changeable once a week).
* **Cost: API free.** Brokerage **₹20/order**.
* Measured: warm median 71.2 ms but best-case 22.9 ms — the **jitteriest** host in the test.

### 4.4 Shoonya (Finvasia)
* REST `api.shoonya.com`; one socket carries quotes **and** order updates (`t:"om"`, fills pushed with price/qty — **no subscribe step**).
* Session valid **for the trading day only**, **no refresh endpoint**; docs offer a **Selenium headless login** as the unattended path.
* **Static IP whitelisting mandatory** — 1 primary + 1 backup only.
* Same trap as XTS: **HTTP 200 for rejected orders** — you must check `stat == "Ok"`.
* Limits: ~10 orders/s; market data ~1/s per instrument; ceilings "may be tuned without notice".
* **Cost: API free.** Brokerage **₹5 per order** — the cheapest here.
* Measured: **fastest host in the test (23.7 ms)**.
* Doc conflict: its FAQ says `/NorenWClientTP/`, its docs say `/NorenWClientAPI/` — probe before building.

### 4.5 Alice Blue (ANT v2)
* REST `a3.aliceblueonline.com`; feed `wss://ws1.aliceblueonline.com/NorenWS`; **order-notify socket** plus optional **webhook**.
* Login is a **browser redirect** (`authCode` → SHA-256 → session). Token life **not stated**; treat as daily. No documented headless path.
* **Best-published rate limits of the field:** orders 10/s, 300/min, 3,000/hr, **200,000/day**; data 20/s; quotes 5/s.
* Contract master per exchange, refreshed **08:00 daily**; V1 endpoints retire **5 Oct 2026**.
* **Cost: API free for life.** Brokerage **₹20/order or 0.05%, whichever lower**.
* Feed needs a **50-second heartbeat**.

### 4.6 Kotak Neo
* **Caveat** — **Measured: the documented gateway host does not resolve** (`gw-napi.kotaksecurities.com` → NXDOMAIN), and `documentation.kotaksecurities.com` is dead too. The REST base is **assigned at login** (`baseUrl` in the response), so this is survivable — but the published entry points are stale.
* Login: **TOTP + MPIN, fully headless** — the cleanest unattended login of the field. Token life **not stated**; `403 Invalid session` is the expiry signal.
* Orders `POST /quick/order/rule/ms/place` → `nOrdNo`. Order/position socket `wss://<baseUrl>/realtime` with a **raw-string auth frame**.
* **Market orders are prohibited for retail algo trading** (SEBI): they are auto-converted to **limit orders with 0.5–2 % protection**. Our engine sends market orders — this changes behaviour.
* Limits: 10 orders/s; per-endpoint REST quotas **not published**.
* Feed: 3,000 tokens total, 200 per request. The old feed was **deprecated 15 Sep 2026**, the v2 SDK **archived 10 Sep 2026** — anything older than that is stale.
* **Cost: API free; ₹0 brokerage on API orders** (statutory charges still apply) — the cheapest of all.
* Only broker here that publishes latency: **<50 ms** order execution, plus per-endpoint figures (login 367 ms, validate 134 ms, quotes 289 ms).

### 4.7 Jainam
* The link Ayush sent is **not XTS** — it is Jainam's own **ProTrade (Noren-based)** API: REST `protrade.jainam.in`, order feed `wss://protrade.jainam.in/omo/odrest/websocket` (**1-minute heartbeat**), market feed `wss://ws.jainam.in/NorenWSTP/`.
* Login is a **browser SSO redirect**; token life not stated.
* Limits: **order APIs "NOT LIMITED"**; everything else 1,800 requests / 15 minutes.
* Contract master per exchange, refreshed **08:00 daily**.
* **Cost: API free.** Brokerage **₹20/order**.
* Jainam **also** sells Symphony XTS (dealer and retail), login window 08:30–15:28 — i.e. the same API family we already run.

### 4.8 Sharekhan (Mirae Asset Sharekhan)
* Measured: **slowest of every broker tested** — warm 101 ms, cold 403 ms, TLS 170 ms. Their marketing claims "up to 25 ms" per trade; our own network numbers are 4× that before any order is even sent.
* REST `api.sharekhan.com/skapi/services/...`; market feed `wss://stream.sharekhan.com/skstream/...`.
* Login is **browser + 2FA**, Kite-style (`request_token` → AES-decrypt with the secret → access token). **JWT valid 24 h, no refresh, no documented headless path** — so somebody logs in by hand every morning.
* Orders: `POST /skapi/services/orders`; modify and cancel are the **same POST path** with `requestType` NEW/MODIFY/CANCEL. **Whether the order id comes back synchronously is not published** — no sample response exists anywhere.
* Order updates ride the same socket under an `ack` key whose semantics are **undocumented**.
* Limits: "up to **30 orders/second**" (highest claimed here). Their own FAQ page for this is literally unfilled — "you will be able to place **XX** orders per second". Per-minute/day limits not stated.
* Instruments: scrip master is an **endpoint** (`GET /skapi/services/master/{exchange}`), not a file; size and refresh cadence not stated.
* **Cost: API free. Options brokerage ₹39 per lot per side** — nearly as bad as Lakshmishree's ₹40, and per *lot*, so a 13-lot round trip is ~₹1,014. (A ₹20/trade F&O plan is reported to exist; plan-dependent, confirm on the account.)
* Wart worth naming: Sharekhan's **own** Python SDK sets `ssl.CERT_NONE` on the websocket — TLS certificate verification switched off — and has no retry/backoff.
* Documentation is a JavaScript single-page app that returns an empty shell to any fetch; the SDK source on their GitHub org is effectively the real documentation.

### 4.9 Groww
* Measured: warm 45.1 ms, behind Cloudflare. Their blog claims an "average response speed of 65 ms"; no SLA.
* REST `api.groww.in/v1`; instrument CSV at a fixed URL; the live feed is **NATS over WebSocket** whose **host is not published** — it is buried in their SDK.
* Three login paths: a token pasted from the profile page (**expires daily at 06:00**, useless unattended), API key + secret with `SHA256(secret+timestamp)`, and a **TOTP** path the SDK page labels "**No Expiry**". **Caveat** — **Their docs contradict each other** — the curl page says both key flows still need "daily approval on the Groww Cloud API Keys page". Must be probed live before trusting it.
* Orders: `POST /v1/order/create` → **returns `groww_order_id` synchronously**, plus modify/cancel/status/trades. Order and position updates push over the NATS feed.
* Limits (published clearly, unlike most): auth 5/s 30/min · **orders 10/s, 250/min** · live data 10/s 300/min · non-trading 20/s 500/min · token endpoint **150 calls per 24 h**. Feed "up to 1,000 instruments" (their blog says 3,000/user — another internal contradiction); REST quote batching max 50.
* Instruments: 19-column CSV including `exchange_token`, `lot_size`, `expiry_date`, `strike_price`, `freeze_quantity` — the cleanest master of the field for our purposes.
* **Cost: free tier covers all APIs except live data; ₹499 + tax/month if you want their data** (we don't — we run TrueData). Brokerage **₹20 per executed order, flat** — best of these three for multi-lot.
* **Restriction that matters: CASH + F&O only, no MCX.** Not an issue for NIFTY/SENSEX.
* SDK is PyPI-only (`growwapi`); there is **no official public GitHub repo**, and the npm package of that name is third-party.

### 4.10 Choice FinX
* Measured: **second fastest (24.5 ms)**, AWS Mumbai, answered 200 on an unauthenticated probe. Their marketing is the most aggressive of anyone: **7.4 ms round trip, p50 7 ms, p99 9.5 ms, "<10 ms" SLA**, co-located at NSE/BSE/MCX. Unaudited, and our own 24.5 ms is the only figure either of us can verify.
* **Caveat** — **Choice publishes no public developer documentation at all** — `finx.choiceindia.com/api-portal` 404s and `docs.finx.choiceindia.com` does not resolve. Everything below comes from a Choice-issued PDF walkthrough, a published SDK and one third-party integration.
* Hosts `finxomne.choiceindia.com` / `finx.choiceindia.com` — the "omne" name betrays an **Omnesys/NEST lineage**. **It is not XTS**: proprietary `api/OpenAPIV1/...` namespaces, `VendorId` + `Bearer` + `SessionId` auth, and market data delivered as **FIX 3.0 over raw TCP with Zlib**, host and port handed out at login — not socket.io. Treat any "Choice = XTS" claim as wrong.
* **Unattended login: yes, and it is the cleanest of the three** — three POSTs (`LoginTOTP` → `GetClientLoginTOTP` → `ValidateTOTP`), and **Choice returns the OTP through the API itself** rather than pushing it to a phone. API key is a JWT you generate in the dashboard with a **1-day or 1-month validity**; the session is day-scoped.
* **Cost: API free forever, no subscription, no per-call fee.** Brokerage **₹25 per lot** on NSE equity options — per *lot* again, so a 13-lot round trip is ~₹650.
* **Three restrictions that would change our engine:**
  1. **Market orders are not supported** — limit and stop-loss-limit only.
  2. **Prices must be sent in paisa** (₹ × 100).
  3. **F&O quantities are in total shares, not lots.**
* **Static-IP binding is mandatory** — one declared IP per key; VPNs, proxies and serverless egress are rejected, and the key must be rotated when the IP changes.
* Unknowns that block any estimate: the **place-order path itself is not published**, nor the order-websocket URL, nor subscription caps, nor any rate limit (they do send `Retry-After`, so throttling exists). Broker login window reported as **08:30–15:28 IST**.

### 4.11 "Sahi Pro" — not a broker
* The "Documentation APIs" link is **Sahi Pro by Tyto Software**, a **software test-automation tool**. Its "Documentation APIs" are `_startDocumentation()` / `_stopDocumentation()` for generating test reports. Nothing to do with trading.
* The **broker** named *Sahi* (sahi.com) is a different company (₹10/order F&O). Its **API is not public yet**.
* **Conclusion: nothing to integrate here today.**

### 4.12 Fyers v3 *(compared in full on 18 Sep — see that report)*
* REST `api-t1.fyers.in`; order socket `wss://socket.fyers.in/trade/v3`.
* Token expires **daily**; OAuth auth-code flow.
* Limits: 10/s, 200/min, 100,000/day — and **3 per-minute breaches = blocked for the day**.
* **Cost: API free.** Brokerage **₹20/order**.
* Symbol master 84 MB daily; its `exToken` equals the exchange token we already map.

---

## 5. One-line scorecard

| Broker | Measured warm | Brokerage (options) | API fee | Unattended daily login | Order-fill push | Static IP | Fit for us |
|---|---|---|---|---|---|---|---|
| **Lakshmishree (ours)** | 29.5 ms | **₹40/lot** ≈ ₹1,040 per 13-lot round trip | free | token-based, works | socket.io (unused) | no | Working, but **by far the most expensive** |
| **Kotak Neo** | host unresolved | **₹0** | free | **Yes (TOTP+MPIN)** | yes | portal-managed | Cheapest, but **no market orders** for algo |
| **Shoonya** | **23.7 ms** | **₹5/order** | free | Selenium only | yes (auto) | **mandatory, 2 IPs** | Cheapest usable, fastest — IP rule is the catch |
| **Fyers** | 42.3 ms | ₹20/order | free | daily OAuth | yes | no | Good all-rounder |
| **Dhan** | 52.9 ms | ₹20/order | free + **₹499/mo data** | **Yes (TOTP)** | dedicated socket | **mandatory** | Good, if the static IP is fine |
| **Angel One** | 71.2 ms (jittery) | ₹20/order | free | **Yes (TOTP)** | yes | **mandatory** | Fine; feed limits tight |
| **Alice Blue** | 29.6 ms | ₹20/order or 0.05% | free | browser login | yes + webhook | not stated | Fast, best-published limits |
| **Zerodha Kite** | 36.3 ms | ₹20/order | **₹500/mo** | **No — manual daily** | yes (same socket) | no | Ruled out by the manual login |
| **Jainam ProTrade** | not probed | ₹20/order | free | browser SSO | yes | not stated | Order APIs unlimited; SSO is the catch |
| **Groww** | 45.1 ms | **₹20/order flat** | free (₹499/mo only for data) | TOTP — **docs contradict** | yes (NATS) | not stated | Cheapest flat rate; no MCX; verify the login |
| **Choice FinX** | **24.5 ms** | **₹25/lot** ≈ ₹650 per round trip | free | **Yes (API returns its own OTP)** | yes | **mandatory** | Best login story, but **no market orders** |
| **Sharekhan** | **101.2 ms** | **₹39/lot** ≈ ₹1,014 | free | **No — browser + 2FA** | undocumented `ack` | not stated | Slowest, expensive, docs unreadable |
| **Sahi Pro** | — | — | — | — | — | — | **Not a broker** |

**Cost, made concrete.** Same 13-lot NIFTY round trip we actually traded on 15 Sep:

| Broker | Structure | Cost of that round trip |
|---|---|---|
| Kotak Neo | ₹0 on API orders | **₹0** (+ statutory) |
| Shoonya | ₹5/order | **₹10** |
| Groww · Fyers · Kite · Dhan · Angel · Alice Blue · Jainam | ₹20/order | **₹40** |
| Choice FinX | ₹25/lot | **₹650** |
| Sharekhan | ₹39/lot/side | **₹1,014** |
| **Lakshmishree (ours today)** | **₹40/lot/side** | **₹1,040** |

That single column is the whole argument. On the trade we booked, brokerage alone
(₹1,040 of the ₹1,397.95 total charge) turned a small loss into a ₹1,398 loss.

---

## 5b. Documentation quality — because this decides how long an integration takes

Ayush asked about "actual effectiveness" versus the documentation. Ranked by how much
of what we need is actually written down:

| Broker | Docs | What that costs us |
|---|---|---|
| Zerodha Kite · DhanHQ · Angel · Groww · Fyers | Complete, public, readable | estimate is reliable |
| Alice Blue · Shoonya | Good, but Shoonya's own FAQ and docs disagree on the URL path | one probe to settle |
| Symphony XTS (Lakshmishree) | Complete on shape, **silent on error behaviour and on charges** | we discovered HTTP-200 errors in production |
| Jainam ProTrade | Adequate; token lifetime not stated | small unknown |
| Kotak Neo | **Published hosts are dead** (`gw-napi…` NXDOMAIN, docs site gone); SDK archived 10 Sep 2026 | can't even start without support contact |
| Sharekhan | **JavaScript shell — returns nothing to a fetch.** Their SDK source is the real doc. Rate-limit FAQ literally says "XX orders per second" | order-id behaviour is an unknown |
| Choice FinX | **None public at all.** The place-order path is not published anywhere | cannot be estimated without their sales team |

A broker whose documentation we cannot read is not a cheaper broker — it is an
integration priced at "unknown".

## 6. What the documentation never tells you (lessons from our own live test)

1. **Brokerage is invisible to every API.** Only the contract note shows it. We found ours was **₹40/lot**, not the ₹17/order we had modelled — a 30× error that no API call would have revealed.
2. **HTTP 200 can mean failure.** True for Lakshmishree (dead token) and Shoonya (rejected order). Always branch on the body.
3. **Broker-side P&L fields can be dead.** Lakshmishree's MTM stayed 0.00 and margin lagged minutes. Compute your own.
4. **Published hostnames rot.** Kotak's documented gateway does not resolve; its feed and SDK were replaced in September 2026.
5. **The login is the fragile part**, not the order. Daily tokens, TOTP, static IPs and browser redirects decide whether a system can run unattended at 09:15.
6. **Since 1 April 2026 every API order must carry a broker-issued Algo ID** (SEBI). Below 10 orders/second a generic exchange ID applies — we are far below it, but the ID still has to be registered with whichever broker we use.

---

## 7. Recommendation

1. **Fix the cost first, not the technology.** Ask Lakshmishree to move from ₹40/lot to a ₹20-per-order plan. That one conversation is worth ₹1,000 per round trip — more than any integration on this list, and it costs nothing to have.
2. **If they refuse, the shortlist is three, in this order:**
   * **Fyers (₹20/order)** — free API, readable docs, order push socket, no static-IP requirement, and the integration is already scoped at **44–60 hours**. Lowest-risk move.
   * **Shoonya (₹5/order)** — cheapest usable and the fastest host we measured (23.7 ms), *if* we can guarantee a fixed egress IP (they allow exactly 2) and accept a Selenium login.
   * **Groww (₹20/order)** — the best public documentation of the field and a clean instrument master; gated on one question we can settle with a single probe: does the TOTP token really survive without a daily dashboard click?
3. **Talk to, don't build for, Kotak Neo.** ₹0 brokerage is genuinely the best number here, but market orders are prohibited for retail algo accounts (auto-converted to protected limit orders — that changes how our engine fills), and their published gateway host does not exist in DNS. Confirm both before anyone writes code.
4. **Rule out for now:**
   * **Zerodha Kite** — the daily login is manual by policy; we cannot run unattended at 09:15.
   * **Sharekhan** — slowest measured by 3×, ₹39 *per lot*, browser-only login, and documentation we cannot read.
   * **Choice FinX** — fast and free, but **no market orders**, prices in paisa, quantities in shares, mandatory static IP, ₹25/lot, and no published place-order endpoint. Too many engine changes for a worse rate than Fyers.
   * **Sahi Pro** — not a broker.
5. **Whatever we pick, re-measure with a funded account.** Nothing in this report measures order matching at the exchange; only a real burn-in like 15 September shows the truth, and that burn-in is exactly what taught us the ₹40/lot number that no API would have revealed.

---

## 8. Sources

Broker documentation: Kite Connect v3 · DhanHQ v2 · Angel One SmartAPI · Shoonya API
docs · Alice Blue v2 API · Kotak Neo (SDK repo + kotakneo.com) · Jainam ProTrade
apidocs · Symphony XTS v2 Interactive/Market Data · Fyers API v3 · Sharekhan · Groww ·
Choice FinX. Charges pages of each broker.

Our own evidence: `docs/burn-in-2026-09-15/LIVE-BROKER-BURN-IN-2026-09-15.md` ·
contract note 322782 (15 Sep 2026) ·
`docs/BROKER-COMPARISON-FYERS-vs-LAKSHMISHREE-2026-09-18.md` ·
`backend/app/algo/broker/xts_interactive.py` · latency probes run 19 Sep 2026.
