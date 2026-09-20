"""NSE UDiFF F&O settlement bhavcopy -> daily bars for contracts that never traded.

Why this exists
---------------
TradingView's D and W series for an option contract carry a bar for EVERY day
the contract was LISTED, not only the days it traded. On an untraded day the
exchange still publishes a settlement price, and TradingView plots it as a
flat bar (O = H = L = C = settlement). Pine's UMP script reads exactly that
series::

    [wH, wL, wC]    = request.security(tickerid, "W", [high[1], low[1], close[1]], lookahead_on)
    [d1H, d1L, d1C] = request.security(tickerid, "D", [high[1], low[1], close[1]], lookahead_on)

so the DISCOVERY (weekly) and BRIDGE (3x daily) rungs are built from those
flat bars whenever a contract is young. Our own feeds are folded from traded
1-minute bars, and the TrueData bhavcopy we already import omits zero-volume
contracts entirely, so a fresh strike has no history at all until its first
trade -- the gate opens days late and every level is built from the wrong
candles (the ``first-week-divergence`` tag in the Pine parity report).

TrueData cannot supply these bars: probed 2026-09-11, ``get_bars(interval=
"EOD")`` for NIFTY26091523450CE returned the same 7 traded days as its 1-min
feed. NSE's own UDiFF file does have them, so we read it directly.

Reconstruction rule (verified 2026-09-11 against the user's TradingView
weekly chart for NIFTY 23450 CE exp 2026-09-15)::

    traded day   (TtlTradgVol > 0):  O/H/L/C = OpnPric/HghPric/LwPric/ClsPric
    untraded day (TtlTradgVol == 0): O = H = L = C = SttlmPric

``SttlmPric`` is the load-bearing field and ``ClsPric`` is a trap: on an
untraded day ClsPric stays FROZEN at the contract's listing price (1271.65 on
every day from 14 Aug to 2 Sep) while SttlmPric is repriced daily
(1166.87 -> 1069.38 -> ... -> 614.34). Folding the reconstructed dailies into
ISO weeks reproduced TradingView's hovered weekly candle to the paisa::

    w/c 2026-08-31   O 768.57  H 768.57  L 556.00  C 566.25   (ours)
    w/c 2026-08-31   O 768.57  H 768.57  L 556.00  C 566.25   (TradingView)

Units: ``TtlTradgVol`` is in CONTRACTS while the TrueData bhavcopy's volume is
in shares, so it is multiplied by the row's own ``NewBrdLotQty`` to keep the
two sources comparable. ``OpnIntrst`` is in contracts in both.

This endpoint is public and unauthenticated -- there are no credentials here
and none may be added.
"""
from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterator, Optional

import httpx

# NSE serves the archive only to what looks like a browser that has already
# been to the site (same pattern as app/services/nse_option_chain.py).
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
HOME_URL = "https://www.nseindia.com/"
REFERER = "https://www.nseindia.com/all-reports-derivatives"
BHAV_URL = (
    "https://nsearchives.nseindia.com/content/fo/"
    "BhavCopy_NSE_FO_0_0_0_{day:%Y%m%d}_F_0000.csv.zip"
)

SOURCE = "nse_settlement"

# Index options / index futures. Stock derivatives (STO/STF) are ignored -- the
# algo trades index options only and the file is 20x larger with them.
_INDEX_TYPES = frozenset({"IDO", "IDF"})


class NseSettlementError(RuntimeError):
    """Fetch or parse failure."""


@dataclass(frozen=True)
class SettlementBar:
    """One contract-day as TradingView's daily bar carries it."""

    trade_date: date
    symbol: str
    expiry: date
    strike: int
    option_type: str          # CE / PE / FUT
    open: float
    high: float
    low: float
    close: float
    volume: int               # shares (contracts x board lot), bhavcopy-comparable
    oi: int                   # contracts
    traded: bool              # False => O=H=L=C=settlement, a listed-but-idle day

    @property
    def token(self) -> str:
        """Stable key, in a namespace distinct from the bhavcopy's ``td:bhav:``
        so both sources coexist on the (trade_date, token) primary key and the
        reader can prefer the exchange-official row."""
        return (
            f"nse:stl:{self.symbol}{self.expiry:%y%m%d}"
            f"{self.strike}{self.option_type}"
        )


