-- Manual fallback: lets you bootstrap the schema without running Alembic.
-- Equivalent of backend/alembic/versions/0001_init.py.
--
-- Run with:
--   psql "postgresql://postgres:postgres@localhost:5432/oi" -f scripts/init_db.sql

CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS option_oi_snapshots (
    ts          TIMESTAMPTZ NOT NULL,
    symbol      TEXT NOT NULL,
    expiry      DATE NOT NULL,
    strike      INTEGER NOT NULL,
    option_type CHAR(2) NOT NULL,
    token       TEXT NOT NULL,
    oi          BIGINT NOT NULL,
    ltp         NUMERIC(14, 2),
    volume      BIGINT,
    underlying  NUMERIC(14, 2),
    PRIMARY KEY (ts, token)
);

SELECT create_hypertable(
    'option_oi_snapshots',
    'ts',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS ix_oi_expiry_strike_ts
    ON option_oi_snapshots (expiry, strike, option_type, ts DESC);
CREATE INDEX IF NOT EXISTS ix_oi_symbol_ts
    ON option_oi_snapshots (symbol, ts DESC);
CREATE INDEX IF NOT EXISTS ix_oi_token_ts
    ON option_oi_snapshots (token, ts DESC);

CREATE MATERIALIZED VIEW IF NOT EXISTS oi_1h
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', ts) AS bucket,
    symbol,
    expiry,
    strike,
    option_type,
    last(oi, ts)         AS oi,
    last(ltp, ts)        AS ltp,
    last(volume, ts)     AS volume,
    last(underlying, ts) AS underlying
FROM option_oi_snapshots
GROUP BY 1, 2, 3, 4, 5
WITH NO DATA;

SELECT add_continuous_aggregate_policy('oi_1h',
    start_offset => INTERVAL '7 days',
    end_offset   => INTERVAL '5 minutes',
    schedule_interval => INTERVAL '5 minutes',
    if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS auth_sessions (
    id            SERIAL PRIMARY KEY,
    client_code   TEXT NOT NULL,
    jwt_token     TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    feed_token    TEXT NOT NULL,
    issued_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_auth_sessions_client_issued
    ON auth_sessions (client_code, issued_at DESC);

CREATE TABLE IF NOT EXISTS websocket_logs (
    id     BIGSERIAL PRIMARY KEY,
    ts     TIMESTAMPTZ NOT NULL DEFAULT now(),
    level  TEXT NOT NULL,
    event  TEXT NOT NULL,
    detail JSONB
);
CREATE INDEX IF NOT EXISTS ix_ws_logs_ts ON websocket_logs (ts DESC);

CREATE TABLE IF NOT EXISTS api_logs (
    id        BIGSERIAL PRIMARY KEY,
    ts        TIMESTAMPTZ NOT NULL DEFAULT now(),
    method    TEXT NOT NULL,
    path      TEXT NOT NULL,
    status    INTEGER NOT NULL,
    latency_ms INTEGER,
    client_ip TEXT
);
CREATE INDEX IF NOT EXISTS ix_api_logs_ts ON api_logs (ts DESC);
