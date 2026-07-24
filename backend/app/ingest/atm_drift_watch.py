"""ATM drift detection — resubscribe when spot moves too far from subscription centre.

The XTS subscription is fixed at startup based on the ATM at that moment. If
the underlying moves significantly during the day, the subscribed strike range
no longer covers the real ATM, so the chart shows no data at ATM.

This watcher runs every 60 seconds, compares the current live spot with the
spot used for the LAST subscription, and triggers a full resubscription via
``switch_active_symbol`` when the ATM has drifted more than half a strike-window.
"""
from __future__ import annotations

import asyncio

from ..core.config import settings
from ..core.logging import get_logger
from ..runtime import get_runtime

log = get_logger("atm_drift_watch")

CHECK_INTERVAL_S = 60.0
# Resubscribe when spot shifts more than this fraction of the subscribed range.
DRIFT_THRESHOLD_FRACTION = 0.4


async def run_atm_drift_watch() -> None:
    """Background task: resubscribe when ATM drifts out of the subscribed window."""
    rt = get_runtime()
    last_sub_spot: float | None = None

    # Delay first check to allow the initial subscription to settle.
    await asyncio.sleep(CHECK_INTERVAL_S)

    while True:
        try:
            spot = rt.latest_spot
            if spot is None or spot <= 0:
                await asyncio.sleep(CHECK_INTERVAL_S)
                continue

            # Per-symbol strike step (registry), NOT the global settings.strike_step.
            # With the global 50, SENSEX (step 100) over-triggered and small-step
            # symbols (stocks 5, NATURALGAS 1) never re-centred — the window drifted
            # off and the chart emptied. Recomputed each iteration since the active
            # symbol can change between checks.
            from ..market.symbols import get_registry

            reg_entry = get_registry().get(rt.active_symbol)
            step = reg_entry.strike_step if reg_entry and reg_entry.strike_step > 0 else settings.strike_step
            window = settings.strike_window
            # Half-window in points: how far ATM can move before we resubscribe.
            threshold_pts = window * step * DRIFT_THRESHOLD_FRACTION

            if last_sub_spot is None:
                last_sub_spot = spot
                await asyncio.sleep(CHECK_INTERVAL_S)
                continue

            drift = abs(spot - last_sub_spot)
            if drift >= threshold_pts:
                log.info(
                    "atm_drift.resubscribing",
                    current_spot=round(spot, 2),
                    last_sub_spot=round(last_sub_spot, 2),
                    drift_pts=round(drift, 2),
                    threshold_pts=threshold_pts,
                )
                try:
                    # Import here to avoid a circular import at module load time.
                    from ..ingest.symbol_controller import switch_active_symbol
                    await switch_active_symbol(rt.active_symbol)
                    last_sub_spot = spot
                    log.info("atm_drift.resubscribed", new_spot=round(spot, 2))
                except Exception as e:
                    log.warning("atm_drift.resubscribe_error", error=str(e))
            else:
                log.debug(
                    "atm_drift.ok",
                    spot=round(spot, 2),
                    drift_pts=round(drift, 2),
                    threshold_pts=threshold_pts,
                )

        except asyncio.CancelledError:
            return
        except Exception as e:
            log.warning("atm_drift_watch.error", error=str(e))

        await asyncio.sleep(CHECK_INTERVAL_S)
