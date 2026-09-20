"""Listed-but-idle days: the NSE settlement bars that give a young contract a
daily and weekly history, and the seed that hands them to the UMP engine.

The reference case throughout is NIFTY 23450 CE expiring 2026-09-15, which was
listed on 2026-08-12 and did not trade until 2026-09-03. TradingView's weekly
chart for it shows four candles (w/c 17, 24, 31 Aug and 7 Sep) while its 4-hour
chart starts on 3 Sep; the numbers below are that contract's real settlement
prices, and the expected weekly candle is the one read off the user's chart.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from app.algo.engines.ump.engine import HtfSeed, UmpEngine
from app.algo.series import (
    PremiumMinute,
    aggregate_minutes,
    daily_series_minutes,
    htf_seed_from_official,
    official_ohlc_map,
    settlement_only_dates,
)
from app.algo.config_models import UmpParams
from app.market_data.nse_settlement import SOURCE, parse_udiff

IST = timezone(timedelta(hours=5, minutes=30))

# (H, L, C, O) exactly as fetch_official_closes_batch returns them. Idle days
# are flat bars at the settlement price; 3-4 Sep are the first traded days.
CONTRACT: dict[date, tuple] = {
    date(2026, 8, 31): (768.57, 768.57, 768.57, 768.57),
    date(2026, 9, 1): (737.73, 737.73, 737.73, 737.73),
    date(2026, 9, 2): (614.34, 614.34, 614.34, 614.34),
    date(2026, 9, 3): (580.90, 580.90, 580.90, 580.90),
    date(2026, 9, 4): (593.05, 556.00, 566.25, 586.70),
}
# The week before the running one, w/c 24 Aug.
WEEK_PRIOR: dict[date, tuple] = {
    date(2026, 8, 24): (950.57,) * 4,
    date(2026, 8, 25): (1038.28,) * 4,
    date(2026, 8, 26): (922.58,) * 4,
    date(2026, 8, 27): (817.65,) * 4,
    date(2026, 8, 28): (876.65,) * 4,
}


def _minute(d: date, hh: int, mm: int, o, h, l, c) -> PremiumMinute:
    return PremiumMinute(
        ts=datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST).astimezone(timezone.utc),
        o=o, h=h, l=l, c=c,
    )


# --------------------------------------------------------------- UDiFF parse

_HEADER = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,"
    "FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,"
    "ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,"
    "ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,"
    "Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4"
)
# An idle day: no trades, O/H/L are 0.00 and ClsPric is FROZEN at the listing
# price while SttlmPric carries the only meaningful number.
_IDLE = (
    "2026-08-19,2026-08-19,FO,NSE,IDO,47295,,NIFTY,,2026-09-15,2026-09-15,"
    "23450.00,CE,NIFTY2691523450CE,0.00,0.00,0.00,1271.65,0.00,1271.65,"
    "24078.30,885.85,1500,0,0,0.00,0,F1,65,,,,,"
)
_TRADED = (
    "2026-09-04,2026-09-04,FO,NSE,IDO,47295,,NIFTY,,2026-09-15,2026-09-15,"
    "23450.00,CE,NIFTY2691523450CE,586.70,593.05,556.00,566.25,566.25,580.90,"
    "24010.00,566.25,2100,600,16,9.00,12,F1,65,,,,,"
)
_STOCK = _TRADED.replace("IDO", "STO").replace("NIFTY,", "RELIANCE,", 1)


def test_idle_day_becomes_a_flat_settlement_bar():
    [bar] = parse_udiff(_HEADER + "\n" + _IDLE, "NIFTY")
    assert bar.traded is False
    # SttlmPric, never the stale ClsPric of 1271.65.
    assert (bar.open, bar.high, bar.low, bar.close) == (885.85,) * 4
    assert bar.trade_date == date(2026, 8, 19)
    assert bar.expiry == date(2026, 9, 15)
    assert bar.strike == 23450 and bar.option_type == "CE"
    assert bar.volume == 0
    assert bar.token.startswith("nse:stl:")


def test_traded_day_keeps_its_real_range_and_share_volume():
    [bar] = parse_udiff(_HEADER + "\n" + _TRADED, "NIFTY")
    assert bar.traded is True
    assert (bar.open, bar.high, bar.low, bar.close) == (586.70, 593.05, 556.00, 566.25)
    # 16 contracts x 65 per lot. The TrueData bhavcopy counts shares, so this
    # must too or the two sources disagree on days both cover -- and they do
    # agree: the stored bhavcopy row for this contract-day also reads 1040.
    assert bar.volume == 1040
    assert bar.oi == 2100


def test_parse_keeps_only_this_underlying_and_index_derivatives():
    rows = parse_udiff(_HEADER + "\n" + _IDLE + "\n" + _TRADED + "\n" + _STOCK, "NIFTY")
    assert len(rows) == 2, "stock derivatives and other underlyings are dropped"
    assert {r.trade_date for r in rows} == {date(2026, 8, 19), date(2026, 9, 4)}


def test_settlement_source_tag_is_distinct_from_the_bhavcopy():
    assert SOURCE == "nse_settlement"


# ----------------------------------------------------------------- seed math


def test_seed_takes_the_three_newest_prior_sessions_newest_first():
    seed = htf_seed_from_official(CONTRACT, before=date(2026, 9, 3))
    assert seed.daily == [
        (614.34, 614.34, 614.34),     # 2 Sep — Pine's d1
        (737.73, 737.73, 737.73),     # 1 Sep — d2
        (768.57, 768.57, 768.57),     # 31 Aug — d3
    ]


def test_seed_never_reads_the_day_it_is_anchored_on_or_later():
    seed = htf_seed_from_official(CONTRACT, before=date(2026, 9, 3))
    closes = [c for _h, _l, c in seed.daily]
    assert 580.90 not in closes, "3 Sep is the running day — reading it is look-ahead"
    assert 566.25 not in closes, "4 Sep is the future outright"
    assert seed.week_c != 566.25


def test_seed_weekly_is_the_last_COMPLETED_iso_week():
    seed = htf_seed_from_official({**WEEK_PRIOR, **CONTRACT}, before=date(2026, 9, 3))
    # w/c 24 Aug: high 1038.28 (25th), low 817.65 (27th), close 876.65 (28th).
    assert seed.weekly == (1038.28, 817.65, 876.65)


def test_seed_carries_the_running_week_so_it_closes_with_its_true_high():
    seed = htf_seed_from_official(CONTRACT, before=date(2026, 9, 3))
    assert seed.week_iso == date(2026, 9, 3).isocalendar()[:2]
    assert seed.week_days == 3                       # 31 Aug, 1 Sep, 2 Sep
    assert seed.week_h == 768.57                     # lives in the Monday idle bar
    assert seed.week_l == 614.34
    assert seed.week_c == 614.34


def test_seed_skips_gaps_rather_than_walking_the_calendar():
    sparse = {
        date(2026, 8, 26): (10.0, 10.0, 10.0, 10.0),
        date(2026, 8, 31): (20.0, 20.0, 20.0, 20.0),   # 27/28 Aug absent
        date(2026, 9, 2): (30.0, 30.0, 30.0, 30.0),    # 1 Sep absent
    }
    seed = htf_seed_from_official(sparse, before=date(2026, 9, 3))
    assert [c for _h, _l, c in seed.daily] == [30.0, 20.0, 10.0]
    assert seed.week_days == 2, "only the dates that exist count toward the week"


def test_seed_is_empty_without_official_data():
    assert htf_seed_from_official(None, before=date(2026, 9, 3)).empty
    assert htf_seed_from_official({}, before=date(2026, 9, 3)).empty
    # Nothing before the anchor is nothing to seed.
    assert htf_seed_from_official(CONTRACT, before=date(2026, 8, 31)).empty


def test_seed_tolerates_the_legacy_three_tuple_and_bare_close_forms():
    legacy = {
        date(2026, 9, 1): (100.0, 90.0, 95.0),    # (H, L, C), no open
        date(2026, 9, 2): 80.0,                   # bare close
    }
    seed = htf_seed_from_official(legacy, before=date(2026, 9, 3))
    assert seed.daily == [(80.0, 80.0, 80.0), (100.0, 90.0, 95.0)]


def test_official_ohlc_map_fills_missing_members_from_the_close():
    got = official_ohlc_map({date(2026, 9, 1): (None, None, 42.0, None)})
    assert got[date(2026, 9, 1)] == (42.0, 42.0, 42.0, 42.0)


# ------------------------------------------------------------- engine wiring


def test_seed_htf_installs_feeds_and_keeps_pines_cap_of_three():
    eng = UmpEngine(UmpParams())
    eng.seed_htf(htf_seed_from_official({**WEEK_PRIOR, **CONTRACT}, date(2026, 9, 3)))
    assert len(eng.feeds.daily) == 3
    assert eng.feeds.weekly == (1038.28, 817.65, 876.65)
    assert eng.feeds.daily[0] == (614.34, 614.34, 614.34)


def test_seeding_opens_the_data_ready_gate_on_the_first_traded_day():
    """The whole point: without the seed a contract's first week is dead."""
    seed = htf_seed_from_official({**WEEK_PRIOR, **CONTRACT}, date(2026, 9, 3))

    blind = UmpEngine(UmpParams())
    seeded = UmpEngine(UmpParams())
    seeded.seed_htf(seed)

    # One 09:15-10:15 hour on 3 Sep, then the first bar of the next hour so the
    # 1H candle completes — the last gate input.
    for eng in (blind, seeded):
        for i in range(61):
            ts = datetime(2026, 9, 3, 9, 15) + timedelta(minutes=i)
            px = 580.0 + (i % 5)
            eng.process_minute(ts, px, px + 1, px - 1, px)

    assert seeded.feeds.loaded, "3 dailies + a completed week + 1H are all present"
    assert not blind.feeds.loaded, "unseeded, the gate is still waiting on D/W"


