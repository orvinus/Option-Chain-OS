"""The Algo Config live stream producer.

Computes one frame per WATCHED scope and hands it to ``ws/algo_hub`` to fan
out. Three deliberate shape decisions:

**Clock-driven, not flush-driven.** Nothing here registers on
``feed_factory._make_on_flush``. That hook writes ``rt.last_ws_flush_at``, the
session steward's dead-socket detector, and the comment at
``feed_factory.py:332-338`` records what happened when publishing was awaited
there: the ingest cadence became hostage to how many dashboard tabs were open.
A clock loop also keeps working under ``RUN_MODE=replay`` and off-hours, where
no flush ever fires — which is what "build once, works in either" requires.
The orchestrator loop already sets this precedent (``main.py:188-192``).

**Two cadences, because the data has two cadences.** Premium/LTP genuinely
moves every second (``PERSIST_BUCKET=1s``), so price and the forming candle
tick at ``ALGO_STREAM_TICK_MS``. Open interest is disseminated by the exchange
roughly ONCE PER MINUTE — recomputing the OI-derived engines per second would
burn union-view queries to redraw an identical number, so they refresh on
``ALGO_STREAM_OI_REFRESH_S``. That is not a compromise; it is the honest
sampling rate for that input.

**Both bases in every frame.** The user asked for intrabar signals. Intrabar
readings are computed from a series that INCLUDES the forming bucket, while
the orchestrator keeps trading on closed candles only. Rather than let those
diverge silently, every frame carries ``intrabar`` and ``closed`` side by side
plus a ``diverged`` flag, so the UI cannot render the headline without the
traded basis next to it.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import structlog

from ..core.config import settings
from ..core.time_utils import IST, is_nse_regular_session_open, now_ist
from ..runtime import get_runtime
from ..ws.algo_hub import AlgoScope, get_algo_hub
from .combine import combine_readings

log = structlog.get_logger(__name__)

# Matches the orchestrator's own entry-staleness threshold
# (orchestrator._ENTRY_STALE_AFTER_S) on purpose: the badge the operator reads
# and the gate that actually blocks entries must never disagree.
_STALE_AFTER_S = 180.0


# ══════════════════════════════════════════════════════════════════════════
# Per-scope cache
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class _ScopeCache:
    """What we keep between ticks for one watched scope, so the per-second
    path costs one small live-table query instead of four union-view ones."""
    oi_refreshed_at: float = 0.0
    intrabar: dict[str, Any] = field(default_factory=dict)
    closed: dict[str, Any] = field(default_factory=dict)
    closed_as_of: Optional[str] = None
    strike: Optional[int] = None          # resolved band pick, cached per minute
    strike_source: str = "none"
    # Set when the live window was empty and we fell back to the chain's last
    # stored moment (replay / post-close / holiday). Drives the frozen bar.
    anchor_utc: Optional[datetime] = None
    # The frozen contract's own last print, resolved once per anchor change.
    bar_anchor: Optional[datetime] = None
    bar_anchor_at: Optional[datetime] = None
    strike_at_minute: Optional[datetime] = None
    seq: int = 0
    # The OI refresh runs in its own task so a slow union-view query can never
    # stall the 1 s price/candle lane. One in flight at a time per scope.
    oi_task: Optional["asyncio.Task"] = None


_cache: dict[str, _ScopeCache] = {}


def _cache_for(scope: AlgoScope) -> _ScopeCache:
    c = _cache.get(scope.key)
    if c is None:
        c = _ScopeCache()
        _cache[scope.key] = c
    return c


# ══════════════════════════════════════════════════════════════════════════
# Feed mode — the honest-degradation verdict
# ══════════════════════════════════════════════════════════════════════════

async def _feed_mode(symbol: str) -> tuple[str, bool, Optional[float]]:
    """(mode, live, option_row_age_s).

    ``mode`` is one of: replay · session_closed · other_symbol · no_data ·
    stale · live. Every one of these is a state the page must SAY, not a state
    it should hide behind a spinner or, worse, keep animating over.
    """
    from .series import freshest_option_row_age_s

    if settings.run_mode != "live":
        return "replay", False, None

    rt = get_runtime()
    try:
        age = await freshest_option_row_age_s(symbol)
    except Exception as e:
        log.warning("algo_stream.age_probe_failed", error=str(e))
        age = None

    if not is_nse_regular_session_open():
        return "session_closed", False, age
    if age is None:
        # No row in the last 30 minutes for THIS symbol. Distinguish "the feed
        # is following something else" from "nothing is arriving at all" — the
        # OI hub silently drops this case (hub.py:68), which is exactly the
        # confusion this page must not inherit.
        if symbol != rt.active_symbol:
            return "other_symbol", False, None
        return "no_data", False, None
    if age > _STALE_AFTER_S:
        return "stale", False, age
    return "live", True, age


# ══════════════════════════════════════════════════════════════════════════
# Frame computation
# ══════════════════════════════════════════════════════════════════════════

def _bucket_starts(now_utc: datetime, tf_min: int = 5) -> tuple[datetime, datetime]:
    """(current 1-minute start, current exchange-aligned entry-candle start).
    ``tf_min`` is the zone's UMP entry timeframe (5 or 15; both align on the
    wall clock). The frame keeps the historical ``b5`` key name for the
    entry-timeframe bar and reports ``tf_min`` beside it."""
    m1 = now_utc.replace(second=0, microsecond=0)
    ist = now_utc.astimezone(IST)
    b5_ist = ist.replace(minute=(ist.minute // tf_min) * tf_min, second=0, microsecond=0)
    return m1, b5_ist.astimezone(timezone.utc)


async def _resolve_strike(scope: AlgoScope, cache: _ScopeCache, zone_cfg) -> None:
    """Cache the zone's band pick for a minute — it is a 15-minute-window
    query and the answer cannot meaningfully change faster than that."""
    m1, _ = _bucket_starts(datetime.now(timezone.utc))
    if scope.strike is not None:
        cache.strike, cache.strike_source = scope.strike, "manual"
        return
    if cache.strike_at_minute == m1 and cache.strike is not None:
        return
    from .series import band_candidates_at, freshest_row_ts, select_strikes_in_band

    try:
        picked = await select_strikes_in_band(
            scope.symbol, scope.expiry, scope.option_type,
            zone_cfg.premium_min, zone_cfg.premium_max, 1,
        )
    except Exception as e:
        log.warning("algo_stream.band_pick_failed", error=str(e))
        picked = []

    cache.strike_at_minute = m1
    if picked:
        cache.strike, cache.strike_source = picked[0][0], "band"
        cache.anchor_utc = None
        return

    # Nothing priced in the live 15-minute window — replay, a holiday, or
    # simply after the close. Re-anchor to the chain's LAST stored moment and
    # pick the band there, so the page shows the last real session frozen
    # instead of an empty card. Same fallback `ump_eval` already performs
    # (api/algo_engines.py), kept consistent on purpose.
    try:
        last_ts = await freshest_row_ts(scope.symbol, scope.expiry)
        if last_ts is not None:
            cands = await band_candidates_at(
                scope.symbol, scope.expiry, scope.option_type,
                zone_cfg.premium_min, zone_cfg.premium_max, 1, last_ts,
            )
            cache.anchor_utc = last_ts
            if cands:
                cache.strike, cache.strike_source = cands[0][0], "band@last"
                return
    except Exception as e:
        log.warning("algo_stream.band_reanchor_failed", error=str(e))
    if cache.strike is None:
        cache.strike_source = "none"


def _combined(zone_cfg, sink: dict[str, Any]) -> str:
    """§4.1 + Signal Console F1, applied to whichever basis this sink holds.

    Delegates to the SAME function the trading orchestrator uses. It used to
    re-implement unanimity here; once the rule became configurable a second
    copy would have shown a signal on screen that the engine did not act on."""
    readings = {
        k: (sink.get(k) or {}).get("signal") or (sink.get(k) or {}).get("reading") or "NO_TRADE"
        for k in zone_cfg.enabled_indicators
    }
    decision = combine_readings(
        zone_cfg.enabled_indicators,
        readings,
        rule=zone_cfg.combine_rule,
        neutral_mode=zone_cfg.neutral_mode,
    )
    # The reason travels with the signal: the strip has to be able to say WHY
    # it is showing what it is showing, under whichever rule is configured.
    sink["combined_reason"] = decision.reason
    return decision.direction or "NO_TRADE"


async def _refresh_oi_readings(scope: AlgoScope, cache: _ScopeCache, zone_cfg) -> None:
    """Recompute the three entry-filter readings on BOTH bases.

    COST DISCIPLINE — the reason this function is shaped the way it is:
    ``fetch_oi_timeseries`` reads ``oi_snapshots_unified``, the 33M-row union
    whose unbounded scans exhausted the DB pool on 2026-08-11. The naive
    version of this (call ``build_oi_change_pair`` once per indicator per
    basis) issues SIX of those queries and was measured at **22.7 s per
    tick**. So:

      * fetch ONCE per DISTINCT ``strikes_atm_window`` — the three indicators
        usually share one or two windows, not three;
      * run the pure transforms twice over the SAME fetched points, which is
        what makes carrying the traded basis alongside the preview nearly free.

    Never call this from the per-second path; it runs in its own task.
    """
    from . import series as ser
    from .engines import mqae, mtf_ratio, oi_structure
    from ..services.oi_timeseries import fetch_oi_timeseries

    now_utc = datetime.now(timezone.utc)
    windows = {
        zone_cfg.oi_structure.strikes_atm_window,
        zone_cfg.mtf_ratio.strikes_atm_window,
        zone_cfg.mqae.strikes_atm_window,
    }

    # window -> {"intra": (oi_pair, ratio_pair), "closed": (...)}
    built: dict[int, dict[str, Any]] = {}
    for w in windows:
        basket = await ser._resolve_strike_basket(scope.symbol, scope.expiry, w, None)
        if basket is None:
            continue
        smin, smax, spot = basket
        ts_resp = await fetch_oi_timeseries(
            scope.symbol, scope.expiry, smin, smax, bucket="1m"
        )
        if not ts_resp.points:
            continue
        built[w] = {}
        for name, inc in (("intra", True), ("closed", False)):
            built[w][name] = (
                ser.oi_change_pair_from_points(
                    ts_resp.points, now_utc=now_utc, strike_min=smin,
                    strike_max=smax, spot=spot, include_forming=inc,
                ),
                ser.ratio_pair_from_points(
                    ts_resp.points, now_utc=now_utc, strike_min=smin,
                    strike_max=smax, spot=spot, include_forming=inc,
                ),
            )

    out: dict[str, dict[str, Any]] = {"intra": {}, "closed": {}}
    for name in ("intra", "closed"):
        sink = out[name]

        pair = (built.get(zone_cfg.oi_structure.strikes_atm_window) or {}).get(name, (None, None))[0]
        try:
            if pair is not None and len(pair.call_change_cr) >= 2:
                r = oi_structure.evaluate(
                    pair.call_change_cr, pair.put_change_cr, zone_cfg.oi_structure
                )
                sink["oi_change"] = {"signal": r.signal,
                                     "confidence": getattr(r, "confidence", None)}
                sink["_tail"] = {"ts": pair.timestamps[-1],
                                 "call_cr": pair.call_change_cr[-1],
                                 "put_cr": pair.put_change_cr[-1]}
            else:
                sink["oi_change"] = {"signal": "NO_TRADE", "confidence": None}
        except Exception as e:
            log.warning("algo_stream.oi_structure_failed", error=str(e))
            sink["oi_change"] = {"signal": "NO_TRADE", "confidence": None}

        pair = (built.get(zone_cfg.mtf_ratio.strikes_atm_window) or {}).get(name, (None, None))[0]
        try:
            if pair is not None and len(pair.call_change_cr) >= 2:
                from .series import mtf_input

                rows = mtf_ratio.rows_from_cumulative_series(
                    *mtf_input(pair), zone_cfg.mtf_ratio.timeframes
                )
                sink["multi_tf"] = {
                    "reading": mtf_ratio.evaluate(rows, zone_cfg.mtf_ratio).reading
                }
            else:
                sink["multi_tf"] = {"reading": "NO_TRADE"}
        except Exception as e:
            log.warning("algo_stream.mtf_failed", error=str(e))
            sink["multi_tf"] = {"reading": "NO_TRADE"}

        rp = (built.get(zone_cfg.mqae.strikes_atm_window) or {}).get(name, (None, None))[1]
        try:
            # Same timeframe conversion the engine applies; the intrabar basis
            # keeps the forming bucket (display only), the closed basis never.
            rp = ser.ratio_pair_for_timeframe(
                rp, zone_cfg.mqae.timeframe, include_forming=(name == "intra")
            )
            if rp is not None and len(rp.green_pcr) >= 2:
                sink["ratio"] = {
                    "signal": mqae.evaluate(rp.green_pcr, rp.yellow_ratio, zone_cfg.mqae).signal
                }
            else:
                sink["ratio"] = {"signal": "NO_TRADE"}
        except Exception as e:
            log.warning("algo_stream.mqae_failed", error=str(e))
            sink["ratio"] = {"signal": "NO_TRADE"}

        sink["combined"] = _combined(zone_cfg, sink)

    cache.intrabar = out["intra"]
    cache.closed = out["closed"]
    cache.closed_as_of = (out["closed"].get("_tail") or {}).get("ts")
    cache.oi_refreshed_at = asyncio.get_running_loop().time()


async def compute_frame(scope: AlgoScope) -> dict:
    """Build one complete frame for a scope. Used by the producer loop AND by
    the WS route's immediate first frame, so a fresh tab paints at once."""
    from .config_store import get_config_store
    from .series import forming_premium_bar

    cache = _cache_for(scope)
    cache.seq += 1
    now_utc = datetime.now(timezone.utc)
    mode, live, age_s = await _feed_mode(scope.symbol)

    frame: dict[str, Any] = {
        "type": "algo",
        "seq": cache.seq,
        "ts": now_ist().isoformat(),
        "scope": scope.as_dict(),
        "live": live,
        "source": mode,
        "option_row_age_s": round(age_s, 1) if age_s is not None else None,
        "run_mode": settings.run_mode,
        "nse_session_open": is_nse_regular_session_open(),
    }

    try:
        cv = await get_config_store().get_live()
        zone_cfg = cv.config.zone(scope.day, scope.zone)  # type: ignore[arg-type]
        frame["config_version"] = cv.version
    except Exception as e:
        log.warning("algo_stream.config_failed", error=str(e))
        zone_cfg = None
    if zone_cfg is None:
        frame["error"] = f"no configuration for {scope.day} {scope.zone}"
        return frame

    # ---- strike + forming premium bar (the per-second path) ----
    await _resolve_strike(scope, cache, zone_cfg)
    frame["strike"] = cache.strike
    frame["strike_source"] = cache.strike_source

    if cache.strike is not None:
        # Live: the bar forming right now. Frozen (replay / post-close): the
        # LAST bar the chain actually printed, so the chart shows the real
        # close instead of an empty window.
        ref = now_utc
        if cache.anchor_utc is not None:
            # Anchor to THIS contract's own last print, not the chain's — a
            # deep-OTM strike stops printing well before the chain does, and
            # anchoring to the chain's max gives an empty window.
            if cache.bar_anchor_at != cache.anchor_utc:
                from .series import last_contract_ts

                try:
                    cache.bar_anchor = await last_contract_ts(
                        scope.symbol, scope.expiry, cache.strike, scope.option_type
                    )
                except Exception as e:
                    log.warning("algo_stream.last_contract_ts_failed", error=str(e))
                    cache.bar_anchor = None
                cache.bar_anchor_at = cache.anchor_utc
            ref = cache.bar_anchor or cache.anchor_utc
        tf_min = int(getattr(zone_cfg.ump.entry, "entry_timeframe_min", 5) or 5)
        m1_start, b5_start = _bucket_starts(ref, tf_min)
        frame["frozen_at"] = (
            ref.astimezone(IST).isoformat() if cache.anchor_utc is not None else None
        )
        try:
            bar = await forming_premium_bar(
                scope.symbol, scope.expiry, cache.strike, scope.option_type,
                m1_start=m1_start, b5_start=b5_start,
            )
        except Exception as e:
            log.warning("algo_stream.forming_bar_failed", error=str(e))
            bar = None
        if bar is not None:
            frame["price"] = {"ltp": bar["b5"]["c"] if bar["b5"] else None}
            frame["candle"] = {
                "m1": bar["m1"],
                "b5": bar["b5"],
                "m1_start": m1_start.astimezone(IST).isoformat(),
                "b5_start": b5_start.astimezone(IST).isoformat(),
                # The zone's entry timeframe the b5 bar is bucketed on (5|15).
                "tf_min": tf_min,
                "forming": True,
                "as_of": bar["as_of"].astimezone(IST).isoformat() if bar["as_of"] else None,
            }
        else:
            frame["price"] = {"ltp": None}
            frame["candle"] = None

    # ---- OI-derived readings (the slow lane, NEVER awaited here) ----
    # Kicked off as a task and left to land whenever it lands. Awaiting it
    # inline is what made a single frame take 22.7 s. The frame ships with
    # whatever the cache holds; `closed_as_of` tells the UI how old that is.
    loop_now = asyncio.get_running_loop().time()
    due = loop_now - cache.oi_refreshed_at >= settings.algo_stream_oi_refresh_s
    in_flight = cache.oi_task is not None and not cache.oi_task.done()
    if due and not in_flight:
        # Claim the slot BEFORE awaiting anything, so two ticks cannot both
        # start a refresh for the same scope.
        cache.oi_refreshed_at = loop_now
        cache.oi_task = asyncio.create_task(
            _refresh_oi_readings(scope, cache, zone_cfg)
        )

    # Which indicators this scope's zone actually trades on — the strip must
    # not print readings of filters the zone has switched off.
    frame["enabled_indicators"] = list(zone_cfg.enabled_indicators)
    # ...and under which rule they are being combined, so the strip can never
    # imply unanimity while the zone is actually running majority.
    frame["combine_rule"] = zone_cfg.combine_rule
    frame["neutral_mode"] = zone_cfg.neutral_mode

    intra = {k: v for k, v in cache.intrabar.items() if not k.startswith("_")}
    closed = {k: v for k, v in cache.closed.items() if not k.startswith("_")}
    frame["readings"] = {
        "intrabar": intra,
        "closed": closed,
        "closed_as_of": cache.closed_as_of,
        # The honesty flag: the UI turns the INTRABAR chip amber on this.
        "diverged": bool(intra) and bool(closed)
        and intra.get("combined") != closed.get("combined"),
        "tail": cache.intrabar.get("_tail"),
    }

    # ---- orchestrator status, enriched with the live position price ----
    frame["status"] = await _status_block(frame.get("price", {}).get("ltp"))
    return frame


