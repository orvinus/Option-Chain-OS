from fastapi import APIRouter

from . import (
    auth,
    expiries,
    health,
    history,
    interpretation,
    iv_scanner,
    multi_timeframe,
    oi_change,
    oi_timeseries,
    option_chain,
    ratio_timeseries,
    replay,
    spot,
    symbols,
    verify_nifty,
    verify_option_chain,
)

api_router = APIRouter(prefix="/api")
api_router.include_router(auth.router)
api_router.include_router(health.router)
api_router.include_router(spot.router)
api_router.include_router(expiries.router)
api_router.include_router(oi_change.router)
api_router.include_router(oi_timeseries.router)
api_router.include_router(ratio_timeseries.router)
api_router.include_router(multi_timeframe.router)
api_router.include_router(history.router)
api_router.include_router(option_chain.router)
api_router.include_router(iv_scanner.router)
api_router.include_router(replay.router)
api_router.include_router(interpretation.router)
api_router.include_router(symbols.router)
api_router.include_router(verify_nifty.router)
api_router.include_router(verify_option_chain.router)

__all__ = ["api_router"]
