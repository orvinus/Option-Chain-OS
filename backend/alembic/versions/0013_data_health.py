"""oi_day_stats + a QUALITY-aware live_days — stop discarding good archive days.

THE BUG THIS FIXES (measured on the live DB, 2026-08-21):

``oi_snapshots_unified`` (migration 0006) suppresses the ENTIRE archive arm for
any ``(symbol, day)`` present in ``live_days``, and ``live_days`` was defined as

    SELECT DISTINCT symbol, (ts AT TIME ZONE 'Asia/Kolkata')::date
    FROM option_oi_snapshots

— i.e. **presence**, not quality. ONE live row is enough to hide a complete
archive session. Measured for NIFTY:

    day          live_min   live_rows   arch_min   served
    2026-07-21         11       8,263        376   11  ← 376-minute session discarded
    2026-07-23        241     190,443        376   241
    2026-07-24         73      25,153        376   73
    2026-07-28         96     164,462        376   96

Confirmed against the view itself: ``SELECT count(DISTINCT time_bucket('1
minute', ts)) FROM oi_snapshots_unified WHERE symbol='NIFTY' AND day =
'2026-07-21'`` returned **11**. The backtest preflight then skipped the day as
``insufficient coverage (11 min < 300)``.

So the "gaps and bad data" on existing days are mostly NOT vendor damage —
they are good archive data being thrown away by the de-dup rule in favour of a
fragmentary live day. Every future session where the backend was down for part
of the day would have been lost the same way.

THE FIX: keep the name and shape of ``live_days`` (so the unified view,
``preflight._MATVIEW_DAYS_SQL`` and the sentinel all keep working untouched)
but derive it from a new ``oi_day_stats`` matview using a QUALITY rule:

    winner = 'live'    when live_minutes >= LEAST(arch_minutes, 300)
                        and live_minutes > 0
           = 'archive' when arch_minutes > 0
           = 'none'    otherwise

Walked against the table above: 07-21 → LEAST(376,300)=300, 11 < 300 → archive
wins, 376 minutes come back. A normal fully-live day (370 min) → 370 >= 300 →
live wins, unchanged. A live-only day (arch 0) → LEAST(0,300)=0 → live wins,
unchanged. The 300 is the same "good enough session" bar
``min_minutes_per_day`` already uses.

DELIBERATELY NOT SYMMETRIC: the live arm of ``oi_snapshots_unified`` stays
ungated. Gating it on ``EXISTS live_days`` would mean a stale matview hides
TODAY's live data — a much worse failure than the duplicate-rows case the
preflight already detects. Keeping the asymmetry means a stale matview degrades
to exactly the behaviour we have today. The residual overlap on archive-wins
days is benign for the backtest: ``backtest/data.py`` aggregates
``last(oi, ts)`` per (strike, type, minute) and never sums across rows.

Revision ID: 0013_data_health
Revises: 0012_trade_broker_order_id
"""
from alembic import op

revision = "0013_data_health"
down_revision = "0012_trade_broker_order_id"
branch_labels = None
depends_on = None

# The session window every day-level statistic is measured over. Matches
# MARKET_OPEN_IST / MARKET_CLOSE_IST (the close moved 15:30 → 15:40 on
# 2026-08-04; hardcoding 15:30 here would silently drop the settlement window,
# the same bug scripts/truedata_backfill.py:78-84 records).
SESSION_OPEN = "09:15"
SESSION_CLOSE = "15:40"
# "A session good enough to trust." Same number as the backtest's
# min_minutes_per_day default.
GOOD_ENOUGH_MINUTES = 300


