"""Entry-filter engines + the execution engine.

Each entry-filter engine is a PURE function of (input series, parameters) →
reading + explainable payload. No I/O, no clocks, no globals — the series
builders feed them and the orchestrator schedules them, which is what makes
golden-vector testing against the reference implementations possible.
"""
