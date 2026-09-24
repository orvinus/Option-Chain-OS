"""Engine evaluation endpoints for the Algo Config dashboards
(`/api/algo/engines/*`, admin-gated).

These are QUERY-mode evaluations: build the input series from stored data and
run the pure engine with the requested zone's parameters — exactly what the
orchestrator will do live, so the dashboard always shows what the engine
would decide, never a parallel opinion. Historical dates replay any stored
session through the same path.
"""
from __future__ import annotations

from dataclasses import asdict
from dataclasses import replace as _dc_replace
from datetime import date as _date
from datetime import datetime, time, timezone
from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query

from ..algo.auth import AdminIdentity, require_admin
from ..algo.config_models import WEEKDAYS, ZONE_IDS, Weekday, ZoneId
from ..algo.config_store import get_config_store
from ..algo.engines import mqae, mtf_ratio, oi_structure
from ..algo.engines.ump import UmpEngine
from ..algo.series import (
    PremiumMinute,
    build_oi_change_pair,
    build_premium_life,
    build_ratio_pair,
    resolve_atm_strike,
)
from ..core.time_utils import IST
from ._expiry_utils import resolve_expiry

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/algo/engines", tags=["algo-engines"])


def _parse_day(day: str) -> Weekday:
    d = day.lower()
    if d not in WEEKDAYS:
        raise HTTPException(400, f"day must be one of {', '.join(WEEKDAYS)}")
    return d  # type: ignore[return-value]


def _parse_zone(zone: str) -> ZoneId:
    z = zone.upper()
    if z not in ZONE_IDS:
        raise HTTPException(400, f"zone must be one of {', '.join(ZONE_IDS)}")
    return z  # type: ignore[return-value]