async def _status_block(live_ltp: Optional[float]) -> dict:
    """The orchestrator's status, plus the two numbers it structurally cannot
    carry: the position's current price and its unrealized P&L.

    Computed HERE rather than in ``_fill_position_status`` on purpose — that
    runs inside the decision pass, which already burns ~14-15 s of its ~57 s
    minute, and it must not grow a DB read.
    """
    from .orchestrator import get_orchestrator

    orch = get_orchestrator()
    if orch is None:
        return {"running": False}
    s = orch.status
    out: dict[str, Any] = {
        "running": True,
        "state": s.state,
        "active_zone": s.active_zone,
        "direction": s.direction,
        "readings": s.readings,
        "gate_blocks": s.gate_blocks,
        "paused_reason": s.paused_reason,
        "realized_pnl_today": s.realized_pnl_today,
        "trades_today": s.trades_today,
        "last_evaluated": s.last_evaluated,
        "hunting_strikes": s.hunting_strikes,
        "position": s.position,
    }
    # Project the in-memory hunt set — strike, side and each engine's live
    # state. None of this is exposed anywhere today; `hunting_strikes` is a
    # bare count, which cannot answer "what is it watching, and why hasn't it
    # fired?". Read-only attribute access, no locks, no mutation.
    try:
        out["hunts"] = [
            {
                "strike": h.contract.strike,
                "option_type": h.contract.option_type,
                "zone": h.zone_id,
                "side": h.side,
                "started": h.started.isoformat() if h.started else None,
                "engine_state": h.engine.state,
                "sub_scenario": h.engine.sub,
                "last_close": h.engine.last_close,
                "trail_sl": h.engine.trail_sl,
                "max_sl": h.engine.max_sl,
            }
            for h in orch.hunts
        ]
    except Exception as e:
        log.warning("algo_stream.hunts_projection_failed", error=str(e))
        out["hunts"] = []

    pos = s.position
    if pos:
        # The position's price comes from ITS OWN contract's newest tick. It
        # used to be ``live_ltp`` — the price of whatever strike the strip is
        # scoped to — so a 23600CE bought at ₹1.15 was marked against the
        # strip's 23150CE at ₹84.70: a phantom +₹70,000 "unrealized" (found in
        # the 2026-09-15 live burn-in). ``live_ltp`` is kept as a fallback only
        # when the strip IS on the position's contract.
        try:
            from datetime import date as _date

            from ..market.symbols import get_registry
            from .series import latest_contract_quote

            symbol = str(pos.get("contract", "")).split(":")[0]
            lot = int(pos.get("lot_size") or 0)
            if lot <= 0:
                entry = get_registry().get(symbol)
                lot = entry.lot_size if entry is not None else 0
            lots = int(pos.get("lots") or 0)
            entry_px = float(pos.get("entry") or 0.0)
            price: Optional[float] = None
            price_ts: Optional[datetime] = None
            strike, ot, exp = pos.get("strike"), pos.get("option_type"), pos.get("expiry")
            if symbol and strike is not None and ot and exp:
                q = await latest_contract_quote(symbol, _date.fromisoformat(str(exp)), int(strike), str(ot))
                if q is not None:
                    price, price_ts = q
            if price is None and pos.get("last_close") is not None:
                price = float(pos["last_close"])
            if price is not None and lot > 0 and lots > 0 and entry_px > 0:
                pts = price - entry_px
                out["position"] = {
                    **pos,
                    "current_price": price,
                    "price_ts": price_ts.astimezone(IST).isoformat() if price_ts else None,
                    "price_age_s": (
                        round((datetime.now(timezone.utc) - price_ts).total_seconds(), 1)
                        if price_ts else None
                    ),
                    "unrealized_points": round(pts * 100) / 100,
                    "unrealized_rupees": round(pts * lot * lots * 100) / 100,
                    "unrealized_pct": round(pts / entry_px * 10000) / 100,
                }
        except Exception as e:
            log.warning("algo_stream.unrealized_failed", error=str(e))
    return out


