"""Minimal TrueData REST client — historical backfill ONLY.

Deliberately tiny and WS-free: the live feed remains XTS. This client exists for
``scripts/truedata_backfill.py`` (6-month 1-min bar extraction) and any future
gap-fill jobs. Design rules baked in from the vendor-doc audit
(``TD_API_Documents/TrueData_Historical_Data_Reference.md``):

* **CSV parsed by header name with aliases, never by position** — the vendor's
  own PDFs contradict each other on bars column order (volume↔oi), and header
  names drift between doc tables (``timestamp/time``, ``oi/openinterest``,
  ``volume/tickvol``).
* **Error strings arrive as 200-style body text** (``"No data exists for X"``,
  ``"API calls quota exceeded!…"``) — every response body is pre-screened before
  CSV parsing, else errors are silently ingested as data rows.
* **Bearer tokens die at a fixed wall clock (~04:00 IST)**, not a rolling TTL —
  ``expires_in`` is seconds *remaining*. The manager refreshes proactively near
  the boundary and retries exactly once on auth-denied.
* **Rate governor**: docs give four contradictory limits (1–10/s); all calls go
  through a token-bucket at ``settings.truedata_rate_limit_rps``.
* Credentials never reach logs: the symbol-master host wants them in the query
  string, so URLs are scrubbed before logging.
"""
from __future__ import annotations

import asyncio
import csv
import io
import re
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from urllib.parse import quote

import httpx

from ..core.config import settings
from ..core.logging import get_logger

log = get_logger("truedata_rest")

AUTH_URL = "https://auth.truedata.in/token"
HISTORY_BASE = "https://history.truedata.in"
MASTER_BASE = "https://api.truedata.in"
CORPORATE_BASE = "https://corporate.truedata.in"

DEFAULT_TIMEOUT = 60.0

# Known vendor error strings (verbatim, matched case-insensitively as prefixes
# anywhere in the first 200 chars of a body).
_ERROR_MARKERS = (
    "api calls quota exceeded",
    "segment not subscribed",
    "symbol does not exist",
    "authorization has been denied",
    "invalid_grant",
)
_NO_DATA_MARKER = "no data exists"

# Header aliases → canonical names (lowercased compare).
_HEADER_ALIASES = {
    "timestamp": "timestamp", "time": "timestamp",
    "open": "open", "high": "high", "low": "low", "close": "close",
    "volume": "volume", "tickvol": "volume", "vol": "volume",
    "oi": "oi", "openinterest": "oi", "open_interest": "oi",
    "ltp": "ltp", "bid": "bid", "bidqty": "bidqty", "ask": "ask", "askqty": "askqty",
}


class TrueDataError(RuntimeError):
    """Vendor returned a recognized error string."""


class QuotaExceeded(TrueDataError):
    """Rate-limit error — caller should back off and retry."""


class AuthDenied(TrueDataError):
    """Bearer rejected — token manager refreshes and retries once."""


def _scrub(url: str) -> str:
    """Strip credentials from a URL before it can reach a log line."""
    return re.sub(r"(user|password)=[^&]*", r"\1=***", url)


def _fmt_dt(dt: datetime) -> str:
    """TrueData history range stamp: ``yymmddTHH:MM:SS`` (IST-naive)."""
    return dt.strftime("%y%m%dT%H:%M:%S")


def _q(symbol: str) -> str:
    """URL-encode a symbol (index names carry spaces: ``NIFTY 50``)."""
    return quote(symbol, safe="")


def _screen_body(body: str) -> None:
    """Raise on known vendor error strings riding in a normal-looking body."""
    head = (body or "")[:200].strip().lower()
    if not head:
        return
    if _NO_DATA_MARKER in head:
        raise _NoData()
    if "quota exceeded" in head or _ERROR_MARKERS[0] in head:
        raise QuotaExceeded(head[:120])
    if "authorization has been denied" in head:
        raise AuthDenied(head[:120])
    for marker in _ERROR_MARKERS:
        if head.startswith(marker) or (marker in head and "," not in head.splitlines()[0]):
            raise TrueDataError(head[:120])


