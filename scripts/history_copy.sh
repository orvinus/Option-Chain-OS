#!/usr/bin/env bash
# Copy stored market history from one database to another — e.g. this PC's
# backfilled February–April archive to the VPS, whose backtests could not see
# it (2026-09-23). Market data only: no credentials, users or config.
#
#   export (on the machine that HAS the data):
#     scripts/history_copy.sh export FROM TO OUT_DIR [SYMBOLS]
#       FROM inclusive, TO exclusive (YYYY-MM-DD); SYMBOLS default NIFTY,SENSEX
#   import (on the machine that NEEDS it):
#     scripts/history_copy.sh import IN_DIR
#
# Safe to repeat: the import skips every row the target already holds
# (ON CONFLICT DO NOTHING on the tables' own unique keys), so an overlapping or
# re-run import never duplicates or overwrites anything. It then refreshes the
# day index (oi_day_stats) so Backtesting sees the new days immediately.
#
# Runs psql inside the compose TimescaleDB container (override with PG_CONTAINER).
set -euo pipefail

PG_CONTAINER="${PG_CONTAINER:-docker-timescaledb-1}"
DB_NAME="${DB_NAME:-oi}"
PSQL=(docker exec -i "$PG_CONTAINER" psql -U postgres -d "$DB_NAME" -v ON_ERROR_STOP=1 -q)

BARS_COLS="ts, symbol, expiry, strike, option_type, token, open, high, low, close, volume, volume_cum, oi, underlying, source"
EOD_COLS="trade_date, symbol, expiry, strike, option_type, token, open, high, low, close, volume, oi, source"

die() { echo "ERROR: $*" >&2; exit 1; }

sym_list() {  # NIFTY,SENSEX -> 'NIFTY','SENSEX'
  local out="" s
  IFS=',' read -ra parts <<< "$1"
  for s in "${parts[@]}"; do
    s="$(echo "$s" | tr '[:lower:]' '[:upper:]' | tr -cd 'A-Z0-9')"
    [[ -n "$s" ]] && out+="${out:+,}'$s'"
  done
  [[ -n "$out" ]] || die "no symbols"
  echo "$out"
}

cmd_export() {
  local from="$1" to="$2" out="$3" syms
  [[ "$from" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ && "$to" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] \
    || die "dates must be YYYY-MM-DD"
  syms="$(sym_list "${4:-NIFTY,SENSEX}")"
  # NB: '<date>'::timestamp AT TIME ZONE 'Asia/Kolkata' = midnight IST. The
  # ::date form converts through the session zone (UTC) first and silently
  # starts the window at 11:00 IST — found in this script's first run.
  mkdir -p "$out"
  echo "$(date -Is) exporting oi_archive_bars [$from, $to) for $syms ..."
  "${PSQL[@]}" -c "COPY (SELECT $BARS_COLS FROM oi_archive_bars
      WHERE symbol IN ($syms)
        AND ts >= ('$from'::timestamp AT TIME ZONE 'Asia/Kolkata')
        AND ts <  ('$to'::timestamp AT TIME ZONE 'Asia/Kolkata'))
      TO STDOUT WITH (FORMAT csv)" | gzip -6 > "$out/oi_archive_bars.csv.gz"
  echo "$(date -Is) exporting eod_bars before $to for $syms (daily history for the UMP levels) ..."
  "${PSQL[@]}" -c "COPY (SELECT $EOD_COLS FROM eod_bars
      WHERE symbol IN ($syms) AND trade_date < '$to'::date)
      TO STDOUT WITH (FORMAT csv)" | gzip -6 > "$out/eod_bars.csv.gz"
  gzip -t "$out/oi_archive_bars.csv.gz" "$out/eod_bars.csv.gz"
  printf 'from=%s\nto=%s\nsymbols=%s\nexported_at=%s\n' "$from" "$to" "$syms" "$(date -Is)" > "$out/MANIFEST"
  echo "$(date -Is) done:"
  ls -lh "$out"
  echo "rows: bars=$(gzip -dc "$out/oi_archive_bars.csv.gz" | wc -l) eod=$(gzip -dc "$out/eod_bars.csv.gz" | wc -l)"
}

load_table() {  # $1 file, $2 table, $3 column list
  local file="$1" table="$2" cols="$3"
  [[ -s "$file" ]] || die "missing $file"
  echo "$(date -Is) importing $table from $(basename "$file") ..."
  {
    echo "BEGIN;"
    echo "CREATE TEMP TABLE _in (LIKE $table INCLUDING DEFAULTS) ON COMMIT DROP;"
    echo "COPY _in ($cols) FROM STDIN WITH (FORMAT csv);"
    gzip -dc "$file"
    echo '\.'
    echo "WITH ins AS (INSERT INTO $table ($cols) SELECT $cols FROM _in ON CONFLICT DO NOTHING RETURNING 1)"
    echo "SELECT '$table: ' || (SELECT count(*) FROM _in) || ' rows read, ' || (SELECT count(*) FROM ins) || ' new';"
    echo "COMMIT;"
  } | docker exec -i "$PG_CONTAINER" psql -U postgres -d "$DB_NAME" -v ON_ERROR_STOP=1 -q -At
}

cmd_import() {
  local in="$1"
  [[ -f "$in/MANIFEST" ]] || die "$in/MANIFEST not found — not an export folder"
  cat "$in/MANIFEST"
  gzip -t "$in/oi_archive_bars.csv.gz" "$in/eod_bars.csv.gz" || die "corrupt export"
  load_table "$in/oi_archive_bars.csv.gz" oi_archive_bars "$BARS_COLS"
  load_table "$in/eod_bars.csv.gz" eod_bars "$EOD_COLS"
  echo "$(date -Is) refreshing the day index (oi_day_stats) ..."
  "${PSQL[@]}" -c "REFRESH MATERIALIZED VIEW oi_day_stats"
  "${PSQL[@]}" -At -c "SELECT symbol || ': first usable day ' || min(day) FILTER (WHERE usable_minutes >= 300)
      || ', ' || count(*) FILTER (WHERE usable_minutes >= 300) || ' usable days'
      FROM oi_day_stats WHERE NOT is_weekend AND symbol IN ('NIFTY','SENSEX') GROUP BY symbol ORDER BY symbol"
  echo "$(date -Is) import done."
}

case "${1:-}" in
  export) [[ $# -ge 4 ]] || die "usage: $0 export FROM TO OUT_DIR [SYMBOLS]"; cmd_export "$2" "$3" "$4" "${5:-}" ;;
  import) [[ $# -ge 2 ]] || die "usage: $0 import IN_DIR"; cmd_import "$2" ;;
  *) die "usage: $0 export FROM TO OUT_DIR [SYMBOLS] | import IN_DIR" ;;
esac