def test_seed_htf_is_a_noop_when_there_is_nothing_to_seed():
    eng = UmpEngine(UmpParams())
    eng.seed_htf(None)
    eng.seed_htf(HtfSeed())
    assert eng.feeds.daily == [] and eng.feeds.weekly is None


def test_running_week_publishes_its_true_high_when_it_rolls():
    """The seeded Monday settlement bar is the week's real high; the replay
    only ever sees Thursday and Friday."""
    eng = UmpEngine(UmpParams())
    eng.seed_htf(htf_seed_from_official({**WEEK_PRIOR, **CONTRACT}, date(2026, 9, 3)))
    for d, o, h, l, c in (
        (date(2026, 9, 3), 580.90, 580.90, 580.90, 580.90),
        (date(2026, 9, 4), 586.70, 593.05, 556.00, 566.25),
        (date(2026, 9, 7), 460.05, 462.55, 391.80, 405.70),   # new ISO week
    ):
        for i in range(3):
            eng.process_minute(datetime(d.year, d.month, d.day, 9, 15 + i), o, h, l, c)

    # TradingView's w/c 31 Aug candle, read off the user's chart.
    assert eng.feeds.weekly == pytest.approx((768.57, 556.00, 566.25))


# -------------------------------------------------------- chart prefix bars