def upgrade() -> None:
    # ── 1. The unified view depends on live_days, so it must go first ──
    op.execute("DROP VIEW IF EXISTS oi_snapshots_unified;")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS live_days;")

    # ── 2. One row per (symbol, day): the single source of truth for every
    #      day-level question the platform asks. ──
    op.execute(
        f"""
        CREATE MATERIALIZED VIEW oi_day_stats AS
        WITH live AS (
            SELECT symbol,
                   (ts AT TIME ZONE 'Asia/Kolkata')::date AS day,
                   count(DISTINCT time_bucket('1 minute', ts)) AS minutes,
                   count(*)                                    AS rows_n,
                   count(underlying)                           AS spot_rows
            FROM option_oi_snapshots
            WHERE option_type IN ('CE', 'PE')
              AND (ts AT TIME ZONE 'Asia/Kolkata')::time
                  BETWEEN '{SESSION_OPEN}' AND '{SESSION_CLOSE}'
            GROUP BY 1, 2
        ),
        arch AS (
            SELECT symbol,
                   (ts AT TIME ZONE 'Asia/Kolkata')::date AS day,
                   count(DISTINCT time_bucket('1 minute', ts)) AS minutes,
                   count(*)                                    AS rows_n,
                   count(underlying)                           AS spot_rows,
                   MIN((ts AT TIME ZONE 'Asia/Kolkata')::time) AS first_t
            FROM oi_archive_bars
            WHERE option_type IN ('CE', 'PE')
              AND (ts AT TIME ZONE 'Asia/Kolkata')::time
                  BETWEEN '{SESSION_OPEN}' AND '{SESSION_CLOSE}'
            GROUP BY 1, 2
        )
        SELECT
            COALESCE(l.symbol, a.symbol)              AS symbol,
            COALESCE(l.day,    a.day)                 AS day,
            COALESCE(l.minutes, 0)                    AS live_minutes,
            COALESCE(l.rows_n,  0)                    AS live_rows,
            COALESCE(l.spot_rows, 0)                  AS live_spot_rows,
            COALESCE(a.minutes, 0)                    AS arch_minutes,
            COALESCE(a.rows_n,  0)                    AS arch_rows,
            COALESCE(a.spot_rows, 0)                  AS arch_spot_rows,
            a.first_t                                 AS arch_first_t,
            EXTRACT(ISODOW FROM COALESCE(l.day, a.day)) >= 6 AS is_weekend,
            CASE
                WHEN COALESCE(l.minutes, 0) > 0
                 AND COALESCE(l.minutes, 0)
                     >= LEAST(COALESCE(a.minutes, 0), {GOOD_ENOUGH_MINUTES})
                    THEN 'live'
                WHEN COALESCE(a.minutes, 0) > 0 THEN 'archive'
                ELSE 'none'
            END                                       AS winner,
            GREATEST(COALESCE(l.minutes, 0), COALESCE(a.minutes, 0))
                                                      AS usable_minutes
        FROM live l
        FULL OUTER JOIN arch a ON a.symbol = l.symbol AND a.day = l.day;
        """
    )
    # UNIQUE index is what permits REFRESH ... CONCURRENTLY (readers never block).
    op.execute(
        "CREATE UNIQUE INDEX ux_oi_day_stats ON oi_day_stats (symbol, day);"
    )
    op.execute("CREATE INDEX ix_oi_day_stats_day ON oi_day_stats (day DESC);")

    # ── 3. live_days keeps its NAME and SHAPE — only its meaning sharpens ──
    op.execute(
        """
        CREATE MATERIALIZED VIEW live_days AS
        SELECT symbol, day FROM oi_day_stats WHERE winner = 'live';
        """
    )
    op.execute("CREATE UNIQUE INDEX ux_live_days ON live_days (symbol, day);")

    # ── 4. Recreate 0006's view verbatim — it is unchanged; only the contents
    #      of live_days differ, which is the whole point. ──
    op.execute(
        """
        CREATE OR REPLACE VIEW oi_snapshots_unified AS
        SELECT ts, symbol, expiry, strike, option_type, token,
               oi, ltp, volume, underlying
        FROM option_oi_snapshots
        UNION ALL
        SELECT a.ts, a.symbol, a.expiry, a.strike, a.option_type, a.token,
               a.oi, a.close AS ltp, a.volume_cum AS volume, a.underlying
        FROM oi_archive_bars a
        WHERE a.option_type IN ('CE', 'PE')
          AND NOT EXISTS (
              SELECT 1 FROM live_days d
              WHERE d.symbol = a.symbol
                AND d.day = (a.ts AT TIME ZONE 'Asia/Kolkata')::date
          );
        """
    )

    # ── 5. Freshness of the refresher itself, so "the archive is current but
    #      the index is stale" is detectable rather than invisible. ──
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS data_health_refresh (
            name         TEXT PRIMARY KEY,
            refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            duration_ms  INTEGER NOT NULL DEFAULT 0,
            rows_n       BIGINT  NOT NULL DEFAULT 0
        );
        """
    )
    op.execute(
        """
        INSERT INTO data_health_refresh (name, rows_n)
        VALUES ('oi_day_stats', (SELECT count(*) FROM oi_day_stats)),
               ('live_days',    (SELECT count(*) FROM live_days))
        ON CONFLICT (name) DO UPDATE
          SET refreshed_at = now(), rows_n = EXCLUDED.rows_n;
        """
    )


def downgrade() -> None:
    # Restore 0006's presence-based definition exactly.
    op.execute("DROP VIEW IF EXISTS oi_snapshots_unified;")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS live_days;")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS oi_day_stats;")
    op.execute("DROP TABLE IF EXISTS data_health_refresh;")
    op.execute(
        """
        CREATE MATERIALIZED VIEW live_days AS
        SELECT DISTINCT symbol, (ts AT TIME ZONE 'Asia/Kolkata')::date AS day
        FROM option_oi_snapshots;
        """
    )
    op.execute("CREATE UNIQUE INDEX ux_live_days ON live_days (symbol, day);")
    op.execute(
        """
        CREATE OR REPLACE VIEW oi_snapshots_unified AS
        SELECT ts, symbol, expiry, strike, option_type, token,
               oi, ltp, volume, underlying
        FROM option_oi_snapshots
        UNION ALL
        SELECT a.ts, a.symbol, a.expiry, a.strike, a.option_type, a.token,
               a.oi, a.close AS ltp, a.volume_cum AS volume, a.underlying
        FROM oi_archive_bars a
        WHERE a.option_type IN ('CE', 'PE')
          AND NOT EXISTS (
              SELECT 1 FROM live_days d
              WHERE d.symbol = a.symbol
                AND d.day = (a.ts AT TIME ZONE 'Asia/Kolkata')::date
          );
        """
    )
