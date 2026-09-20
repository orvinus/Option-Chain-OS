"""NIFTY Ultra Master Pro — faithful Python port of Pine v24
(``nifty_oi_level_24.txt``, the master spec's authoritative source).

Split the way the Pine script splits:

- ``levels``  — the institutional level pipeline (1H structural reversal
  detection, the 13-value pivot matrix, bridge pool, weekly discovery
  staircase, downside + void-filling bridges, the 20% Expansion pass and
  recursive Median injection) — pure functions of the HTF feeds.
- ``engine``  — the entry/exit state machine (trigger classification SC1–SC3,
  Body Closing entries S1A..S3C, Retest R1/R2, the Q-ladder System A trail,
  the System B zone trail, and the exit priority chain P1→P2→P2b→P3→P4 with
  the ERROR-1..5 tick-ordering disciplines).

The engine consumes 1-minute premium bars and aggregates its own 5-minute
candles; levels rebuild only on confirmed 5m closes and FREEZE during a trade.
"""
from .engine import UmpEngine, UmpEvent, UmpFeeds, UmpTrade
from .levels import Candle, Level, LEVEL_TYPE_NAMES, build_levels, detect_h1_reversals

__all__ = [
    "Candle",
    "Level",
    "LEVEL_TYPE_NAMES",
    "build_levels",
    "detect_h1_reversals",
    "UmpEngine",
    "UmpEvent",
    "UmpFeeds",
    "UmpTrade",
]
