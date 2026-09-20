"""Layer FX — cross-vendor comparison: our live feed vs TrueData.

Named ``layer_fx`` and not ``layer_f`` because ``oi_delta.py`` already emits
``layer_f``; reusing the tag would collide in the scorecard.

RETROSPECTIVE MODE (this file, today)
------------------------------------
Runs with **no TrueData subscription, no live feed and no code in the ingest
path**, against data already on disk.

``scripts/nightly_topup_vps.sh`` has been running ``pull --include-live-days``
for NIFTY and SENSEX every night. That flag disables the backfill's day-skip, so
for every recent trading day we hold BOTH:

    option_oi_snapshots   XTS, live, arrival-stamped, PERSIST_BUCKET granularity
    oi_archive_bars       TrueData, 1-min bars, exchange-minute stamped

The same contracts, the same minutes, two vendors. That accidental overlap is a
free A/B corpus, and it answers — on real production data rather than a sandbox
probe — the four questions that otherwise gate the whole migration:

    D-1  OI units          is TrueData reporting units or lots? (per exchange)
    D-3  OI cadence        does TrueData's OI actually CHANGE more often than the
                           ~1/min XTS gave us, or does exchange dissemination cap
                           it anyway? This is the migration's headline claim and
                           it has never been measured.
    D-5  LTP divergence    do the two vendors agree on price?
    P-10 timestamps        does the archive's 09:15 bar line up with the live
                           table's 09:15 bucket, or is there a systematic skew?

It also produces the XTS-side baselines the cutover gates compare against (gap
census, coverage). What it CANNOT do is gate per-contract OI/LTP at tick
tolerances: the live row is "last tick arriving in minute T" while the archive
row is a 1-min bar close, so the two are not sampled at the same instant. Those
checks are reported as INFO here and become gates only in LIVE mode.
gap census (G3) and the per-contract coverage the shadow run must match.

Run it BEFORE the ``live_days`` de-duplication in migration 0006 starts hiding
the overlap — the fix is correct, but it removes this corpus for future days.

    python backend/validation/layer_fx_crossvendor.py --symbol NIFTY --days 20
    python backend/validation/layer_fx_crossvendor.py --symbol SENSEX --days 20 --json

LIVE MODE (phase 3, not implemented here)
-----------------------------------------
The same metrics with ``td_shadow_snapshots`` as the TrueData source instead of
``oi_archive_bars``. The comparison logic below is written against a generic
(ours, theirs) row shape so that swap is a query change, not a rewrite.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import text  # noqa: E402

import common  # noqa: E402
from common import Finding, SourceSnapshot  # noqa: E402
from oi_units import check_oi_units  # noqa: E402

LAYER = "layer_fx"

# Tolerances. Deliberately tight — these gate a production vendor swap.
OI_TOL_PCT = 0.5          # per-contract OI level
LTP_TOL_ABS = 0.50        # rupees
LTP_TOL_PCT = 0.5
TOTALS_TOL_PCT = 0.5      # chain-level call/put OI totals and PCR
# Only compare contracts with real interest; illiquid strikes are noise and the
# archive legitimately holds no bar for a minute with no trade.
MIN_OI_FOR_COMPARE = 50


# --------------------------------------------------------------------------
# Data access
# --------------------------------------------------------------------------
_OVERLAP_DAYS_SQL = text(
    """
    SELECT d.day
    FROM (
        SELECT DISTINCT (ts AT TIME ZONE 'Asia/Kolkata')::date AS day
        FROM option_oi_snapshots
        WHERE symbol = :symbol AND ts >= :since
    ) d
    JOIN (
        SELECT DISTINCT (ts AT TIME ZONE 'Asia/Kolkata')::date AS day
        FROM oi_archive_bars
        WHERE symbol = :symbol AND ts >= :since AND option_type IN ('CE','PE')
    ) a USING (day)
    ORDER BY d.day DESC
    """
)

# One row per (contract, minute) with both vendors' values side by side.
#
# The live table is bucketed at PERSIST_BUCKET (1s in production) while the
# archive is 1-min bars, so the live side is collapsed to the LAST value in each
# IST minute before joining — which is exactly what a 1-min bar's close means.
# Anything else would compare a mid-minute tick against a minute close and
# manufacture divergence that isn't there.
#
# EXPIRY IS PART OF THE KEY, and omitting it is not a subtle inaccuracy: strike
# 24500 CE exists in the weekly AND the monthly simultaneously. The live feed
# subscribes one expiry (current_weekly, ATM±11); the archive pulled every expiry
# in six months. Grouping without expiry makes last(oi, ts) pick an arbitrary
# expiry on each side independently, and the resulting ratio is meaningless — it
# reported a "4x units mismatch" on real data during development, which would
# have been a catastrophic false positive against the vendor.
_PAIRED_SQL = text(
    """
    WITH live AS (
        SELECT
            time_bucket('1 minute', ts) AS minute,
            expiry, strike, option_type,
            last(oi, ts)         AS oi,
            last(ltp, ts)        AS ltp,
            last(volume, ts)     AS volume,
            last(underlying, ts) AS underlying,
            max(ts)              AS last_ts,
            count(*)             AS row_count
        FROM option_oi_snapshots
        WHERE symbol = :symbol
          AND (ts AT TIME ZONE 'Asia/Kolkata')::date = :day
          AND token ~ '^[0-9]+$'          -- XTS-era rows only
        GROUP BY 1, 2, 3, 4
    ),
    arch AS (
        SELECT
            time_bucket('1 minute', ts) AS minute,
            expiry, strike, option_type,
            last(oi, ts)         AS oi,
            last(close, ts)      AS ltp,
            last(volume_cum, ts) AS volume,
            last(underlying, ts) AS underlying
        FROM oi_archive_bars
        WHERE symbol = :symbol
          AND (ts AT TIME ZONE 'Asia/Kolkata')::date = :day
          AND option_type IN ('CE','PE')
        GROUP BY 1, 2, 3, 4
    )
    SELECT
        l.minute, l.expiry, l.strike, l.option_type,
        l.oi   AS oi_x,  a.oi   AS oi_t,
        l.ltp  AS ltp_x, a.ltp  AS ltp_t,
        l.volume AS vol_x, a.volume AS vol_t,
        l.underlying AS spot_x, a.underlying AS spot_t,
        l.row_count
    FROM live l
    JOIN arch a
      ON  a.minute      = l.minute + make_interval(mins => :offset_min)
      AND a.expiry      = l.expiry
      AND a.strike      = l.strike
      AND a.option_type = l.option_type
    ORDER BY l.minute, l.expiry, l.strike, l.option_type
    """
)

# Distinct OI VALUES per contract per session, per vendor. This is the cadence
# measurement: a vendor whose OI genuinely updates faster produces more distinct
# values over the same session, regardless of how many frames it sent.
# Restricted to the contracts BOTH vendors carried that day (via :expiries), so
# the comparison is not "the live feed's one subscribed weekly" against "every
# expiry the backfill pulled". Grouped by expiry for the same reason as above.
_CADENCE_SQL = text(
    """
    SELECT 'xts' AS vendor, expiry, strike, option_type,
           count(DISTINCT oi) AS distinct_oi
    FROM option_oi_snapshots
    WHERE symbol = :symbol AND (ts AT TIME ZONE 'Asia/Kolkata')::date = :day
      AND token ~ '^[0-9]+$'
      AND expiry = ANY(:expiries)
    GROUP BY 1,2,3,4
    UNION ALL
    SELECT 'truedata', expiry, strike, option_type, count(DISTINCT oi)
    FROM oi_archive_bars
    WHERE symbol = :symbol AND (ts AT TIME ZONE 'Asia/Kolkata')::date = :day
      AND option_type IN ('CE','PE')
      AND expiry = ANY(:expiries)
    GROUP BY 1,2,3,4
    """
)

# Per-contract inter-row gap census on the live side — the XTS baseline the
# shadow run must beat (gate G3).
_GAP_SQL = text(
    """
    WITH ordered AS (
        -- Partitioned by the FULL contract key: interleaving two expiries'
        -- rows for the same strike would hide real gaps behind the other
        -- expiry's ticks and understate the baseline.
        SELECT expiry, strike, option_type, ts,
               lag(ts) OVER (PARTITION BY expiry, strike, option_type
                             ORDER BY ts) AS prev_ts
        FROM option_oi_snapshots
        WHERE symbol = :symbol AND (ts AT TIME ZONE 'Asia/Kolkata')::date = :day
          AND token ~ '^[0-9]+$'
    )
    SELECT
        count(*) FILTER (WHERE gap >  30) AS gt30,
        count(*) FILTER (WHERE gap >  60) AS gt60,
        count(*) FILTER (WHERE gap > 300) AS gt300,
        count(DISTINCT minute)            AS coverage_minutes
    FROM (
        SELECT EXTRACT(EPOCH FROM (ts - prev_ts)) AS gap,
               date_trunc('minute', ts) AS minute
        FROM ordered WHERE prev_ts IS NOT NULL
    ) g
    """
)


def _overlap_days(conn, symbol: str, days: int) -> list[date]:
    since = date.today() - timedelta(days=days)
    return [r[0] for r in conn.execute(_OVERLAP_DAYS_SQL, {"symbol": symbol, "since": since})]


# --------------------------------------------------------------------------
# Per-day analysis
# --------------------------------------------------------------------------
def _agreement(rows) -> float:
    """Share of joined contract-minutes whose OI matches within tolerance.

    The alignment score. Deliberately OI and not LTP: OI is a slow, step-wise
    quantity, so a one-minute misalignment barely perturbs it on a quiet strike
    but is unmistakable in aggregate — while LTP moves every tick and would score
    noisily at every offset.
    """
    n = ok = 0
    for r in rows:
        ox, ot = r["oi_x"], r["oi_t"]
        if ox and ot and float(ot) >= MIN_OI_FOR_COMPARE:
            n += 1
            if abs(common.pct_diff(float(ox), float(ot))) <= OI_TOL_PCT:
                ok += 1
    return (ok / n) if n else 0.0


def _best_offset(conn, symbol: str, day: date) -> tuple[int, dict[int, float], list]:
    """Find the minute offset at which the two vendors actually agree.

    Nothing documents whether TrueData labels a 1-min bar at its OPEN or its
    CLOSE, and the live table's ``ts`` is arrival wall-clock, not exchange time.
    A one-minute systematic offset still produces plenty of joined rows — so
    "did the join return data?" cannot detect it, and comparing at offset 0
    silently reports a real vendor as ~90% wrong.

    Scanning offsets turns that ambiguity into the P-10 measurement: the offset
    with the best agreement IS the bar-labelling convention.
    """
    scores: dict[int, float] = {}
    best: int | None = None
    best_rows: list = []
    # Scanned nearest-first so an exact tie resolves to 0 (no offset) rather than
    # inventing a skew the data does not actually support.
    #
    # The window MUST be wide enough to contain a timezone-class error, not just
    # a bar-labelling one. The original +/-2 scan was too narrow and reported a
    # flat ~11% at every offset — which reads as "the vendors merely disagree"
    # when the truth was a -23 minute shift scoring 92%. -23 and +23 are included
    # explicitly because that is the pytz Local-Mean-Time trap for Asia/Kolkata
    # (+05:53 vs +05:30), and -330/+330 because a whole-offset IST/UTC mixup is
    # the other classic. A scan that cannot see the error it is looking for is
    # worse than no scan: it produces a confident wrong answer.
    for off in (0, -1, 1, -2, 2, -23, 23, -330, 330):
        rows = conn.execute(
            _PAIRED_SQL, {"symbol": symbol, "day": day, "offset_min": off}
        ).mappings().all()
        scores[off] = _agreement(rows)
        if best is None or scores[off] > scores[best]:
            best, best_rows = off, rows
    return best or 0, scores, best_rows


def _analyse_day(conn, symbol: str, day: date, lot: int | None) -> list[Finding]:
    findings: list[Finding] = []
    offset, scores, rows = _best_offset(conn, symbol, day)
    # A winner only counts if it beats offset 0 DECISIVELY. Agreement differences
    # of a point or two are sampling noise, and treating noise as a signal would
    # publish "the vendor labels its bars at the wrong end" on no evidence — the
    # kind of false finding that derails a migration. Measured on this corpus the
    # scan is flat (~11% at every offset from -2 to +2), which is itself the
    # answer: there is no systematic minute skew to correct for.
    margin = scores[offset] - scores.get(0, 0.0)
    decisive = offset != 0 and margin >= 0.05
    findings.append(Finding(
        layer=LAYER, metric="ts_offset_minutes", key=f"{symbol}:{day}",
        ours=offset if decisive else 0, theirs=0,
        diff={k: round(v * 100, 1) for k, v in sorted(scores.items())},
        tol="a real skew must beat offset 0 by >= 5 agreement points",
        verdict="FAIL" if decisive else "INFO",
        note=("OI agreement %% by minute offset applied to the ARCHIVE side. "
              f"best={offset:+d} beats 0 by {margin * 100:.1f} points — "
              + ("DECISIVE: TrueData labels 1-min bars at the opposite end of the "
                 "bucket, and every downstream comparison must apply this offset."
                 if decisive else
                 "not decisive, so no skew is claimed. A FLAT scan means minute "
                 "labels already agree.")),
    ))
    if not rows:
        return [Finding(
            layer=LAYER, metric="paired_rows", key=f"{symbol}:{day}",
            ours=0, theirs=0, diff=0, tol="n/a", verdict="SOURCE_UNAVAILABLE",
            note="no overlapping contract-minutes — the archive or the live feed is missing this day",
        )]

    # ---- OI units (D-1): the highest-severity check, so it runs first ----
    unit_pairs = [
        (float(r["oi_x"]), float(r["oi_t"]))
        for r in rows
        if r["oi_x"] and r["oi_t"] and float(r["oi_t"]) >= MIN_OI_FOR_COMPARE
    ]
    uv = check_oi_units(unit_pairs, label=f"{symbol}:{day}", lot=lot)
    findings.append(Finding(
        layer=LAYER, metric="oi_unit_sanity", key=f"{symbol}:{day}",
        ours=1.0, theirs=uv.median_ratio, diff=uv.implied_scale,
        tol=f"median XTS/TD OI ratio in [{0.98}, {1.02}]",
        verdict=uv.verdict, note=uv.note,
    ))

    # ---- Per-contract levels ----
    oi_fail = oi_n = 0
    ltp_fail = ltp_n = 0
    spot_diffs: list[float] = []
    for r in rows:
        oi_x, oi_t = r["oi_x"], r["oi_t"]
        if oi_x and oi_t and float(oi_t) >= MIN_OI_FOR_COMPARE:
            oi_n += 1
            if abs(common.pct_diff(float(oi_x), float(oi_t))) > OI_TOL_PCT:
                oi_fail += 1
        lx, lt = r["ltp_x"], r["ltp_t"]
        if lx and lt:
            ltp_n += 1
            if abs(float(lx) - float(lt)) > max(LTP_TOL_ABS,
                                                abs(float(lt)) * LTP_TOL_PCT / 100.0):
                ltp_fail += 1
        sx, st = r["spot_x"], r["spot_t"]
        if sx and st:
            spot_diffs.append(abs(common.pct_diff(float(sx), float(st))))

    def _pass_pct(fail: int, n: int) -> float:
        return round(100.0 * (n - fail) / n, 2) if n else 0.0

    # These are INFO in retrospective mode, and that is a deliberate limitation of
    # the corpus rather than a lowered standard.
    #
    # The two sides are not sampled at the same INSTANT. The live row is "the last
    # tick that arrived during IST minute T" — which may be at T+0.2s or T+59s —
    # while the archive row is TrueData's 1-minute bar close for T. OI and premium
    # both move within a minute, so a per-contract 0.5% / Rs0.50 tolerance is
    # measuring sampling phase, not vendor disagreement. Empirically this scores
    # ~11% agreement at EVERY minute offset, i.e. the residual is phase, not skew.
    #
    # The strict per-contract gates (G1) belong to LIVE mode, where both sides are
    # tick-stamped and can be matched within +/-90s of each other. Reporting a
    # retrospective FAIL here would manufacture a blocking finding out of a known
    # methodological limit.
    findings.append(Finding(
        layer=LAYER, metric="oi_level_agreement_pct", key=f"{symbol}:{day}",
        ours=_pass_pct(oi_fail, oi_n), theirs=None, diff=oi_fail,
        tol=f"informational: within {OI_TOL_PCT}% at minute granularity",
        verdict="SKIP" if not oi_n else "INFO",
        note=(f"{oi_fail}/{oi_n} contract-minutes differ by more than {OI_TOL_PCT}%. "
              "NOT a defect signal: the two sources sample at different instants "
              "within the minute. Gate this in LIVE mode against tick-stamped rows."),
    ))
    findings.append(Finding(
        layer=LAYER, metric="ltp_agreement_pct", key=f"{symbol}:{day}",
        ours=_pass_pct(ltp_fail, ltp_n), theirs=None, diff=ltp_fail,
        tol=f"informational: within max(Rs{LTP_TOL_ABS}, {LTP_TOL_PCT}%)",
        verdict="SKIP" if not ltp_n else "INFO",
        note=(f"{ltp_fail}/{ltp_n} contract-minutes outside tolerance. Same "
              "sampling-phase caveat as OI, and stronger: premium moves on every "
              "tick, so a within-minute phase difference dominates."),
    ))
    if spot_diffs:
        findings.append(Finding(
            layer=LAYER, metric="spot_divergence_pct", key=f"{symbol}:{day}",
            ours=round(statistics.median(spot_diffs), 4),
            theirs=0.1, diff=round(max(spot_diffs), 4),
            tol="median |spot diff| <= 0.1%",
            verdict="PASS" if statistics.median(spot_diffs) <= 0.1 else "FAIL",
            note=f"median over {len(spot_diffs)} minutes; max {max(spot_diffs):.4f}%",
        ))

    # ---- Chain totals + PCR ----
    # Nearest shared expiry only — a "chain total" summed across weekly AND
    # monthly is not a number any screen in the product displays.
    front = min(r["expiry"] for r in rows)
    tot_x = defaultdict(float)
    tot_t = defaultdict(float)
    front_rows = [r for r in rows if r["expiry"] == front]
    last_minute = max(r["minute"] for r in front_rows)
    for r in front_rows:
        if r["minute"] != last_minute:
            continue
        tot_x[r["option_type"]] += float(r["oi_x"] or 0)
        tot_t[r["option_type"]] += float(r["oi_t"] or 0)
    for ot in ("CE", "PE"):
        if tot_x[ot] and tot_t[ot]:
            d = abs(common.pct_diff(tot_x[ot], tot_t[ot]))
            findings.append(Finding(
                layer=LAYER, metric=f"total_{ot.lower()}_oi", key=f"{symbol}:{day}",
                ours=int(tot_x[ot]), theirs=int(tot_t[ot]), diff=round(d, 4),
                tol=f"informational: within {TOTALS_TOL_PCT}% at one sampled minute",
                verdict="INFO",
                note=(f"at {last_minute} UTC, expiry {front}. Chain totals are more "
                      "robust to sampling phase than single contracts, but still a "
                      "one-minute snapshot — treat a large gap as a prompt to "
                      "investigate, not as a vendor defect."),
            ))
    if tot_x["CE"] and tot_t["CE"] and tot_x["PE"] and tot_t["PE"]:
        pcr_x = tot_x["PE"] / tot_x["CE"]
        pcr_t = tot_t["PE"] / tot_t["CE"]
        d = abs(common.pct_diff(pcr_x, pcr_t))
        findings.append(Finding(
            layer=LAYER, metric="pcr", key=f"{symbol}:{day}",
            ours=round(pcr_x, 4), theirs=round(pcr_t, 4), diff=round(d, 4),
            tol=f"informational: within {TOTALS_TOL_PCT}%",
            verdict="INFO",
            note="derived from the totals above; same one-minute sampling caveat",
        ))

    # ---- OI change cadence (D-3) — REPORT, never pass/fail ----
    # This is a measurement, not a gate: either answer is acceptable, but shipping
    # without the number means claiming a headline benefit we never verified.
    shared_expiries = sorted({r["expiry"] for r in rows})
    cad = conn.execute(
        _CADENCE_SQL,
        {"symbol": symbol, "day": day, "expiries": shared_expiries},
    ).mappings().all()
    per_vendor: dict[str, list[int]] = defaultdict(list)
    for r in cad:
        per_vendor[r["vendor"]].append(int(r["distinct_oi"]))
    med_x = statistics.median(per_vendor["xts"]) if per_vendor.get("xts") else None
    med_t = statistics.median(per_vendor["truedata"]) if per_vendor.get("truedata") else None
    if med_x is not None and med_t is not None:
        findings.append(Finding(
            layer=LAYER, metric="oi_change_cadence", key=f"{symbol}:{day}",
            ours=med_x, theirs=med_t,
            diff=round((med_t / med_x) if med_x else 0, 3),
            tol="measurement only — no threshold",
            verdict="INFO",
            note=(
                "median DISTINCT OI values per contract per session (TrueData/XTS "
                "ratio in diff). ASYMMETRIC EVIDENCE, read it carefully: the "
                "archive is 1-min BARS, capped at ~375 values/session, while the "
                "live table buckets at 1s and can register more. So a ratio > 1 is "
                "DECISIVE — TrueData's OI genuinely moves faster even when "
                "handicapped. A ratio <= 1 proves NOTHING: it is exactly what the "
                "bar-granularity cap would produce regardless of the true cadence. "
                "Only the live tick feed (shadow mode, gate G6) can settle that "
                "case, and until it does, no sub-minute OI improvement may be "
                "claimed for the migration."
            ),
        ))

    # ---- Sample size (the offset scan above is the real alignment test) ----
    findings.append(Finding(
        layer=LAYER, metric="paired_contract_minutes", key=f"{symbol}:{day}",
        ours=len(rows), theirs=None, diff=offset,
        tol="enough joined rows for the day's verdicts to mean anything",
        verdict="PASS" if len(rows) > 100 else "FAIL",
        note=f"joined at offset {offset:+d} min",
    ))

    # ---- XTS gap-census baseline (feeds gate G3) ----
    g = conn.execute(_GAP_SQL, {"symbol": symbol, "day": day}).mappings().first()
    if g:
        findings.append(Finding(
            layer=LAYER, metric="xts_gap_census", key=f"{symbol}:{day}",
            ours={"gt30": g["gt30"], "gt60": g["gt60"], "gt300": g["gt300"]},
            theirs=None, diff=g["coverage_minutes"],
            tol="baseline only — TrueData must not be worse (gate G3)",
            verdict="INFO",
            note=f"{g['coverage_minutes']} distinct minutes covered by the live feed",
        ))
    return findings


# --------------------------------------------------------------------------
def run(symbol: str, days: int) -> list[Finding]:
    from app.market.symbols import get_registry  # noqa: E402

    entry = get_registry().get(symbol)
    lot = entry.lot_size if entry else None

    findings: list[Finding] = []
    eng = common.sync_engine()
    try:
        with eng.connect() as conn:
            overlap = _overlap_days(conn, symbol, days)
            if not overlap:
                return [Finding(
                    layer=LAYER, metric="overlap_days", key=symbol,
                    ours=0, theirs=None, diff=None,
                    tol="need >= 1 day present in BOTH stores",
                    verdict="SOURCE_UNAVAILABLE",
                    note=("no overlapping days. Either the nightly --include-live-days "
                          "pull has not run for this symbol, or migration 0006's "
                          "live_days de-duplication is already hiding the overlap."),
                )]
            print(f"== {symbol}: {len(overlap)} overlapping day(s): "
                  f"{overlap[-1]} .. {overlap[0]}")
            for day in overlap:
                day_findings = _analyse_day(conn, symbol, day, lot)
                findings.extend(day_findings)
                verdicts = common.summarize(day_findings)
                print(f"   {day}  " + "  ".join(f"{k}={v}" for k, v in sorted(verdicts.items())))
    finally:
        eng.dispose()
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--days", type=int, default=20, help="lookback window in calendar days")
    ap.add_argument("--json", action="store_true", help="also write logs/validation/<date>/layer_fx.json")
    args = ap.parse_args()

    findings = run(args.symbol.upper(), args.days)
    summary = common.summarize(findings)

    print("\n== layer_fx summary ==")
    for k, v in sorted(summary.items()):
        print(f"   {k:20s} {v}")

    # Surface the three answers this run exists to produce.
    print("\n== headline answers ==")
    for metric in ("oi_unit_sanity", "oi_change_cadence", "ts_offset_minutes"):
        hits = [f for f in findings if f.metric == metric]
        if not hits:
            continue
        verdicts = {f.verdict for f in hits}
        print(f"   {metric:20s} {sorted(verdicts)}  (n={len(hits)} days)")
        print(f"        e.g. ours={hits[0].ours} theirs={hits[0].theirs} — {hits[0].note[:120]}")

    if args.json:
        common.ensure_dirs()
        snapshots = [
            SourceSnapshot(
                source="option_oi_snapshots (XTS live)",
                fetched_at_utc=common.iso(common.now_utc()) or "",
                ok=True,
                extra={"vendor": "xts", "token_filter": "^[0-9]+$"},
            ),
            SourceSnapshot(
                source="oi_archive_bars (TrueData getbars)",
                fetched_at_utc=common.iso(common.now_utc()) or "",
                ok=True,
                extra={"vendor": "truedata", "granularity": "1min"},
            ),
        ]
        path = common.write_layer_output(
            LAYER, findings, snapshots,
            meta={"symbol": args.symbol.upper(), "days": args.days, "mode": "retrospective"},
        )
        print(f"\nwrote {path}")

    return 1 if summary.get("FAIL") else 0


if __name__ == "__main__":
    raise SystemExit(main())
