"""The Ratio (MQAE) features run on the zone's selected timeframe (2026-09-15).

`series.ratio_pair_for_timeframe` is the single conversion live trading, the
backtest, the dashboard endpoint and the live strip apply. These tests pin its
contract: 1m is the identity (old behaviour byte-identical), N-minute buckets
are session-anchored and CLOSED-only (no look-ahead), the session's final short
bucket counts once the day is over, and Full Day is the chart's cumulative
formula.
"""
from __future__ import annotations

import asyncio

from app.algo.config_models import MqaeParams, default_config
from app.algo.orchestrator import evaluate_indicator_from_pairs
from app.algo.series import RatioPair, ratio_pair_for_timeframe


def _ts(h: int, m: int, d: str = "2026-09-15") -> str:
    return f"{d}T{h:02d}:{m:02d}:00+05:30"


def _pair(minutes: list[tuple[int, int]], d: str = "2026-09-15") -> RatioPair:
    n = len(minutes)
    call = [1000 + 10 * i for i in range(n)]
    put = [2000 + 25 * i for i in range(n)]
    return RatioPair(
        timestamps=[_ts(h, m, d) for h, m in minutes],
        green_pcr=[p / c for c, p in zip(call, put)],
        yellow_ratio=[c / p for c, p in zip(call, put)],
        strike_min=22900, strike_max=23900, spot=23400.0,
        total_call_oi=call, total_put_oi=put,
    )


def _minutes(h0: int, m0: int, count: int) -> list[tuple[int, int]]:
    out = []
    t = h0 * 60 + m0
    for i in range(count):
        out.append(((t + i) // 60, (t + i) % 60))
    return out


def test_one_minute_is_the_identity() -> None:
    p = _pair(_minutes(9, 15, 20))
    assert ratio_pair_for_timeframe(p, "1m") is p
    assert ratio_pair_for_timeframe(p, None) is p


def test_five_minute_uses_the_value_at_each_closed_bucket_close() -> None:
    # 09:15 … 09:31 closed (17 minutes): buckets 09:15, 09:20, 09:25 are closed;
    # 09:30 holds only 09:30–09:31 and has NOT closed.
    p = _pair(_minutes(9, 15, 17))
    q = ratio_pair_for_timeframe(p, "5m")
    assert q.timestamps == [_ts(9, 15), _ts(9, 20), _ts(9, 25)], q.timestamps
    # value at bucket close = the bucket's last minute (09:19, 09:24, 09:29)
    assert q.green_pcr == [p.green_pcr[4], p.green_pcr[9], p.green_pcr[14]]
    assert q.yellow_ratio == [p.yellow_ratio[4], p.yellow_ratio[9], p.yellow_ratio[14]]
    assert q.total_call_oi == [p.total_call_oi[4], p.total_call_oi[9], p.total_call_oi[14]]


def test_forming_bucket_only_for_the_display_path() -> None:
    p = _pair(_minutes(9, 15, 17))
    q = ratio_pair_for_timeframe(p, "5m", include_forming=True)
    assert q.timestamps[-1] == _ts(9, 30) and q.green_pcr[-1] == p.green_pcr[16]


def test_a_bucket_closes_on_its_last_minute() -> None:
    # 09:15 … 09:19 = exactly one full 5-minute bucket.
    q = ratio_pair_for_timeframe(_pair(_minutes(9, 15, 5)), "5m")
    assert q is not None and q.timestamps == [_ts(9, 15)]
    assert ratio_pair_for_timeframe(_pair(_minutes(9, 15, 4)), "5m") is None


def test_final_short_bucket_counts_once_the_session_is_over() -> None:
    # A full post-2026-08-03 session: 09:15 … 15:39 (385 minutes, close 15:40).
    p = _pair(_minutes(9, 15, 385))
    q = ratio_pair_for_timeframe(p, "30m")
    assert len(q.timestamps) == 13, len(q.timestamps)          # 15:15–15:40 included
    assert q.timestamps[-1] == _ts(15, 15)
    assert q.green_pcr[-1] == p.green_pcr[-1]
    # Mid-session the same bucket is still forming and must be excluded.
    mid = _pair(_minutes(9, 15, 375))                          # data to 15:29
    assert ratio_pair_for_timeframe(mid, "30m").timestamps[-1] == _ts(14, 45)


def test_full_day_is_the_cumulative_change_ratio() -> None:
    p = _pair(_minutes(9, 15, 6))
    q = ratio_pair_for_timeframe(p, "full_day")
    # the first point has zero change on both sides → dropped
    assert q.timestamps == p.timestamps[1:]
    for i, t in enumerate(q.timestamps, start=1):
        dc = p.total_call_oi[i] - p.total_call_oi[0]
        dp = p.total_put_oi[i] - p.total_put_oi[0]
        assert q.green_pcr[i - 1] == dp / dc and q.yellow_ratio[i - 1] == dc / dp


def test_full_day_drops_a_point_where_either_side_has_no_change() -> None:
    p = _pair(_minutes(9, 15, 4))
    p.total_put_oi[2] = p.total_put_oi[0]          # no put change at 09:17
    q = ratio_pair_for_timeframe(p, "full_day")
    assert _ts(9, 17) not in q.timestamps and len(q.green_pcr) == len(q.yellow_ratio) == 2


def test_default_config_keeps_one_minute() -> None:
    assert MqaeParams().timeframe == "1m"
    cfg = default_config()
    assert all(z.mqae.timeframe == "1m" for d in cfg.days.values() for z in d.zones.values())


def test_live_and_backtest_evaluation_use_the_zone_timeframe() -> None:
    """`evaluate_indicator_from_pairs` is the path BOTH live trading and the
    backtest take — the engine must see the converted series."""
    cfg = default_config()
    zone = cfg.days["tuesday"].zones["Z3"].model_copy(deep=True)
    pair = _pair(_minutes(9, 15, 60))
    seen = {}

    async def ratio_pair(window):
        return pair

    async def oi_pair(window):
        return None

    import app.algo.engines.mqae as mqae_mod

    real = mqae_mod.evaluate

    def spy(green, yellow, params):
        seen["n"] = len(green)
        return real(green, yellow, params)

    mqae_mod.evaluate = spy
    try:
        for tf, expected in (("1m", 60), ("5m", 12), ("15m", 4), ("full_day", 59)):
            zone.mqae.timeframe = tf
            asyncio.run(evaluate_indicator_from_pairs("ratio", zone, oi_pair, ratio_pair))
            assert seen["n"] == expected, (tf, seen["n"])
        # 30m: only 2 closed buckets in the first hour → still evaluates
        zone.mqae.timeframe = "30m"
        asyncio.run(evaluate_indicator_from_pairs("ratio", zone, oi_pair, ratio_pair))
        assert seen["n"] == 2
    finally:
        mqae_mod.evaluate = real
