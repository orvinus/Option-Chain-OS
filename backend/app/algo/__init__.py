"""Algo Config — the semi-automated trading engine.

Two hard-separated layers (nifty_algo_system_spec v2):

- Entry Filter Layer: three independent indicators (OI Change = OI Structure
  Engine, Multi-TF = MTF Ratio rule engine, Ratio = Master Quantitative Action
  Engine), each outputting strictly CALL or PUT, combined by the automatic
  unanimous-among-enabled rule.
- Execution engine: NIFTY Ultra Master Pro, a faithful port of Pine v24
  (``nifty_oi_level_24.txt``), which performs zero computation for a zone until
  the filter layer resolves a confirmed direction, then owns entry hunting and
  every exit exclusively.

This package holds the configuration document + store, admin auth, the audit
log, the engines, and (from M5) the zone orchestrator and paper simulator.
"""
