"""Backtesting — the live orchestrator replayed over stored history.

The decision core (``ZoneOrchestrator``) is reused VERBATIM; only its deps are
swapped for in-memory providers over a prefetched per-day frame. Nothing here
may fork engine math — the shared seams are ``series.oi_change_pair_from_points``
/ ``ratio_pair_from_points`` and ``orchestrator.warmup_ump_engine``.
"""
from .runner import BacktestJob, active_run_id, get_job, start_run  # noqa: F401