# ══════════════════════════════════════════════════════════════════════════
# The supervised producer loop
# ══════════════════════════════════════════════════════════════════════════

async def run_algo_stream_loop() -> None:
    """One pass per tick over the DISTINCT watched scopes.

    Zero subscribers → zero work and zero queries. The loop exists whether or
    not the feed does, so the page behaves identically (and honestly) under
    ``RUN_MODE=replay``.
    """
    if not settings.algo_stream_enabled:
        log.info("algo_stream.disabled")
        return

    hub = get_algo_hub()
    tick_s = max(0.25, settings.algo_stream_tick_ms / 1000.0)
    log.info("algo_stream.started", tick_s=tick_s,
             oi_refresh_s=settings.algo_stream_oi_refresh_s)

    while True:
        try:
            scopes = await hub.live_scopes()
            if scopes:
                t0 = asyncio.get_running_loop().time()
                for scope in scopes:
                    try:
                        frame = await compute_frame(scope)
                        await hub.publish(scope, frame)
                    except Exception as e:
                        log.warning(
                            "algo_stream.scope_failed", scope=scope.key, error=str(e)
                        )
                took_ms = (asyncio.get_running_loop().time() - t0) * 1000
                if took_ms > 500:
                    # Louder than a debug line: a slow tick means the page is
                    # competing with the decision pass for the 30-slot pool.
                    log.warning(
                        "algo_stream.slow_tick",
                        ms=round(took_ms),
                        scopes=len(scopes),
                        subs=hub.subscriber_count,
                    )
            # Prune caches for scopes nobody watches any more.
            if len(_cache) > 32:
                keep = {s.key for s in scopes}
                for k in [k for k in _cache if k not in keep]:
                    _cache.pop(k, None)
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("algo_stream.tick_failed")
        try:
            await asyncio.sleep(tick_s)
        except asyncio.CancelledError:
            return