def _window_for_date(date_s: Optional[str]) -> tuple[Optional[datetime], Optional[datetime]]:
    if date_s is None:
        return None, None
    try:
        d = _date.fromisoformat(date_s)
    except ValueError as e:
        raise HTTPException(400, f"invalid date {date_s!r}") from e
    # ist_naive_to_utc, NEVER tzinfo=IST: attaching the pytz zone directly
    # selects its +05:53 LMT entry and silently shifts the whole window 23
    # minutes early (the archive-corruption incident, in API form — this very
    # line had it: historical windows ran 08:52→15:17 instead of 09:15→15:40).
    from ..core.time_utils import ist_naive_to_utc, session_close_min

    close_min = session_close_min(d)   # 15:40 from 2026-08-03, 15:30 before
    return (
        ist_naive_to_utc(datetime.combine(d, time(9, 15))),
        ist_naive_to_utc(datetime.combine(d, time(close_min // 60, close_min % 60))),
    )


async def _config_for(
    config_version: Optional[int],
    config_run: Optional[int] = None,
    config_sandbox: bool = False,
):
    """The evaluation config source, in priority order:

    1. ``config_run``     — a backtest run's FROZEN document (works for every
       run, including inline/sandbox runs whose config_version is NULL);
    2. ``config_sandbox`` — the Backtesting workspace's editable document;
    3. ``config_version`` — a saved live-config version;
    4. live (default).
    """
    from types import SimpleNamespace

    from ..algo.config_models import AlgoConfig

    if config_run is not None:
        from ..algo.backtest import store as bt_store

        run = await bt_store.get_run(config_run, include_config=True)
        if run is None:
            raise HTTPException(404, f"backtest run {config_run} not found")
        return SimpleNamespace(
            config=AlgoConfig.model_validate(run["config"]),
            version=run.get("config_version"),
        )
    if config_sandbox:
        from ..algo.backtest import store as bt_store

        row = await bt_store.get_sandbox()
        if row is None:
            raise HTTPException(404, "sandbox config not created yet — open the Backtesting page once")
        return SimpleNamespace(
            config=AlgoConfig.model_validate(row["config"]), version=None
        )
    store = get_config_store()
    if config_version is None:
        return await store.get_live()
    cv = await store.get_version(config_version)
    if cv is None:
        raise HTTPException(404, f"config version {config_version} not found")
    return cv


def _parse_at(at: Optional[str]) -> Optional[time]:
    if at is None:
        return None
    try:
        return time(*map(int, at.split(":")))
    except ValueError as e:
        # The route pattern only checks digit COUNT — 25:99 must still be a
        # clean client error, never a 500.
        raise HTTPException(422, f"invalid at {at!r} — hours 00-23, minutes 00-59") from e


def _clamp_to_cut(
    to_ts: Optional[datetime], date_s: Optional[str], cut: Optional[time]
) -> Optional[datetime]:
    """When replaying a historical date at a cut, the series WINDOW must end
    at the cut too — otherwise the ATM basket and strike membership resolve
    as of end-of-day, which is knowledge the live instant never had."""
    if cut is None or date_s is None or to_ts is None:
        return to_ts
    from ..core.time_utils import ist_naive_to_utc

    d = _date.fromisoformat(date_s)
    cut_utc = ist_naive_to_utc(datetime.combine(d, cut))
    return min(to_ts, cut_utc)


def _count_at_or_before(timestamps: list[str], cut: time) -> int:
    """How many leading ISO-IST buckets fall at/before the cut (series are
    chronological)."""
    n = 0
    for i, ts_s in enumerate(timestamps):
        if datetime.fromisoformat(ts_s).astimezone(IST).time() <= cut:
            n = i + 1
    return n


async def mtf_pair_at(
    sym: str,
    exp: _date,
    atm_window: int,
    date_s: Optional[str],
    cut: Optional[time],
):
    """THE Multi-TF data path — Algo Config's panel AND the main Multi-TF
    page (2026-09-23). The OI series the engine trades on: closed minutes
    only, the archive filling any minute the live feed missed, the strike
    basket resolved as of the cut. The main page used to read the live table
    alone, so a live-feed gap (15 Sep 13:58–14:23) read as zero change there
    while Algo Config showed the real move. Returns None with fewer than two
    closed minutes."""
    from_ts, to_ts = _window_for_date(date_s)
    to_ts = _clamp_to_cut(to_ts, date_s, cut)
    pair = await build_oi_change_pair(sym, exp, atm_window, from_ts=from_ts, to_ts=to_ts)
    if pair is None or len(pair.call_change_cr) < 2:
        return None
    if cut is not None:
        # TIME-REPLAY: point-in-time snapshot as of ``cut`` — the window is
        # already clamped above; this trims any residue.
        n = _count_at_or_before(pair.timestamps, cut)
        if n < 2:
            return None
        pair = _dc_replace(
            pair,
            timestamps=pair.timestamps[:n],
            call_change_cr=pair.call_change_cr[:n],
            put_change_cr=pair.put_change_cr[:n],
            call_change_full_cr=pair.call_change_full_cr[:n],
            put_change_full_cr=pair.put_change_full_cr[:n],
        )
    return pair


def replay_ump(
    minutes: list[PremiumMinute],
    params: Any,
    official_close: Optional[dict] = None,
    *,
    record_history: bool = False,
) -> tuple[UmpEngine, list[dict[str, Any]]]:
    """Pure full-life replay: feed EVERY stored session minute (all days) to
    a fresh engine with EMPTY feeds — exactly a TradingView chart evaluated
    from the contract's first bar. Daily/weekly/1H context grows inside the
    engine via day rollover; the Data-Ready Gate opening mid-replay as feeds
    accumulate IS the TV behaviour. Also builds the multi-day ENTRY-timeframe
    candles the chart plots (5 or 15 minutes per the zone; date-qualified
    keys, so day boundaries can't alias). ``record_history`` makes the engine
    keep its level/state histories for the chart replay."""
    from ..algo.series import aggregate_minutes, htf_seed_from_official

    engine = UmpEngine(params, official_close=official_close, record_history=record_history)
    # Pre-replay daily/weekly context (settlement bars for the days the
    # contract was listed but idle) — the same seed the live and backtest
    # warmups install, so this page, the dashboard and the engine agree.
    if minutes:
        first_ist = minutes[0].ts.astimezone(IST).replace(tzinfo=None).date()
        engine.seed_htf(htf_seed_from_official(official_close, first_ist))
    for m in minutes:
        ist = m.ts.astimezone(IST)
        engine.process_minute(ist.replace(tzinfo=None), m.o, m.h, m.l, m.c)
    tf_min = int(getattr(params.entry, "entry_timeframe_min", 5) or 5)
    return engine, aggregate_minutes(minutes, tf_min * 60)


# Every chart interval the UMP page offers (TradingView's NSE set): seconds
# per candle. Sub-minute = folded 1-second live rows (cursor day only);
# minute and longer = session-anchored folds of the stored minutes.
_DISPLAY_INTERVALS: dict[str, int] = {
    "1s": 1, "5s": 5, "15s": 15, "30s": 30,
    "1m": 60, "3m": 180, "5m": 300, "10m": 600, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "3h": 10800, "1d": 86400, "1W": 604800,
}
_INTERVAL_PATTERN = "^(entry|" + "|".join(_DISPLAY_INTERVALS) + ")$"


def default_cut_for(session_date: _date) -> time:
    """A pinned date without ``at`` cuts at that date's OWN session close
    (15:30 before 2026-08-03, 15:40 since) — never a fixed 15:30, which
    silently dropped the last ten minutes of every post-change day."""
    from ..core.time_utils import session_close_min

    cm = session_close_min(session_date)
    return time(cm // 60, cm % 60)


def _open_position_strike(sym: str, exp: _date, option_type: str) -> Optional[int]:
    """Strike of the orchestrator's open position when it is on this exact
    chain side (symbol, expiry, CE/PE); None otherwise or with no live engine
    in this process (replay box, Backtesting)."""
    try:
        from ..algo.orchestrator import get_orchestrator

        orch = get_orchestrator()
        pos = orch.position if orch is not None else None
        if pos is None:
            return None
        c = pos.contract
        if (
            str(c.symbol).upper() == sym
            and c.expiry == exp
            and str(c.option_type).upper() == option_type.upper()
        ):
            return int(c.strike)
    except Exception:  # noqa: BLE001 — a display nicety must never 500 the chart
        log.warning("algo.engines.position_pin_failed", exc_info=True)
    return None


def choose_display_candles(
    interval: str,
    entry_candles: list[dict[str, Any]],
    tf_min: int,
    minutes: list[PremiumMinute],
    seconds: Optional[list[PremiumMinute]],
    cursor_date: _date,
    official_close: Optional[dict] = None,
) -> dict[str, Any]:
    """Pure: pick the candle series the chart displays for ``interval``.
    ``seconds`` = the 1-second rows already fetched for the cursor day (None
    when not requested). An empty 1s fetch falls back to 1-minute candles
    with a visible note — the day predates the live table's retention, was
    served by the archive only, or the DB was not persisting at 1s.

    ``official_close`` extends the DAY and WEEK intervals back to the
    contract's listing with settlement bars, so the chart shows the same
    history the engine's DISCOVERY/BRIDGE levels are built from. Intraday
    intervals are deliberately left alone: TradingView's 4h chart starts at
    the first trade and so must ours."""
    from ..algo.series import (
        aggregate_minutes,
        daily_series_minutes,
        session_filled_minutes,
        settlement_only_dates,
    )

    if interval == "entry" or interval not in _DISPLAY_INTERVALS:
        return {
            "candles": entry_candles,
            "candles_interval": tf_min * 60,
            "candles_source": "entry",
            "candles_note": None,
            "candles_window": None,
        }
    sec = _DISPLAY_INTERVALS[interval]
    if sec >= 86400:
        # Day and week bars come from the exchange's own daily record (official
        # O/H/L/C on traded days, the settlement price on days the contract was
        # listed but idle), with the running session folded from its minutes.
        # That is what TradingView plots and what the engine's daily/weekly
        # feeds read, so the chart and the levels drawn on it agree.
        idle = settlement_only_dates(official_close, minutes)
        candles = aggregate_minutes(daily_series_minutes(official_close, minutes), sec)
        if not idle:
            return {
                "candles": candles,
                "candles_interval": sec,
                "candles_source": "minutes",
                "candles_note": None,
                "candles_window": None,
            }
        return {
            "candles": candles,
            "candles_interval": sec,
            "candles_source": "minutes+settlement",
            "candles_note": (
                f"{len(idle)} session(s) from {idle[0].isoformat()} come from the "
                "exchange settlement price — the contract was listed but did "
                "not trade"
            ),
            "candles_window": None,
            "settlement_dates": [d.isoformat() for d in idle],
        }
    if sec >= 60:
        # A thinly-traded strike leaves holes in its own session — NIFTY 23450
        # CE traded exactly once on 2026-09-03 — and the chart then shows one
        # candle for a whole day. Hold the last price across the untraded
        # minutes so every session runs open-to-close. Display only: the engine
        # still evaluates the real traded minutes, because TradingView draws no
        # bar for a minute with no trade and Pine never evaluates one.
        series, synthesized = session_filled_minutes(minutes, official_close)
        out = {
            "candles": aggregate_minutes(series, sec),
            "candles_interval": sec,
            "candles_source": "minutes",
            "candles_note": None,
            "candles_window": None,
        }
        if synthesized:
            out["candles_source"] = "minutes+held"
            out["candles_filled"] = synthesized
            out["candles_note"] = (
                f"{synthesized} minute(s) of this contract's sessions had no "
                "trade — shown at the last traded price"
            )
        return out
    if seconds:
        return {
            "candles": aggregate_minutes(seconds, sec),
            "candles_interval": sec,
            "candles_source": "live_1s",
            "candles_note": f"{interval} candles cover {cursor_date.isoformat()} only",
            "candles_window": {
                "from": seconds[0].ts.astimezone(IST).isoformat(),
                "to": seconds[-1].ts.astimezone(IST).isoformat(),
            },
        }
    return {
        "candles": aggregate_minutes(minutes, 60),
        "candles_interval": 60,
        "candles_source": "minutes",
        "candles_note": (
            f"sub-minute data is not stored for {cursor_date.isoformat()} — showing 1-minute candles"
        ),
        "candles_window": None,
    }


def _focus_window(
    session_dates: list[_date], expiry: _date
) -> Optional[dict[str, str]]:
    """The expiry-to-expiry week the chart opens zoomed to: previous-expiry+1
    (the day after the prior weekly settled) through the expiry itself,
    anchored on the expiry's weekday and clamped to the stored data."""
    if not session_dates:
        return None
    from datetime import timedelta as _td

    last_d = session_dates[-1]
    anchor_wd = expiry.weekday()
    to_d = last_d + _td(days=(anchor_wd - last_d.weekday()) % 7)
    if to_d > expiry:
        to_d = expiry
    from_d = max(session_dates[0], to_d - _td(days=6))
    return {"from": from_d.isoformat(), "to": to_d.isoformat()}


@router.get("/ump")
async def ump_eval(
    day: str = Query(...),
    zone: str = Query(...),
    date: Optional[str] = Query(default=None),
    expiry: Optional[str] = Query(default=None),
    symbol: Optional[str] = Query(default=None),
    strike: Optional[int] = Query(default=None),
    option_type: str = Query(default="CE", pattern="^(CE|PE)$"),
    at: Optional[str] = Query(default=None, pattern=r"^\d{2}:\d{2}$"),
    config_version: Optional[int] = Query(default=None),
    config_run: Optional[int] = Query(default=None),
    config_sandbox: bool = Query(default=False),
    interval: str = Query(default="entry", pattern=_INTERVAL_PATTERN),
    history: bool = Query(default=False),
    _: AdminIdentity = Depends(require_admin),
) -> dict[str, Any]:
    """Replay the Ultra Master Pro engine over one contract's ENTIRE stored
    life (all days, expiry-to-expiry and beyond — the TradingView chart) with
    the zone's parameters. ``date``/``at`` form the AS-OF cursor: the replay
    always starts at the contract's first stored bar and stops there; blank =
    full life up to now (live). ``interval`` selects the DISPLAY candles only
    (the engine always evaluates its entry timeframe); ``1s`` is served for the
    cursor day from the live tick table and falls back to 1-minute with a
    note. ``history=true`` adds the level/state histories the chart replay
    needs to reconstruct any earlier instant without another request."""
    day_key = _parse_day(day)
    zone_key = _parse_zone(zone)

    cv = await _config_for(config_version, config_run, config_sandbox)
    zone_cfg = cv.config.zone(day_key, zone_key)
    day_cfg = cv.config.days.get(day_key)
    if zone_cfg is None or day_cfg is None:
        raise HTTPException(404, f"no configuration for {day_key} {zone_key}")

    sym = (symbol or day_cfg.index_symbol).upper()
    exp = await resolve_expiry(expiry, sym)

    session_date: Optional[_date] = None
    if date is not None:
        try:
            session_date = _date.fromisoformat(date)
        except ValueError as e:
            raise HTTPException(400, f"invalid date {date!r}") from e

    # The AS-OF cursor — one instant that bounds BOTH the engine replay and
    # the band-candidate/ladder anchoring, so the badge never claims band
    # authority for a moment the replay isn't showing.
    cut = _parse_at(at)
    from ..core.time_utils import ist_naive_to_utc, now_ist

    cursor_utc: Optional[datetime] = None
    if session_date is not None:
        cursor_utc = ist_naive_to_utc(
            datetime.combine(session_date, cut or default_cut_for(session_date))
        )
    elif cut is not None:
        cursor_utc = ist_naive_to_utc(datetime.combine(now_ist().date(), cut))

    # Default strike = what the orchestrator would actually hunt: the zone's
    # premium-band pick (nearest band-mid). ATM only as a last resort — the
    # old ATM default showed a contract the engine would often never trade.
    band_candidates: list[dict[str, Any]] = []
    try:
        from ..algo.series import band_candidates_at

        ref_utc = cursor_utc or datetime.now(timezone.utc)
        from ..algo.series import freshest_row_ts, strike_ladder_at

        cands = []
        if cursor_utc is None:
            # LIVE: rank with the orchestrator's OWN picker (live table, same
            # moment) so the chart shows the strike the engine actually hunts.
            # band_candidates_at reads the unified view and could name a
            # different strike, and the live candle is drawn only when the
            # chart and the stream agree (2026-09-23 5-6-minute freeze).
            from ..algo.series import select_strikes_in_band

            cands = await select_strikes_in_band(
                sym, exp, option_type,
                zone_cfg.premium_min, zone_cfg.premium_max,
                zone_cfg.strike_scan_count,
            )
        if not cands:
            cands = await band_candidates_at(
                sym, exp, option_type,
                zone_cfg.premium_min, zone_cfg.premium_max,
                zone_cfg.strike_scan_count, ref_utc,
            )
        ladder = await strike_ladder_at(sym, exp, option_type, ref_utc)
        if not ladder:
            # Off-hours (or a not-yet-traded chain): the 15-minute window at
            # NOW is empty. Re-anchor to the chain's last stored moment so
            # the page shows the ladder frozen at the close, not nothing.
            last_ts = await freshest_row_ts(sym, exp)
            if last_ts is not None:
                ref_utc = last_ts
                cands = await band_candidates_at(
                    sym, exp, option_type,
                    zone_cfg.premium_min, zone_cfg.premium_max,
                    zone_cfg.strike_scan_count, ref_utc,
                )
                ladder = await strike_ladder_at(sym, exp, option_type, ref_utc)
        band_candidates = [{"strike": s, "premium": p} for s, p in cands]
        all_strikes = [{"strike": s, "premium": p} for s, p in ladder[:120]]
        candidates_as_of = ref_utc.astimezone(IST).isoformat()
    except Exception as e:
        log.warning("algo.engines.band_candidates_failed", error=str(e))
        all_strikes = []
        candidates_as_of = None
    position_strike = (
        _open_position_strike(sym, exp, option_type)
        if strike is None and cursor_utc is None and config_run is None and not config_sandbox
        else None
    )
    if strike is not None:
        strike_val: Optional[int] = strike
        strike_source = "manual"
    elif position_strike is not None:
        # The engine holds a trade on this chain side: show THAT contract. The
        # band pick moves with the premium exactly while a trade runs, and the
        # chart used to hop to another strike (and lose its live candle) the
        # moment an entry came in (reported 2026-09-23).
        strike_val = position_strike
        strike_source = "position"
    elif band_candidates:
        strike_val = int(band_candidates[0]["strike"])
        strike_source = "band"
    else:
        strike_val = await resolve_atm_strike(sym, exp)
        strike_source = "atm_fallback"
    if strike_val is None:
        raise HTTPException(404, f"no stored spot for {sym} {exp.isoformat()} to derive ATM")

    life = await build_premium_life(
        sym, exp, strike_val, option_type, cut_utc=cursor_utc
    )
    if life is None or len(life.minutes) < 2:
        raise HTTPException(
            404,
            f"no stored premium data for {sym} {exp.isoformat()} {strike_val}{option_type}"
            + (
                f" at/before {cursor_utc.astimezone(IST).isoformat()}"
                if cursor_utc is not None
                else ""
            ),
        )

    # Full-life replay — feeds start EMPTY and grow inside the engine
    # (day rollover + session-anchored 1H accumulation), exactly a TV chart
    # evaluated from the contract's first stored bar.
    _t0 = datetime.now(timezone.utc)
    engine, entry_candles = replay_ump(
        life.minutes, zone_cfg.ump, life.official_close, record_history=history
    )
    log.info(
        "algo.engines.ump_replay",
        replay_ms=round((datetime.now(timezone.utc) - _t0).total_seconds() * 1000),
        minutes=len(life.minutes),
        days=len(life.session_dates),
    )
    tf_min = int(zone_cfg.ump.entry.entry_timeframe_min)

    # Display candles (the engine's evaluation is untouched by this).
    cursor_date = life.session_dates[-1]
    seconds: Optional[list[PremiumMinute]] = None
    if _DISPLAY_INTERVALS.get(interval, 60) < 60:
        from ..algo.series import fetch_premium_seconds

        day_open = ist_naive_to_utc(datetime.combine(cursor_date, time(9, 15)))
        day_end = cursor_utc or datetime.now(timezone.utc)
        _t1 = datetime.now(timezone.utc)
        try:
            seconds = await fetch_premium_seconds(
                sym, exp, strike_val, option_type, day_open, day_end
            )
        except Exception as e:  # noqa: BLE001 — a display extra must not 500 the eval
            log.warning("algo.engines.ump_1s_failed", error=str(e))
            seconds = []
        log.info(
            "algo.engines.ump_1s",
            rows=len(seconds),
            ms=round((datetime.now(timezone.utc) - _t1).total_seconds() * 1000),
        )
    display = choose_display_candles(
        interval, entry_candles, tf_min, life.minutes, seconds, cursor_date,
        life.official_close,
    )

    # REPLAY SUB-CANDLES (2026-09-13). The chart animates price moving INSIDE a
    # forming candle, which needs a finer series than the one being displayed.
    # It is sent only under ``history`` (already replay-only) so live and
    # backtest responses are byte-identical to before, and it is built from the
    # SAME filled minute series the display candles fold — otherwise on a thin
    # strike the sub-candles would not tile their parent and the forming bar
    # would visibly disagree with the closed one.
    sub_sec = 60
    if history and int(display.get("candles_interval") or 0) > sub_sec:
        from ..algo.series import (
            aggregate_minutes,
            daily_series_minutes,
            session_filled_minutes,
        )

        disp_sec = int(display["candles_interval"])
        if disp_sec >= 86400:
            base_minutes = daily_series_minutes(life.official_close, life.minutes)
        else:
            base_minutes, _ = session_filled_minutes(life.minutes, life.official_close)
        display["subcandles"] = aggregate_minutes(base_minutes, sub_sec)
        display["subcandles_interval"] = sub_sec

    # The zone band belongs to the LIVE trade (Pine's Step-4 guide lines
    # delete on exit) — returning it after the exit left a stale blue band
    # painted on the chart.
    zone_view = None
    if engine.in_trade and engine.zone is not None:
        z = engine.zone
        zone_view = {"base": z.b, "zone_top": z.zt, "upper_median": z.um,
                     "lower_median": z.lm, "zone_bottom": z.zb}

    # Q1/Q2/Q3 + NB target are in-trade guide lines, exactly like the zone
    # band above. ``_exit`` keeps ``base_level`` (Pine keeps it too), so gating
    # on it alone drew the Q-lines on an IDLE engine for the rest of the
    # contract's life after its first trade (reported 2026-09-23).
    nb = (
        engine.nearest_above(engine.base_level)
        if engine.in_trade and engine.base_level is not None
        else None
    )
    q_levels = None
    if nb is not None and engine.base_level is not None:
        b = engine.base_level
        q2 = (b + nb) / 2
        q_levels = {"q1": (b + q2) / 2, "q2": q2, "q3": (q2 + nb) / 2, "nb": nb}

    # Pine's f_nearest dashboard rows: nearest resistance/support around the
    # running close, with signed distance %.
    last_close = engine.last_close
    nearest_res = nearest_sup = None
    if last_close is not None and last_close > 0:
        r = engine.nearest_above(last_close)
        s = engine.nearest_below(last_close)
        if r is not None:
            nearest_res = {"price": r, "distance_pct": round((r - last_close) / last_close * 10000) / 100}
        if s is not None:
            nearest_sup = {"price": s, "distance_pct": round((last_close - s) / last_close * 10000) / 100}
    # Previous COMPLETED daily close at the cursor (Pine's dashboard price
    # row compares against it) — read from the engine's GROWN feeds.
    prev_close = engine.feeds.daily[0][2] if engine.feeds.daily else None

    return {
        "day": day_key,
        "zone": zone_key,
        "symbol": sym,
        "expiry": exp.isoformat(),
        "strike": strike_val,
        "strike_source": strike_source,
        "band_candidates": band_candidates,
        "all_strikes": all_strikes,
        "candidates_as_of": candidates_as_of,
        "option_type": option_type,
        "replay_at": at,
        "config_version": config_version,
        "config_run": config_run,
        "config_sandbox": config_sandbox,
        # The cursor's IST date (= the last replayed day). The replay itself
        # always spans first_session_date → session_date.
        "session_date": life.session_dates[-1].isoformat(),
        "first_session_date": life.session_dates[0].isoformat(),
        "session_dates": [d.isoformat() for d in life.session_dates],
        "as_of": (
            (cursor_utc or datetime.now(timezone.utc)).astimezone(IST).isoformat()
        ),
        "focus_window": _focus_window(life.session_dates, exp),
        "gate": {
            "daily_feeds": len(engine.feeds.daily),
            "weekly_feed": engine.feeds.weekly is not None,
            "h1_candles": len(engine.feeds.h1_candles),
            "levels_ready": engine.levels_ready,
        },
        "state": engine.state,
        "sub_scenario": engine.sub,
        "entry_price": engine.entry_price if engine.in_trade else None,
        "base_level": engine.base_level if engine.in_trade else None,
        "max_sl": engine.max_sl,
        "trail_sl": engine.trail_sl,
        "sb_trail_sl": engine.sb_trail_sl,
        "sb_stage": engine.sb_stage,
        "levels": [
            {"price": lv.price, "type": lv.type, "name": lv.name} for lv in engine.levels
        ],
        "active_zone": zone_view,
        "q_levels": q_levels,
        "nearest_resistance": nearest_res,
        "nearest_support": nearest_sup,
        "prev_close": prev_close,
        "entry_timeframe_min": tf_min,
        "entry_candles": entry_candles,
        **display,
        "levels_history": (
            [
                {
                    "ts": ts,
                    "levels": [{"price": lv.price, "type": lv.type, "name": lv.name} for lv in lvs],
                }
                for ts, lvs in engine.levels_history
            ]
            if history
            else None
        ),
        "state_history": engine.state_history if history else None,
        "events": [
            {"ts": e.ts, "kind": e.kind, "price": e.price, "text": e.text}
            for e in engine.events
        ],
        "trades": [
            {
                "entry_ts": t.entry_ts,
                "entry_price": t.entry_price,
                "sub_scenario": t.sub_scenario,
                "base_level": t.base_level,
                "exit_ts": t.exit_ts,
                "exit_price": t.exit_price,
                "exit_reason": t.exit_reason,
                "pnl_points": (
                    round((t.exit_price - t.entry_price) * 100) / 100
                    if t.exit_price is not None
                    else None
                ),
            }
            for t in engine.trades
        ],
        "params": zone_cfg.ump.model_dump(),
    }


# One-day OI-integrated simulations are pure functions of (config, day, last
# closed minute) — cache them so chart polling and replay re-opens are free.
_OI_CYCLE_CACHE: "dict[tuple, dict[str, Any]]" = {}


@router.get("/ump/oi-cycle")
async def ump_oi_cycle(
    day: str = Query(...),
    zone: str = Query(...),
    date: Optional[str] = Query(default=None),
    config_version: Optional[int] = Query(default=None),
    config_run: Optional[int] = Query(default=None),
    config_sandbox: bool = Query(default=False),
    _: AdminIdentity = Depends(require_admin),
) -> dict[str, Any]:
    """The OI-integrated entry layer for the Ultra Master Pro chart
    (2026-09-25): the same orchestrator live and the backtest run, simulated
    over one day — OI signal windows plus the trades the platform takes, each
    with its engine events. The chart's own ``/ump`` replay is independent of
    OI and stays as it is. Today is simulated only up to the last CLOSED
    minute. A zone with the Fresh-OI switch OFF is not calculated."""
    import hashlib
    from datetime import date as _d

    from ..algo.backtest.oi_cycle import simulate_day
    from ..core.time_utils import now_ist

    day_key = _parse_day(day)
    zone_key = _parse_zone(zone)
    cv = await _config_for(config_version, config_run, config_sandbox)
    zone_cfg = cv.config.zone(day_key, zone_key)
    if zone_cfg is None:
        raise HTTPException(404, f"no configuration for {day_key} {zone_key}")
    if not zone_cfg.oi_fresh_entries:
        return {"enabled": False, "reason": "Fresh OI-integrated entry calculation is OFF for this zone"}
    today = now_ist().date()
    try:
        d = _d.fromisoformat(date) if date else today
    except ValueError as e:
        raise HTTPException(400, f"invalid date {date!r}") from e
    up_to = now_ist().replace(tzinfo=None) if d == today else None
    doc_hash = hashlib.sha1(cv.config.model_dump_json(by_alias=True).encode()).hexdigest()[:16]
    key = (d.isoformat(), doc_hash, up_to.strftime("%H:%M") if up_to else "close")
    hit = _OI_CYCLE_CACHE.get(key)
    if hit is None:
        res = await simulate_day(cv.config, d, up_to=up_to)
        if res is None:
            raise HTTPException(404, f"no stored data to simulate {d.isoformat()}")
        hit = {"enabled": True, **res}
        if len(_OI_CYCLE_CACHE) > 64:
            _OI_CYCLE_CACHE.clear()
        _OI_CYCLE_CACHE[key] = hit
    return hit


@router.get("/mqae")
async def mqae_eval(
    day: str = Query(...),
    zone: str = Query(...),
    date: Optional[str] = Query(default=None),
    expiry: Optional[str] = Query(default=None),
    symbol: Optional[str] = Query(default=None),
    at: Optional[str] = Query(default=None, pattern=r"^\d{2}:\d{2}$"),
    config_version: Optional[int] = Query(default=None),
    config_run: Optional[int] = Query(default=None),
    config_sandbox: bool = Query(default=False),
    # Preview a timeframe before it is saved (the panel's pills). Omitted =
    # the zone's saved ``mqae.timeframe`` — what live trading uses.
    timeframe: Optional[str] = Query(default=None, pattern=r"^(1m|5m|10m|15m|30m|full_day)$"),
    # The panel's UNSAVED Ratio parameters (JSON of MqaeParams). PDS §11: a kill
    # switch or parameter change recalculates everything at once, retroactively —
    # before Save. Preview only: live trading keeps reading the saved config.
    params: Optional[str] = Query(default=None, max_length=4000),
    _: AdminIdentity = Depends(require_admin),
) -> dict[str, Any]:
    import json as _json

    from pydantic import ValidationError

    from ..algo.config_models import MqaeParams
    from ..algo.series import ratio_pair_for_timeframe

    day_key = _parse_day(day)
    zone_key = _parse_zone(zone)

    cv = await _config_for(config_version, config_run, config_sandbox)
    zone_cfg = cv.config.zone(day_key, zone_key)
    day_cfg = cv.config.days.get(day_key)
    if zone_cfg is None or day_cfg is None:
        raise HTTPException(404, f"no configuration for {day_key} {zone_key}")

    sym = (symbol or day_cfg.index_symbol).upper()
    exp = await resolve_expiry(expiry, sym)
    from_ts, to_ts = _window_for_date(date)

    cut = _parse_at(at)
    to_ts = _clamp_to_cut(to_ts, date, cut)

    preview = False
    if params is not None:
        try:
            eval_params = MqaeParams.model_validate(_json.loads(params))
        except (ValueError, ValidationError) as e:
            raise HTTPException(422, f"invalid Ratio parameters: {str(e)[:200]}") from e
        preview = eval_params != zone_cfg.mqae
    else:
        eval_params = zone_cfg.mqae
    params = eval_params
    if timeframe is not None and timeframe != params.timeframe:
        params = params.model_copy(update={"timeframe": timeframe})
    pair = await build_ratio_pair(
        sym, exp, params.strikes_atm_window, from_ts=from_ts, to_ts=to_ts
    )
    if pair is None or len(pair.green_pcr) < 2:
        raise HTTPException(
            404,
            f"no stored 1-minute data for {sym} {exp.isoformat()}"
            + (f" on {date}" if date else ""),
        )

    # TIME-REPLAY: evaluate as if the session had ended at ``at`` (IST wall
    # clock) — the window above already ends there; this trims any residue.
    if cut is not None:
        n = _count_at_or_before(pair.timestamps, cut)
        if n < 2:
            raise HTTPException(404, f"no data at or before {at} IST")
        pair = _dc_replace(
            pair,
            timestamps=pair.timestamps[:n],
            green_pcr=pair.green_pcr[:n],
            yellow_ratio=pair.yellow_ratio[:n],
            total_call_oi=pair.total_call_oi[:n],
            total_put_oi=pair.total_put_oi[:n],
        )

    # The features run on the selected timeframe; the 1-minute arrays above
    # stay in the response for the chart (it draws any interval from them).
    eng = ratio_pair_for_timeframe(pair, params.timeframe)
    if eng is None or len(eng.green_pcr) < 2:
        label = "Full Day (change since 09:15)" if params.timeframe == "full_day" else params.timeframe
        raise HTTPException(
            404,
            f"no data at or before {at or 'now'} IST — fewer than two closed {label} candles yet",
        )
    result = mqae.evaluate(eng.green_pcr, eng.yellow_ratio, params)
    history = mqae.history_transitions(eng.green_pcr, eng.yellow_ratio, params)

    def _logs(logs: list[mqae.ModelLog]) -> list[dict[str, Any]]:
        return [{"model": l.model, "text": l.text, "points": l.points} for l in logs]

    def _swings(swings: list[mqae.MqaeSwing]) -> list[dict[str, Any]]:
        return [asdict(s) for s in swings]

    def _blocks(blocks: list[mqae.OrderBlock]) -> list[dict[str, Any]]:
        return [
            {"type": b.type, "top": b.top, "bot": b.bot, "origin_index": b.origin_index}
            for b in blocks
        ]

    def _bar_ts(bar: int) -> Optional[str]:
        # history bars are slice LENGTHS: the state at bar i is evaluated on
        # the first i points, so its wall-clock moment is timestamps[i-1].
        return eng.timestamps[bar - 1] if 0 < bar <= len(eng.timestamps) else None

    return {
        "day": day_key,
        "zone": zone_key,
        "symbol": sym,
        "expiry": exp.isoformat(),
        "replay_at": at,
        "config_version": config_version,
        "config_run": config_run,
        "config_sandbox": config_sandbox,
        "strike_min": pair.strike_min,
        "strike_max": pair.strike_max,
        "spot": pair.spot,
        "timestamps": pair.timestamps,
        "green_pcr": pair.green_pcr,
        "yellow_ratio": pair.yellow_ratio,
        # Chart extras (Ratio-tab tooltip Call/Put rows + Full-Day mode).
        "total_call_oi": pair.total_call_oi,
        "total_put_oi": pair.total_put_oi,
        # What the five models actually ran on. Overlay indices (swings, order
        # blocks, rider, velocity) and history bars index THESE arrays.
        "timeframe": params.timeframe,
        # True when the result reflects unsaved panel edits, not the saved config.
        "preview": preview,
        "engine_timestamps": eng.timestamps,
        "engine_green": eng.green_pcr,
        "engine_yellow": eng.yellow_ratio,
        "signal": result.signal,
        "total": result.total,
        "score_green": result.score_green,
        "score_yellow": result.score_yellow,
        "score_cross": result.score_cross,
        "logs_green": _logs(result.logs_green),
        "logs_yellow": _logs(result.logs_yellow),
        "logs_cross": _logs(result.logs_cross),
        "green_swings": _swings(result.green_swings),
        "yellow_swings": _swings(result.yellow_swings),
        "green_blocks": _blocks(result.green_blocks),
        "yellow_blocks": _blocks(result.yellow_blocks),
        "green_rider": [{"val": r.val, "trend": r.trend} for r in result.green_rider],
        "green_velocity": result.green_velocity,
        "yellow_velocity": result.yellow_velocity,
        "history": [
            {"bar": h.bar, "ts": _bar_ts(h.bar), "signal": h.signal, "total": h.total}
            for h in history
        ],
        "params": params.model_dump(),
    }


@router.get("/mtf-ratio")
async def mtf_ratio_eval(
    day: str = Query(...),
    zone: str = Query(...),
    date: Optional[str] = Query(default=None),
    expiry: Optional[str] = Query(default=None),
    symbol: Optional[str] = Query(default=None),
    at: Optional[str] = Query(default=None, pattern=r"^\d{2}:\d{2}$"),
    config_version: Optional[int] = Query(default=None),
    config_run: Optional[int] = Query(default=None),
    config_sandbox: bool = Query(default=False),
    # DISPLAY override (2026-09-25): the panel passes the strike window its
    # box shows, so an unsaved edit can never leave the table computed for a
    # different window than the one on screen. Trading always uses the saved
    # config; this never reaches the orchestrator.
    atm_window: Optional[int] = Query(default=None, ge=-1, le=60),
    _: AdminIdentity = Depends(require_admin),
) -> dict[str, Any]:
    day_key = _parse_day(day)
    zone_key = _parse_zone(zone)

    cv = await _config_for(config_version, config_run, config_sandbox)
    zone_cfg = cv.config.zone(day_key, zone_key)
    day_cfg = cv.config.days.get(day_key)
    if zone_cfg is None or day_cfg is None:
        raise HTTPException(404, f"no configuration for {day_key} {zone_key}")

    sym = (symbol or day_cfg.index_symbol).upper()
    exp = await resolve_expiry(expiry, sym)
    cut = _parse_at(at)

    params = zone_cfg.mtf_ratio
    window = atm_window if atm_window is not None else params.strikes_atm_window
    pair = await mtf_pair_at(sym, exp, window, date, cut)
    if pair is None:
        raise HTTPException(
            404,
            f"no stored 1-minute data for {sym} {exp.isoformat()}"
            + (f" on {date}" if date else "")
            + (f" at or before {at} IST" if at else ""),
        )

    from ..algo.series import mtf_input

    rows = mtf_ratio.rows_from_cumulative_series(
        *mtf_input(pair), params.timeframes
    )
    result = mtf_ratio.evaluate(rows, params)

    def _row_out(r: mtf_ratio.RatioReading) -> dict[str, Any]:
        return {
            "timeframe": r.timeframe,
            "call_delta_cr": r.call_delta,
            "put_delta_cr": r.put_delta,
            "side": r.side,
            "factor": None if r.factor == float("inf") else round(r.factor * 100) / 100,
            "text": r.text,
            "lowest_side": r.lowest_side,
            "call_sign": r.call_sign,
            "put_sign": r.put_sign,
        }

    return {
        "day": day_key,
        "zone": zone_key,
        "symbol": sym,
        "expiry": exp.isoformat(),
        "replay_at": at,
        "config_version": config_version,
        "config_run": config_run,
        "config_sandbox": config_sandbox,
        "strike_min": pair.strike_min,
        "strike_max": pair.strike_max,
        "spot": pair.spot,
        "as_of": pair.timestamps[-1],
        "rows": [_row_out(r) for r in result.rows],
        "direction": result.direction,
        "reading": result.reading,
        "matched_rule": result.matched_rule,
        "traces": [
            {
                "name": t.name,
                "on": t.on,
                "matched": t.matched,
                "out": t.out,
                "checks": [
                    {"timeframe": c.timeframe, "passed": c.passed, "reason": c.reason}
                    for c in t.checks
                ],
            }
            for t in result.traces
        ],
        "params": params.model_dump(),
    }


@router.get("/oi-structure")
async def oi_structure_eval(
    day: str = Query(..., description="monday…friday — selects the zone config"),
    zone: str = Query(..., description="Z1/Z2/Z3"),
    date: Optional[str] = Query(default=None, description="YYYY-MM-DD historical session"),
    expiry: Optional[str] = Query(default=None),
    symbol: Optional[str] = Query(default=None, description="override the day's index"),
    at: Optional[str] = Query(default=None, pattern=r"^\d{2}:\d{2}$"),
    config_version: Optional[int] = Query(default=None),
    config_run: Optional[int] = Query(default=None),
    config_sandbox: bool = Query(default=False),
    _: AdminIdentity = Depends(require_admin),
) -> dict[str, Any]:
    day_key = _parse_day(day)
    zone_key = _parse_zone(zone)

    cv = await _config_for(config_version, config_run, config_sandbox)
    zone_cfg = cv.config.zone(day_key, zone_key)
    day_cfg = cv.config.days.get(day_key)
    if zone_cfg is None or day_cfg is None:
        raise HTTPException(404, f"no configuration for {day_key} {zone_key}")

    sym = (symbol or day_cfg.index_symbol).upper()
    exp = await resolve_expiry(expiry, sym)
    from_ts, to_ts = _window_for_date(date)

    cut = _parse_at(at)
    to_ts = _clamp_to_cut(to_ts, date, cut)

    params = zone_cfg.oi_structure
    pair = await build_oi_change_pair(
        sym, exp, params.strikes_atm_window, from_ts=from_ts, to_ts=to_ts
    )
    if pair is None or len(pair.call_change_cr) < 2:
        raise HTTPException(
            404,
            f"no stored 1-minute data for {sym} {exp.isoformat()}"
            + (f" on {date}" if date else ""),
        )

    # TIME-REPLAY: swings/zones/breakouts as of ``at`` — window already
    # clamped above; this trims any residue.
    if cut is not None:
        n = _count_at_or_before(pair.timestamps, cut)
        if n < 2:
            raise HTTPException(404, f"no data at or before {at} IST")
        pair = _dc_replace(
            pair,
            timestamps=pair.timestamps[:n],
            call_change_cr=pair.call_change_cr[:n],
            put_change_cr=pair.put_change_cr[:n],
        )

    result = oi_structure.evaluate(pair.call_change_cr, pair.put_change_cr, params)
    evaluated_at = datetime.now(timezone.utc).astimezone(IST).isoformat()

    # Chart extra: per-strike Δ since open at the last CLOSED bucket the engine
    # used, so the dashboard-style strike bars sum to the series' last point.
    # Never lets a chart extra 500 the evaluation.
    by_strike: Optional[list[dict[str, Any]]] = None
    as_of: Optional[str] = pair.timestamps[-1] if pair.timestamps else None
    strike_step: Optional[int] = None
    try:
        from datetime import timedelta as _td

        from ..market.symbols import get_registry
        from ..services.oi_timeseries import fetch_oi_strike_deltas

        entry = get_registry().get(sym)
        strike_step = entry.strike_step if entry is not None else 50
        if as_of is not None:
            deltas_to = (
                datetime.fromisoformat(as_of) + _td(minutes=1) - _td(microseconds=1)
            )
            deltas = await fetch_oi_strike_deltas(
                sym, exp, pair.strike_min, pair.strike_max,
                from_ts=from_ts, to_ts=deltas_to,
            )
            by_strike = [asdict(d) for d in deltas]
    except Exception as e:  # noqa: BLE001 — chart extra only
        log.warning("oi_structure.by_strike_failed", symbol=sym, error=str(e))
        by_strike = None

    return {
        "day": day_key,
        "zone": zone_key,
        "symbol": sym,
        "expiry": exp.isoformat(),
        "replay_at": at,
        "config_version": config_version,
        "config_run": config_run,
        "config_sandbox": config_sandbox,
        "strike_min": pair.strike_min,
        "strike_max": pair.strike_max,
        "spot": pair.spot,
        "timestamps": pair.timestamps,
        "call_series_cr": pair.call_change_cr,
        "put_series_cr": pair.put_change_cr,
        "call_swings": [asdict(s) for s in result.call_swings],
        "put_swings": [asdict(s) for s in result.put_swings],
        "call_zone": asdict(result.call_zone) if result.call_zone else None,
        "put_zone": asdict(result.put_zone) if result.put_zone else None,
        "call_breakout": result.call_breakout.direction,
        "put_breakout": result.put_breakout.direction,
        "call_red_5m": result.call_red_5m,
        "put_red_5m": result.put_red_5m,
        "call_pts": result.call_pts,
        "put_pts": result.put_pts,
        "signal": result.signal,
        "confidence": result.confidence,
        "contributions": [asdict(c) for c in result.contributions],
        "payload": result.algo_payload(
            timestamp=evaluated_at,
            engine_1m_on=params.engine_1m_on,
            engine_5m_on=params.engine_5m_on,
        ),
        "params": params.model_dump(),
        "by_strike": by_strike,
        "as_of": as_of,
        "strike_step": strike_step,
    }