# The real 4 Sep session: the last 1-minute print is 561.10 but the exchange
# closed it at 566.25, which is what TradingView's daily bar shows.
_SEP4 = [
    _minute(date(2026, 9, 4), 9, 15, 586.70, 593.05, 570.00, 575.00),
    _minute(date(2026, 9, 4), 15, 29, 575.00, 578.00, 556.00, 561.10),
]


def test_settlement_only_dates_are_the_listed_but_idle_ones():
    minutes = [_minute(date(2026, 9, 3), 9, 15, 580.90, 580.90, 580.90, 580.90)]
    assert settlement_only_dates(CONTRACT, minutes) == [
        date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 4),
    ]
    assert settlement_only_dates(None, minutes) == []


def test_daily_series_stamps_one_bar_per_session_at_0915():
    minutes = [_minute(date(2026, 9, 3), 9, 15, 580.90, 580.90, 580.90, 580.90)]
    bars = daily_series_minutes(CONTRACT, minutes)
    assert [m.ts.astimezone(IST).date() for m in bars] == [
        date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3),
    ]
    assert all(m.ts.astimezone(IST).time() == time(9, 15) for m in bars)


def test_completed_day_closes_at_the_official_close_not_the_last_print():
    minutes = [
        _minute(date(2026, 9, 3), 9, 15, 580.90, 580.90, 580.90, 580.90),
        *_SEP4,
        _minute(date(2026, 9, 7), 9, 15, 460.05, 462.55, 391.80, 405.70),
    ]
    days = aggregate_minutes(daily_series_minutes(CONTRACT, minutes), 86400)
    sep4 = next(d for d in days if d["ts"].startswith("2026-09-04"))
    assert sep4["c"] == pytest.approx(566.25), "official close, not the 561.10 print"
    assert sep4["o"] == pytest.approx(586.70)


def test_running_session_still_folds_from_its_own_minutes():
    """Mid-session the chart must show what has happened so far, never the
    full official day — that would be look-ahead drawn on the chart."""
    minutes = [
        _minute(date(2026, 9, 3), 9, 15, 580.90, 580.90, 580.90, 580.90),
        _minute(date(2026, 9, 4), 9, 15, 586.70, 593.05, 570.00, 575.00),
    ]
    days = aggregate_minutes(daily_series_minutes(CONTRACT, minutes), 86400)
    sep4 = next(d for d in days if d["ts"].startswith("2026-09-04"))
    assert sep4["c"] == pytest.approx(575.00)
    assert sep4["l"] == pytest.approx(570.00), "the 556.00 low has not happened yet"


def test_weekly_chart_matches_tradingview():
    """w/c 31 Aug on the chart must equal w/c 31 Aug in the engine's feed."""
    minutes = [
        _minute(date(2026, 9, 3), 9, 15, 580.90, 580.90, 580.90, 580.90),
        *_SEP4,
        _minute(date(2026, 9, 7), 9, 15, 460.05, 462.55, 391.80, 405.70),
    ]
    weeks = aggregate_minutes(daily_series_minutes(CONTRACT, minutes), 604800)
    w = next(x for x in weeks if x["ts"].startswith("2026-08-31"))
    assert (w["o"], w["h"], w["l"], w["c"]) == pytest.approx(
        (768.57, 768.57, 556.00, 566.25)
    ), "the TradingView readout for this candle"