def _f(v: Optional[str]) -> float:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return 0.0


def _i(v: Optional[str]) -> int:
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return 0


def _d(v: Optional[str]) -> Optional[date]:
    try:
        return date.fromisoformat(str(v).strip())
    except (TypeError, ValueError):
        return None


def parse_udiff(csv_text: str, symbol: str) -> list[SettlementBar]:
    """Pure transform: one UDiFF CSV -> this underlying's index option/future
    bars. Columns are read BY HEADER NAME only (NSE has reordered this file
    before). Rows with no usable price are dropped rather than stored as zeros.
    """
    out: list[SettlementBar] = []
    want = symbol.upper()
    for rec in csv.DictReader(io.StringIO(csv_text)):
        if (rec.get("TckrSymb") or "").strip().upper() != want:
            continue
        if (rec.get("FinInstrmTp") or "").strip().upper() not in _INDEX_TYPES:
            continue
        trad = _d(rec.get("TradDt"))
        expiry = _d(rec.get("XpryDt"))
        if trad is None or expiry is None:
            continue
        opt = (rec.get("OptnTp") or "").strip().upper() or "FUT"
        if opt not in ("CE", "PE", "FUT"):
            continue
        strike = int(round(_f(rec.get("StrkPric"))))
        vol_contracts = _i(rec.get("TtlTradgVol"))
        lot = _i(rec.get("NewBrdLotQty")) or 1
        settle = _f(rec.get("SttlmPric"))
        o = _f(rec.get("OpnPric"))
        h = _f(rec.get("HghPric"))
        low = _f(rec.get("LwPric"))
        c = _f(rec.get("ClsPric"))
        traded = vol_contracts > 0 and min(o, h, low, c) > 0
        if not traded:
            # Listed but idle: the exchange publishes only a settlement price.
            # ClsPric is stale here (frozen at the listing price) -- never use it.
            if settle <= 0:
                continue
            o = h = low = c = settle
        out.append(
            SettlementBar(
                trade_date=trad,
                symbol=want,
                expiry=expiry,
                strike=strike,
                option_type=opt,
                open=o,
                high=h,
                low=low,
                close=c,
                volume=vol_contracts * lot,
                oi=_i(rec.get("OpnIntrst")),
                traded=traded,
            )
        )
    return out


def new_client(timeout: float = 45.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": UA, "Accept": "*/*", "Referer": REFERER},
    )


async def warm_cookies(client: httpx.AsyncClient) -> None:
    """One homepage hit so the archive host accepts us. Failures are not fatal
    -- the archive sometimes serves a cold client anyway."""
    for url in (HOME_URL, REFERER):
        try:
            await client.get(url, timeout=20.0)
        except httpx.HTTPError:
            pass


async def fetch_settlement_day(
    day: date,
    symbol: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
    timeout: float = 45.0,
) -> Optional[list[SettlementBar]]:
    """One trading day's bars for ``symbol``.

    Returns None when NSE has no file for that date (weekend, holiday, or not
    published yet) -- deliberately distinct from an empty list, which means the
    file existed but held no rows for this underlying.
    """
    owned = client is None
    c = client or new_client(timeout)
    try:
        if owned:
            await warm_cookies(c)
        try:
            r = await c.get(BHAV_URL.format(day=day))
        except httpx.HTTPError as e:
            raise NseSettlementError(f"{day}: {type(e).__name__}") from e
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise NseSettlementError(f"{day}: HTTP {r.status_code}")
        body = r.content
        if len(body) < 2000 or body[:2] != b"PK":
            return None
        try:
            z = zipfile.ZipFile(io.BytesIO(body))
            text = z.read(z.namelist()[0]).decode("utf-8", "replace")
        except (zipfile.BadZipFile, IndexError, KeyError) as e:
            raise NseSettlementError(f"{day}: unreadable archive ({e})") from e
        return parse_udiff(text, symbol)
    finally:
        if owned:
            await c.aclose()


def weekdays(start: date, end: date) -> Iterator[date]:
    """Weekdays in [start, end]; NSE holidays simply have no file (404)."""
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)
