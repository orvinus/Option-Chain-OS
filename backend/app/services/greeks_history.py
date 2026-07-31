"""Intraday per-strike greeks/IV persistence + windowed read for replay.

Writes IV + delta/gamma/theta/vega per (strike, option_type) into
``greeks_snapshots`` so historical replay/exports can show the greeks that were
actually computed live, instead of recomputing them on read (which is
non-deterministic — it depends on the live spot override and time-to-expiry at
compute time). Populated going forward by ``_greeks_history_loop`` in main.py.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import text

from ..core.db import AsyncSessionLocal
from ..core.logging import get_logger
from ..core.time_utils import IST

log = get_logger("services.greeks_history")

_UPSERT_GREEKS_SQL = text(
    """
    INSERT INTO greeks_snapshots (ts, symbol, expiry, strike, option_type, iv, delta, gamma, theta, vega)
    VALUES (:ts, :symbol, :expiry, :strike, :option_type, :iv, :delta, :gamma, :theta, :vega)
    ON CONFLICT (ts, symbol, expiry, strike, option_type) DO UPDATE SET
        iv = EXCLUDED.iv, delta = EXCLUDED.delta, gamma = EXCLUDED.gamma,
        theta = EXCLUDED.theta, vega = EXCLUDED.vega
    """
)

# Windowed greeks series for replay: last-known greeks per (strike, option_type)
# carried forward into each bucket (mirrors the OI replay query).
_GREEKS_SERIES_SQL = text(
    """
    SELECT
        time_bucket_gapfill((:step_iv)::interval, ts, :start, :end) AS bucket,
        strike, option_type,
        locf(last(iv, ts))    AS iv,
        locf(last(delta, ts)) AS delta,
        locf(last(gamma, ts)) AS gamma,
        locf(last(theta, ts)) AS theta,
        locf(last(vega, ts))  AS vega
    FROM greeks_snapshots
    WHERE symbol = :symbol AND expiry = :expiry AND ts >= :start AND ts <= :end
    GROUP BY 1, 2, 3
    """
)


async def snapshot_greeks_for_symbol(symbol: str, timeframe: str = "5m") -> None:
    """Compute the current chain's per-strike greeks and persist them."""
    from ..api._expiry_utils import resolve_expiry
    from .option_chain_full import get_option_chain_full_engine

    try:
        expiry = await resolve_expiry(None, symbol=symbol)
        engine = get_option_chain_full_engine()
        res = await engine.get(timeframe, expiry, symbol=symbol)

        # This loop runs 24/7; without a guard it stamped ts=now onto greeks recomputed
        # from the LAST session's prices every weekend, holiday and overnight tick, so
        # replay/exports showed "greeks" for hours the market never traded. Only persist
        # when the chain is actually from the current session (same test as iv_history).
        if res.asof:
            try:
                if datetime.fromisoformat(res.asof).astimezone(IST).date() != datetime.now(IST).date():
                    log.info("greeks_history.skip_stale_chain", symbol=symbol, asof=res.asof)
                    return
            except ValueError:
                pass

        ts = datetime.now(timezone.utc)
        params: list[dict] = []
        for r in res.rows:
            if any(v is not None for v in (r.call_iv, r.call_delta, r.call_gamma, r.call_theta, r.call_vega)):
                params.append({
                    "ts": ts, "symbol": symbol.upper(), "expiry": expiry,
                    "strike": r.strike, "option_type": "CE",
                    "iv": r.call_iv, "delta": r.call_delta, "gamma": r.call_gamma,
                    "theta": r.call_theta, "vega": r.call_vega,
                })
            if any(v is not None for v in (r.put_iv, r.put_delta, r.put_gamma, r.put_theta, r.put_vega)):
                params.append({
                    "ts": ts, "symbol": symbol.upper(), "expiry": expiry,
                    "strike": r.strike, "option_type": "PE",
                    "iv": r.put_iv, "delta": r.put_delta, "gamma": r.put_gamma,
                    "theta": r.put_theta, "vega": r.put_vega,
                })
        if not params:
            return
        async with AsyncSessionLocal() as s:
            await s.execute(_UPSERT_GREEKS_SQL, params)
            await s.commit()
        log.info("greeks_history.snapshot", symbol=symbol, rows=len(params))
    except Exception as e:  # noqa: BLE001 — best-effort background snapshot
        log.warning("greeks_history.snapshot_failed", symbol=symbol, error=str(e))


async def fetch_greeks_series(
    symbol: str, expiry: date, start: datetime, end: datetime, step_iv: str,
) -> dict[datetime, dict[tuple[int, str], dict]]:
    """Return {bucket -> {(strike, option_type) -> {iv,delta,gamma,theta,vega}}}.

    Empty when nothing has been persisted yet for the window (greeks are only
    populated going forward, so pre-deploy sessions have none).
    """
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(
                _GREEKS_SERIES_SQL,
                {"step_iv": step_iv, "symbol": symbol, "expiry": expiry, "start": start, "end": end},
            )
        ).mappings().all()
    out: dict[datetime, dict[tuple[int, str], dict]] = {}
    for r in rows:
        if r["iv"] is None and r["delta"] is None:
            continue
        out.setdefault(r["bucket"], {})[(r["strike"], r["option_type"])] = {
            "iv": r["iv"], "delta": r["delta"], "gamma": r["gamma"],
            "theta": r["theta"], "vega": r["vega"],
        }
    return out