class _NoData(Exception):
    """Internal: vendor said 'No data exists' — an empty result, not an error."""


class _RateGovernor:
    """Token-bucket pacing: at most ``rps`` requests/second, single-flight lock."""

    def __init__(self, rps: float) -> None:
        self.min_interval = 1.0 / max(rps, 0.1)
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = _time.monotonic()
            delta = now - self._last
            if delta < self.min_interval:
                await asyncio.sleep(self.min_interval - delta)
            self._last = _time.monotonic()


@dataclass
class _TokenState:
    token: str = ""
    expires_at: float = 0.0  # unix epoch


class TrueDataRest:
    """Async REST client. One instance per process; share it across tasks."""

    def __init__(
        self,
        user: str | None = None,
        password: str | None = None,
        rps: float | None = None,
    ) -> None:
        self.user = (user or settings.truedata_user).strip()
        self.password = (password or settings.truedata_password).strip()
        if not self.user or not self.password:
            raise RuntimeError(
                "Missing TrueData credentials. Set TRUEDATA_USER and "
                "TRUEDATA_PASSWORD in .env (never in code or logs)."
            )
        self.gov = _RateGovernor(rps or settings.truedata_rate_limit_rps)
        self._tok = _TokenState()
        self._tok_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------------- auth ----------------

    async def _bearer(self, force: bool = False) -> str:
        async with self._tok_lock:
            now = _time.time()
            # Refresh 300 s before expiry; expiry is a fixed ~04:00 IST wall clock,
            # so `expires_in` from the response is authoritative per issue.
            if force or not self._tok.token or now >= self._tok.expires_at - 300:
                resp = await self._client.post(
                    AUTH_URL,
                    data={
                        "username": self.user,
                        "password": self.password,
                        "grant_type": "password",
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                if resp.status_code != 200:
                    raise AuthDenied(f"token endpoint HTTP {resp.status_code}: {resp.text[:120]}")
                data = resp.json()
                token = data.get("access_token")
                if not token:
                    raise AuthDenied(f"no access_token in auth response: {str(data)[:120]}")
                expires_in = float(data.get("expires_in") or 3600)
                self._tok = _TokenState(token=token, expires_at=now + expires_in)
                log.info("truedata.auth.ok", expires_in_s=int(expires_in))
            return self._tok.token

    # ---------------- transport ----------------

    async def _get_text(self, url: str, *, bearer: bool) -> str:
        """Rate-governed GET returning body text; auth-retry once; quota backoff
        is the CALLER's job (it owns retry cadence and checkpointing)."""
        await self.gov.wait()
        headers = {}
        if bearer:
            headers["Authorization"] = f"Bearer {await self._bearer()}"
        resp = await self._client.get(url, headers=headers)
        body = resp.text or ""
        try:
            _screen_body(body)
        except AuthDenied:
            # One forced refresh + retry — covers the 04:00 IST boundary mid-run.
            log.warning("truedata.auth.retry", url=_scrub(url))
            await self.gov.wait()
            headers["Authorization"] = f"Bearer {await self._bearer(force=True)}"
            resp = await self._client.get(url, headers=headers)
            body = resp.text or ""
            try:
                _screen_body(body)
            except _NoData:
                return ""
        except _NoData:
            return ""
        if resp.status_code >= 400:
            raise TrueDataError(f"HTTP {resp.status_code} from {_scrub(url)}: {body[:120]}")
        return body

    # ---------------- parsing ----------------

    @staticmethod
    def parse_csv(body: str) -> list[dict[str, str]]:
        """Header-name CSV parse with aliases. Unknown columns are kept verbatim
        (lowercased) so probe output can reveal undocumented fields."""
        if not body.strip():
            return []
        reader = csv.reader(io.StringIO(body.strip()))
        rows = [r for r in reader if r and any(c.strip() for c in r)]
        if not rows:
            return []
        header = [_HEADER_ALIASES.get(h.strip().lower(), h.strip().lower()) for h in rows[0]]
        # A header row must be non-numeric; if the first row parses as data, the
        # vendor omitted the header — refuse rather than guess positions.
        if any(re.fullmatch(r"-?\d+(\.\d+)?", c or "") for c in rows[0]):
            raise TrueDataError(
                "CSV response has no header row — refusing positional parse "
                "(column-order is a known DOC-CONFLICT)."
            )
        return [dict(zip(header, (c.strip() for c in r))) for r in rows[1:]]

    # ---------------- endpoints ----------------

    # Measured 2026-08-10: getbars SILENTLY caps a response at ~3,750-3,800 rows
    # and keeps the TAIL of the range (newest bars). Any request that could
    # exceed the cap must be split, or the head of the range is silently lost —
    # this truncated 358 contracts and every 30-day index chunk in the first
    # pull. Auto-split: if a response lands in the cap zone and the range is
    # still divisible, halve and recurse; merge on timestamp.
    _CAP_ZONE = 3500

    async def get_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        interval: str = "1min",
    ) -> list[dict[str, str]]:
        """1..60-min bars for ``symbol`` over [start, end], IST-naive datetimes.
        Returns [] when the vendor has no data for the range. Transparently
        splits ranges that hit the vendor's silent ~3.8k-row response cap."""
        url = (
            f"{HISTORY_BASE}/getbars?symbol={_q(symbol)}"
            f"&from={_fmt_dt(start)}&to={_fmt_dt(end)}"
            f"&interval={interval}&response=csv"
        )
        rows = self.parse_csv(await self._get_text(url, bearer=True))
        span = end - start
        if len(rows) >= self._CAP_ZONE and span > timedelta(days=2):
            mid = start + span / 2
            left = await self.get_bars(symbol, start, mid, interval)
            right = await self.get_bars(symbol, mid, end, interval)
            merged: dict[str, dict[str, str]] = {}
            for r in left + right + rows:
                ts = r.get("timestamp")
                if ts:
                    merged[ts] = r
            return sorted(merged.values(), key=lambda r: r.get("timestamp") or "")
        return rows

    async def get_symbol_expiry_list(self, symbol: str) -> list[str]:
        url = f"{HISTORY_BASE}/getSymbolExpiryList?symbol={_q(symbol)}&response=csv"
        rows = self.parse_csv(await self._get_text(url, bearer=True))
        out: list[str] = []
        for r in rows:
            for v in r.values():
                if v and re.search(r"\d", v):
                    out.append(v)
                    break
        return out

    async def get_symbol_option_chain(self, symbol: str, expiry_yymmdd: str) -> list[dict[str, str]]:
        """Contract list for one expiry (history host, ``expiry=yymmdd``)."""
        url = (
            f"{HISTORY_BASE}/getSymbolOptionChain?symbol={_q(symbol)}"
            f"&expiry={expiry_yymmdd}&response=csv"
        )
        return self.parse_csv(await self._get_text(url, bearer=True))

    async def get_ticks(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        bidask: bool = True,
    ) -> list[dict[str, str]]:
        """Tick history for [start, end] (IST-naive). Vendor serves ONLY the
        last 5 trading days; bid/ask columns appear only when the account's
        bid/ask-history entitlement is enabled."""
        url = (
            f"{HISTORY_BASE}/getticks?symbol={_q(symbol)}"
            f"&from={_fmt_dt(start)}&to={_fmt_dt(end)}"
            f"&bidask={1 if bidask else 0}&response=csv"
        )
        return self.parse_csv(await self._get_text(url, bearer=True))

    async def get_bhavcopy_status(self, segment: str, day: date) -> str:
        url = (
            f"{HISTORY_BASE}/getbhavcopystatus?segment={segment}"
            f"&date={day:%Y-%m-%d}&response=csv"
        )
        return await self._get_text(url, bearer=True)

    async def get_bhavcopy(self, segment: str, day: date) -> list[dict[str, str]]:
        """Official EOD bhavcopy for a whole segment (columns:
        symbolid,symbol,open,high,low,close,volume,oi)."""
        url = (
            f"{HISTORY_BASE}/getbhavcopy?segment={segment}"
            f"&date={day:%Y-%m-%d}&response=csv"
        )
        return self.parse_csv(await self._get_text(url, bearer=True))

    async def get_fii_dii(self, day: date, segment: str = "fo") -> list[dict[str, str]]:
        """FII/DII flows for one day (corporate host, 1/s limit; date=yymmdd).
        Columns: timestamp,category,Exchange,buy,sell,net."""
        url = (
            f"{CORPORATE_BASE}/getfiidiidata?segment={segment}"
            f"&date={day:%y%m%d}&response=csv"
        )
        return self.parse_csv(await self._get_text(url, bearer=True))

    async def get_news(self, start: datetime, end: datetime, top: int = 500) -> list[dict[str, str]]:
        """News archive (corporate host). from/to use 'YYMMDD HH:MM:SS' with a
        literal space — URL-encoded here. Columns: id,pub_date,title,
        description,img,category,source,source_link."""
        f = start.strftime("%y%m%d %H:%M:%S").replace(" ", "%20")
        t = end.strftime("%y%m%d %H:%M:%S").replace(" ", "%20")
        url = f"{CORPORATE_BASE}/getNewsForDateRange?response=csv&from={f}&to={t}&top={top}"
        return self.parse_csv(await self._get_text(url, bearer=True))

    async def get_all_symbols(
        self, segment: str, search: str | None = None, allexpiry: bool = False
    ) -> str:
        """Raw master dump from api.truedata.in (query-string creds — scrubbed
        from logs). Returned raw: master columns are undocumented; the caller
        introspects."""
        url = (
            f"{MASTER_BASE}/getAllSymbols?segment={segment}"
            f"&user={self.user}&password={self.password}&csv=true&csvHeader=true"
        )
        if search:
            url += f"&search={_q(search)}"
        if allexpiry:
            url += "&allexpiry=true"
        return await self._get_text(url, bearer=False)


# ---------------- shared symbol-string helpers ----------------

# Option strike+type from a contract symbol tail. Naming measured live
# (2026-08-10): ``SYMBOL + yymmdd + STRIKE + CE/PE`` (NIFTY26081121600CE).
# The 6-digit expiry MUST be consumed explicitly — a greedy "trailing digits"
# parse swallows the date into the strike (bug found in the first pull: strike
# 26022423100 instead of 23100; repaired in SQL, fixed here at the source).
_OPT_TAIL_RE = re.compile(r"(\d{6})(\d+(?:\.\d+)?)\s*(CE|PE)\s*$", re.IGNORECASE)


def parse_option_tail(sym: str) -> tuple[float, str] | None:
    """→ (strike, 'CE'|'PE') with the yymmdd expiry prefix stripped and
    validated as a real date."""
    m = _OPT_TAIL_RE.search(sym.strip())
    if not m:
        return None
    try:
        datetime.strptime(m.group(1), "%y%m%d")
    except ValueError:
        return None
    return float(m.group(2)), m.group(3).upper()


_EXPIRY_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%Y%m%d", "%y%m%d", "%d-%b-%Y", "%d%b%Y")


def parse_expiry_any(s: str) -> date | None:
    """Tolerant expiry parser — the vendor uses at least four formats across hosts."""
    v = s.strip().split("T")[0].split(" ")[0]
    for fmt in _EXPIRY_FORMATS:
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def month_chunks(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """[start, end] split into ~30-day chunks (newest LAST; callers reverse for
    newest-first fetch order)."""
    chunks: list[tuple[datetime, datetime]] = []
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=30), end)
        chunks.append((cur, nxt))
        cur = nxt
    return chunks
