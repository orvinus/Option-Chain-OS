"""Rich option-chain snapshot: OI change, volume, IV, trends — mirrors OIChangeEngine SQL."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from cachetools import TTLCache
from sqlalchemy import text

from ..core.config import settings
from ..core.db import AsyncSessionLocal
from ..core.time_utils import IST, market_open_today, parse_timeframe, session_floor_for
from ..market.symbols import get_registry
from .greeks import calc_greeks, synthetic_future
from .iv_calculator import calc_iv

CACHE_TTL_SECONDS = 30


# NSE/BSE F&O contracts settle at 15:30 IST on the expiry date.
EXPIRY_SETTLE_TIME = time(15, 30)
YEAR_SECONDS = 365.25 * 24 * 3600
# Numerical floor (one minute) so the final seconds before settlement cannot blow up
# the Black-Scholes math. Deliberately tiny — it must never distort a real T.
MIN_T_YEARS = 60.0 / YEAR_SECONDS


def _years_to_expiry(expiry: date, ref_utc: datetime) -> float | None:
    """Fraction of a year until the expiry SETTLEMENT INSTANT (15:30 IST).

    Returns ``None`` once the contract has settled, so callers emit a null IV/greek
    rather than a fabricated one.

    Previously this was derived from the calendar DATE alone and floored at ~1 hour,
    so the ENTIRE expiry-day session solved against T ~= 1h: 09:15 and 15:25 got the
    same T. Because an ATM premium ~ 0.4*S*sigma*sqrt(T), a T that is ~6x too small
    forces the solved sigma ~2.5x too high (and overstates theta ~6x). It also masked
    an already-expired expiry as "1 hour to go" instead of reporting no time value.
    """
    settle = IST.localize(datetime.combine(expiry, EXPIRY_SETTLE_TIME))
    secs = (settle - ref_utc.astimezone(IST)).total_seconds()
    if secs <= 0:
        return None
    return max(secs / YEAR_SECONDS, MIN_T_YEARS)


def _classify_trend(oi_chg: int, ltp_chg: float | None) -> str:
    if ltp_chg is None:
        return ""
    if oi_chg > 0 and ltp_chg > 0:
        return "LB"
    if oi_chg < 0 and ltp_chg > 0:
        return "SC"
    if oi_chg > 0 and ltp_chg < 0:
        return "SB"
    if oi_chg < 0 and ltp_chg < 0:
        return "LU"
    return ""


def _safe_ratio(num: float, den: float) -> float | None:
    if den == 0:
        return None
    return num / den


@dataclass
class OptionChainFullRow:
    strike: int
    call_oi: int
    put_oi: int
    call_oi_change: int
    put_oi_change: int
    call_ltp: float | None
    put_ltp: float | None
    call_ltp_change: float | None
    put_ltp_change: float | None
    call_volume: int
    put_volume: int
    call_iv: float | None
    put_iv: float | None
    call_trend: str
    put_trend: str
    pcr_oi: float | None
    pcr_volume: float | None
    pe_ce_oi: int
    pe_ce_oi_change: int
    call_delta: float | None = None
    call_gamma: float | None = None
    call_theta: float | None = None
    call_vega: float | None = None
    put_delta: float | None = None
    put_gamma: float | None = None
    put_theta: float | None = None
    put_vega: float | None = None


@dataclass
class OptionChainFullResponse:
    timeframe: str
    expiry: str
    spot: float | None
    asof: str
    computed_at: str
    lot_size: int
    rows: list[OptionChainFullRow]
    synthetic_future: float | None = None
    atm_iv: float | None = None
    ivp: float | None = None


# Now-side floored at the anchor day's session open — previous-session frozen
# strikes must not surface in the "current" chain (mirrors OIChangeEngine).
_LATEST_SNAPSHOT_SQL = text(
    """
    SELECT DISTINCT ON (strike, option_type)
        strike, option_type, oi, ltp, volume, underlying, ts
    FROM option_oi_snapshots
    WHERE symbol = :symbol AND expiry = :expiry AND ts >= :floor
    ORDER BY strike, option_type, ts DESC
    """
)

_SNAPSHOT_AT_OR_BEFORE_SQL = text(
    """
    SELECT DISTINCT ON (strike, option_type)
        strike, option_type, oi, ltp, volume, underlying, ts
    FROM option_oi_snapshots
    WHERE symbol = :symbol AND expiry = :expiry AND ts <= :cutoff
    ORDER BY strike, option_type, ts DESC
    """
)

# Baseline floored to today's session — prevents a short-timeframe delta from
# borrowing a previous session's OI as its baseline (mirrors OIChangeEngine).
_SNAPSHOT_AT_OR_BEFORE_FLOOR_SQL = text(
    """
    SELECT DISTINCT ON (strike, option_type)
        strike, option_type, oi, ltp, volume, underlying, ts
    FROM option_oi_snapshots
    WHERE symbol = :symbol AND expiry = :expiry AND ts <= :cutoff AND ts >= :floor
    ORDER BY strike, option_type, ts DESC
    """
)

_SNAPSHOT_AT_OR_AFTER_SQL = text(
    """
    SELECT DISTINCT ON (strike, option_type)
        strike, option_type, oi, ltp, volume, underlying, ts
    FROM option_oi_snapshots
    WHERE symbol = :symbol AND expiry = :expiry AND ts >= :cutoff
    ORDER BY strike, option_type, ts ASC
    """
)

_MAX_TS_SQL = text(
    """
    SELECT MAX(ts) AS max_ts
    FROM option_oi_snapshots
    WHERE symbol = :symbol AND expiry = :expiry
    """
)

_MIN_TS_SQL = text(
    """
    SELECT MIN(ts) AS min_ts
    FROM option_oi_snapshots
    WHERE symbol = :symbol AND expiry = :expiry
    """
)


class OptionChainFullEngine:
    def __init__(self) -> None:
        self._cache: TTLCache = TTLCache(maxsize=512, ttl=CACHE_TTL_SECONDS)
        self._lock = asyncio.Lock()

    async def _fetch_anchor_ts(self, symbol: str, expiry: date) -> datetime | None:
        async with AsyncSessionLocal() as s:
            row = (
                await s.execute(_MAX_TS_SQL, {"symbol": symbol, "expiry": expiry})
            ).mappings().first()
        if not row or row["max_ts"] is None:
            return None
        ts = row["max_ts"]
        if getattr(ts, "tzinfo", None) is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)

    async def get(
        self,
        timeframe: str,
        expiry: date,
        symbol: str | None = None,
        live_spot: float | None = None,
    ) -> OptionChainFullResponse:
        from ..runtime import get_runtime
        symbol = (symbol or get_runtime().active_symbol).upper()
        async with self._lock:
            anchor = await self._fetch_anchor_ts(symbol, expiry)
            cache_key = (timeframe, expiry.isoformat(), symbol, anchor)
            cached = self._cache.get(cache_key)
            if cached is not None:
                # Re-use cached data but inject the fresh live_spot override
                if live_spot is not None and live_spot != cached.spot:
                    return self._override_spot(cached, live_spot)
                return cached
            result = await self._compute(timeframe, expiry, symbol, anchor, live_spot)
            self._cache[cache_key] = result
            return result

    def _override_spot(
        self, cached: "OptionChainFullResponse", live_spot: float
    ) -> "OptionChainFullResponse":
        """Return a shallow copy of cached response with spot replaced by live value.

        IV values are NOT recomputed here — the next full cache refresh picks them
        up. This just ensures the spot reported to the frontend is current so ATM
        highlighting uses the correct strike.
        """
        from dataclasses import replace as dc_replace
        return dc_replace(cached, spot=live_spot)

    async def _compute(
        self,
        timeframe: str,
        expiry: date,
        symbol: str,
        anchor_from_db: datetime | None,
        live_spot: float | None = None,
    ) -> OptionChainFullResponse:
        delta_or_marker = parse_timeframe(timeframe)
        now_utc = datetime.now(timezone.utc)
        anchor = anchor_from_db or now_utc
        session_floor = session_floor_for(anchor).astimezone(timezone.utc)
        params = {"symbol": symbol, "expiry": expiry}
        if delta_or_marker == "full_day":
            then_sql = _SNAPSHOT_AT_OR_AFTER_SQL
            then_params = {**params, "cutoff": session_floor}
            floored_then = False
        else:
            assert isinstance(delta_or_marker, timedelta)
            cutoff = anchor - delta_or_marker
            # FLOOR the baseline to today's session (mirrors OIChangeEngine): a
            # strike lacking a today row at/before ``cutoff`` (re-added by ATM
            # drift, or first row after a reconnect gap) must not borrow a
            # previous session's OI as baseline — that made short-timeframe
            # ``now - then`` a huge, wrong-sign negative.
            then_sql = _SNAPSHOT_AT_OR_BEFORE_FLOOR_SQL
            then_params = {**params, "cutoff": cutoff, "floor": session_floor}
            floored_then = True

        async with AsyncSessionLocal() as s:
            now_rows = (
                await s.execute(_LATEST_SNAPSHOT_SQL, {**params, "floor": session_floor})
            ).mappings().all()
            then_rows = list((await s.execute(then_sql, then_params)).mappings().all())
            # Per-strike baseline clamp: a now-strike missing from the floored
            # ``then`` map (entered mid-session) is backfilled with its earliest
            # today snapshot → change reads "since first row today", not ``now-0``.
            if floored_then:
                then_keys = {(r["strike"], r["option_type"]) for r in then_rows}
                missing = {
                    (r["strike"], r["option_type"]) for r in now_rows
                } - then_keys
                if missing:
                    earliest_rows = (
                        await s.execute(
                            _SNAPSHOT_AT_OR_AFTER_SQL, {**params, "cutoff": session_floor}
                        )
                    ).mappings().all()
                    then_rows += [
                        r for r in earliest_rows
                        if (r["strike"], r["option_type"]) in missing
                    ]

        now_map: dict[tuple[int, str], dict] = {(r["strike"], r["option_type"]): dict(r) for r in now_rows}
        then_map: dict[tuple[int, str], dict] = {(r["strike"], r["option_type"]): dict(r) for r in then_rows}

        strikes = sorted({k[0] for k in now_map.keys()})
        rows: list[OptionChainFullRow] = []
        max_ts: Optional[datetime] = None
        # Spot must come from the FRESHEST row, not the first one in strike order.
        # now_rows is ordered by strike, so the old "first non-null underlying" picked
        # the LOWEST strike — typically a quiet deep-ITM contract whose locf-carried
        # underlying can be minutes stale, and that stale S then priced the whole chain.
        spot: Optional[float] = None
        _spot_row = max(
            (r for r in now_rows if r.get("underlying") is not None),
            key=lambda r: r["ts"],
            default=None,
        )
        if _spot_row is not None:
            spot = float(_spot_row["underlying"])

        # None once the contract has settled -> IV/greeks are emitted as null.
        T_year = _years_to_expiry(expiry, now_utc)
        # Commodity options are options-on-FUTURES: approximate Black-76 by using a
        # zero cost-of-carry (r=0) on the forward, since iv_spot is the near-future
        # price. Equity/index options keep the spot risk-free rate. This removes the
        # systematic carry bias a spot-BS r-drift would add to commodity IVs.
        _iv_reg = get_registry().get(symbol)
        r_rate = 0.0 if (_iv_reg is not None and _iv_reg.kind == "commodity") else 0.065
        # Prefer the live WebSocket spot for IV accuracy; fall back to tick underlying
        iv_spot_global: Optional[float] = live_spot or spot

        for strike in strikes:
            ce_now = now_map.get((strike, "CE"))
            pe_now = now_map.get((strike, "PE"))
            ce_then = then_map.get((strike, "CE"))
            pe_then = then_map.get((strike, "PE"))

            ce_oi_now = int(ce_now["oi"]) if ce_now else 0
            pe_oi_now = int(pe_now["oi"]) if pe_now else 0
            ce_oi_then = int(ce_then["oi"]) if ce_then else 0
            pe_oi_then = int(pe_then["oi"]) if pe_then else 0

            ce_vol_now = int(ce_now.get("volume") or 0) if ce_now else 0
            pe_vol_now = int(pe_now.get("volume") or 0) if pe_now else 0

            ce_ltp_now = float(ce_now["ltp"]) if ce_now and ce_now.get("ltp") is not None else None
            pe_ltp_now = float(pe_now["ltp"]) if pe_now and pe_now.get("ltp") is not None else None
            ce_ltp_then = float(ce_then["ltp"]) if ce_then and ce_then.get("ltp") is not None else None
            pe_ltp_then = float(pe_then["ltp"]) if pe_then and pe_then.get("ltp") is not None else None

            ce_oi_chg = ce_oi_now - ce_oi_then
            pe_oi_chg = pe_oi_now - pe_oi_then
            ce_ltp_chg = (ce_ltp_now - ce_ltp_then) if ce_ltp_now is not None and ce_ltp_then is not None else None
            pe_ltp_chg = (pe_ltp_now - pe_ltp_then) if pe_ltp_now is not None and pe_ltp_then is not None else None

            # Use live spot if available; fall back to per-tick underlying value
            iv_spot: Optional[float] = iv_spot_global
            if iv_spot is None and ce_now and ce_now.get("underlying") is not None:
                iv_spot = float(ce_now["underlying"])
            if iv_spot is None and pe_now and pe_now.get("underlying") is not None:
                iv_spot = float(pe_now["underlying"])

            call_iv = calc_iv("CE", ce_ltp_now, iv_spot, float(strike), T_year, r_rate)
            put_iv = calc_iv("PE", pe_ltp_now, iv_spot, float(strike), T_year, r_rate)

            call_g = calc_greeks("CE", iv_spot, float(strike), T_year, r_rate, call_iv)
            put_g = calc_greeks("PE", iv_spot, float(strike), T_year, r_rate, put_iv)

            ce_tr = _classify_trend(ce_oi_chg, ce_ltp_chg)
            pe_tr = _classify_trend(pe_oi_chg, pe_ltp_chg)

            pcr_oi = _safe_ratio(float(pe_oi_now), float(ce_oi_now))
            pcr_vol = _safe_ratio(float(pe_vol_now), float(ce_vol_now))
            pe_ce = pe_oi_now - ce_oi_now
            pe_ce_chg = pe_oi_chg - ce_oi_chg

            # Filter out strikes with no meaningful data.
            # Keep a strike if it had ANY intraday activity (volume or OI change).
            # Also keep near-ATM strikes (within 20 strikes of ATM) even if quiet,
            # so ATM context is always visible.
            has_oi_change = ce_oi_chg != 0 or pe_oi_chg != 0
            has_volume = ce_vol_now > 0 or pe_vol_now > 0
            has_activity = has_oi_change or has_volume

            if not has_activity:
                # Allow quiet strikes only within ATM ±20 strikes with two-sided OI
                # Lot size and strike step come from the per-symbol registry entry.
                reg_entry = get_registry().get(symbol)
                min_lot_oi = (reg_entry.lot_size if reg_entry else 0) or settings.nifty_lot_size
                step = (reg_entry.strike_step if reg_entry else 0) or settings.strike_step
                has_two_sided_oi = ce_oi_now >= min_lot_oi and pe_oi_now >= min_lot_oi
                if spot is not None and step > 0:
                    atm_approx = round(float(spot) / step) * step
                    distance_strikes = abs(strike - atm_approx) / step
                    near_atm = distance_strikes <= 20
                else:
                    near_atm = True  # can't determine ATM, keep all
                if not (has_two_sided_oi and near_atm):
                    continue

            rows.append(
                OptionChainFullRow(
                    strike=strike,
                    call_oi=ce_oi_now,
                    put_oi=pe_oi_now,
                    call_oi_change=ce_oi_chg,
                    put_oi_change=pe_oi_chg,
                    call_ltp=ce_ltp_now,
                    put_ltp=pe_ltp_now,
                    call_ltp_change=ce_ltp_chg,
                    put_ltp_change=pe_ltp_chg,
                    call_volume=ce_vol_now,
                    put_volume=pe_vol_now,
                    call_iv=call_iv,
                    put_iv=put_iv,
                    call_trend=ce_tr,
                    put_trend=pe_tr,
                    pcr_oi=pcr_oi,
                    pcr_volume=pcr_vol,
                    pe_ce_oi=pe_ce,
                    pe_ce_oi_change=pe_ce_chg,
                    call_delta=call_g.delta if call_g else None,
                    call_gamma=call_g.gamma if call_g else None,
                    call_theta=call_g.theta if call_g else None,
                    call_vega=call_g.vega if call_g else None,
                    put_delta=put_g.delta if put_g else None,
                    put_gamma=put_g.gamma if put_g else None,
                    put_theta=put_g.theta if put_g else None,
                    put_vega=put_g.vega if put_g else None,
                )
            )

            for r in (ce_now, pe_now):
                if not r:
                    continue
                if max_ts is None or r["ts"] > max_ts:
                    max_ts = r["ts"]
                if spot is None and r.get("underlying") is not None:
                    spot = float(r["underlying"])

        asof_ts = anchor if anchor_from_db is not None else (max_ts or now_utc)
        computed_wall = now_utc.astimezone(IST).isoformat()
        # Prefer the live WebSocket spot for accurate ATM calculation
        final_spot = live_spot if live_spot is not None else spot

        reg_entry = get_registry().get(symbol)
        lot_size = (reg_entry.lot_size if reg_entry else 0) or settings.nifty_lot_size
        step = (reg_entry.strike_step if reg_entry else 0) or settings.strike_step

        # ATM strike + synthetic future + ATM IV for header metrics
        atm_iv: float | None = None
        synth: float | None = None
        if final_spot is not None and rows and step > 0:
            atm_strike = round(float(final_spot) / step) * step
            atm_row = min(rows, key=lambda r: abs(r.strike - atm_strike))
            synth = synthetic_future(
                final_spot, atm_row.call_ltp, atm_row.put_ltp, float(atm_row.strike)
            )
            ivs = [v for v in (atm_row.call_iv, atm_row.put_iv) if v is not None]
            if ivs:
                atm_iv = sum(ivs) / len(ivs)

        # IVP from accumulated iv_daily history (None until enough days exist)
        ivp: float | None = None
        if atm_iv is not None:
            try:
                from .iv_history import compute_ivp

                ivp = await compute_ivp(symbol, atm_iv)
            except Exception:
                ivp = None

        return OptionChainFullResponse(
            timeframe=timeframe,
            expiry=expiry.isoformat(),
            spot=final_spot,
            asof=asof_ts.astimezone(IST).isoformat(),
            computed_at=computed_wall,
            lot_size=lot_size,
            rows=rows,
            synthetic_future=synth,
            atm_iv=atm_iv,
            ivp=ivp,
        )


_singleton: OptionChainFullEngine | None = None


def get_option_chain_full_engine() -> OptionChainFullEngine:
    global _singleton
    if _singleton is None:
        _singleton = OptionChainFullEngine()
    return _singleton