def test_daily_chart_gains_one_candle_per_listed_day():
    minutes = [_minute(date(2026, 9, 3), 9, 15, 580.90, 580.90, 580.90, 580.90)]
    days = aggregate_minutes(daily_series_minutes(CONTRACT, minutes), 86400)
    assert [d["ts"][:10] for d in days] == [
        "2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03",
    ]


def test_intraday_intervals_are_left_alone():
    """TradingView's 4h chart for this contract starts 3 Sep and so must ours."""
    from app.api.algo_engines import choose_display_candles

    minutes = [_minute(date(2026, 9, 3), 9, 15 + i, 580.0, 581.0, 579.0, 580.0)
               for i in range(5)]
    for interval in ("5m", "15m", "1h"):
        got = choose_display_candles(
            interval, [], 5, minutes, None, date(2026, 9, 3), CONTRACT
        )
        assert got["candles"][0]["ts"].startswith("2026-09-03"), interval
        assert got["candles_source"] == "minutes"

    week = choose_display_candles("1W", [], 5, minutes, None, date(2026, 9, 3), CONTRACT)
    assert week["candles_source"] == "minutes+settlement"
    assert week["candles"][0]["ts"].startswith("2026-08-31")
    assert "listed but did not trade" in week["candles_note"]
    assert "2026-08-31" in week["settlement_dates"]

    bare = choose_display_candles("1W", [], 5, minutes, None, date(2026, 9, 3), None)
    assert bare["candles_source"] == "minutes"
    assert bare["candles_note"] is None


# ------------------------------------------------------------- boot gap-fill


def test_settlement_backfill_is_on_by_default_and_covers_a_quarter():
    from app.core.config import settings

    assert settings.settlement_backfill_on_boot is True
    assert settings.settlement_backfill_days >= 60, (
        "a NIFTY monthly reaches ~3 months of listed life; the window must "
        "cover it or the oldest contracts seed short"
    )


def test_gapfill_finds_the_settlement_script():
    from app.ingest import gapfill

    assert gapfill._settlement_script_path() is not None, (
        "the boot pass silently no-ops if the script cannot be located"
    )


async def test_settlement_pass_runs_before_the_truedata_credential_gate(monkeypatch):
    """It needs no vendor credentials, so a stack without them must still
    get its settlement bars."""
    from app.ingest import gapfill

    calls: list[tuple] = []

    async def fake_puller(symbol, script, *extra):
        calls.append((symbol, script.name, extra))
        return 0, ["== done: 0 rows written"]

    monkeypatch.setattr(gapfill, "_run_puller", fake_puller)
    monkeypatch.setattr(gapfill.settings, "truedata_user", "")
    monkeypatch.setattr(gapfill.settings, "truedata_password", "")

    report = await gapfill.run_gapfill_once(reason="test")
    assert report["status"] == "skipped_no_truedata_credentials"
    assert report["settlement"]["symbols"], "the settlement pass must have run"
    assert all(c[1] == "nse_settlement_backfill.py" for c in calls)
    assert all("--symbol" in c[2] for c in calls)


async def test_settlement_pass_failure_never_blocks_the_vendor_pass(monkeypatch):
    from app.ingest import gapfill

    async def boom(*a, **k):
        raise RuntimeError("archive down")

    monkeypatch.setattr(gapfill, "_run_puller", boom)
    monkeypatch.setattr(gapfill.settings, "truedata_user", "")
    monkeypatch.setattr(gapfill.settings, "truedata_password", "")

    report = await gapfill.run_gapfill_once(reason="test")
    assert "archive down" in str(report["settlement"]["symbols"])
    assert report["status"] == "skipped_no_truedata_credentials"


async def test_settlement_pass_skips_non_nse_underlyings(monkeypatch):
    """The NSE F&O file never contains SENSEX, and nothing is written, so the
    already-present check can never short-circuit -- an unskipped BSE symbol
    re-downloads the whole archive window on every boot."""
    from app.ingest import gapfill

    calls: list[str] = []

    async def fake_puller(symbol, script, *extra):
        calls.append(symbol)
        return 0, ["== done: 0 rows written"]

    monkeypatch.setattr(gapfill, "_run_puller", fake_puller)
    monkeypatch.setattr(gapfill.settings, "gapfill_symbols", "NIFTY,SENSEX")

    got = await gapfill._settlement_pass()
    assert calls == ["NIFTY"]
    assert got["skipped_non_nse"] == ["SENSEX"]
