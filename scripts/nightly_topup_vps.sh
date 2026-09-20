#!/usr/bin/env bash
# ============================================================================
# NIGHTLY TRUEDATA TOP-UP (VPS-resident).
# Installed by scripts/install-sentinel.sh as /usr/local/bin/oi-topup and driven
# by oi-topup.timer (17:35 / 21:00 IST + a 06:30 next-morning safety net).
# The old header claimed "cron 12:05 UTC"; no such crontab entry ever existed in
# this repo, which is exactly how the archive went 8 days stale unnoticed.
# Pulls straight into the production DB through the WARP proxy; no PC involved.
# TrueData's edge drops the VPS's direct IP (hosting-ASN filter), so every
# vendor call rides TRUEDATA_PROXY = Cloudflare WARP's local SOCKS port.
#   1. self-heal the WARP proxy path (restart + reconnect if unreachable)
#   2. clear recent ledger units so the last days re-fetch (upsert-safe)
#   3. re-pull both symbols' chains + index/futures, then the 5-day tick window
#   4. validate + freshness check
# Supersedes scripts/nightly_topup.cmd (PC job, kept as manual fallback).
# ============================================================================
set -u
cd "$(dirname "$0")/.."
PY="$HOME/.venvs/oi/bin/python"
LOG="logs/backfill/nightly_$(date +%Y%m%d).log"
mkdir -p logs/backfill

exec 9>/tmp/nightly_topup.lock
flock -n 9 || exit 0

{
echo "===== NIGHTLY TOPUP START $(date -u '+%F %T UTC') ====="

# --- 1. WARP self-heal (TrueData is only reachable through the proxy) ---
probe_warp() { curl -s --max-time 8 --socks5-hostname 127.0.0.1:40000 -o /dev/null -w '%{http_code}' https://auth.truedata.in 2>/dev/null; }
if [ "$(probe_warp)" = "000" ] || [ -z "$(probe_warp)" ]; then
  echo "warp path down — restarting warp-svc"
  systemctl restart warp-svc; sleep 8
  warp-cli --accept-tos connect; sleep 8
fi
code=$(probe_warp)
if [ "$code" = "000" ] || [ -z "$code" ]; then
  echo "FATAL: TrueData unreachable through WARP proxy"; echo "===== NIGHTLY TOPUP END (FAILED) $(date -u '+%F %T UTC') ====="; exit 1
fi
echo "warp path ok (HTTP $code)"

# --- 2. clear recent ledger units so the last days re-fetch (upsert-safe) ---
# Also re-opens the once-only EOD series units and every unit that ended in
# 'error' (mirrors backend/app/ingest/gapfill.py _RESET_LEDGER_SQL).
docker exec docker-timescaledb-1 psql -U postgres -d oi -c "DELETE FROM oi_backfill_progress WHERE (split_part(unit_key,':',2) ~ '^[0-9]{6}\$' AND to_date(split_part(unit_key,':',2),'YYMMDD') >= current_date - 7) OR (split_part(unit_key,':',2) IN ('IDX','FUT') AND to_date(split_part(unit_key,':',3),'YYMMDD') >= current_date - 35) OR unit_key LIKE 'eod:td:%' OR status = 'error';"

# --- 3. pulls (chains + index/futures, then the tick window) ---
"$PY" -u scripts/truedata_backfill.py pull --symbol NIFTY --include-live-days
"$PY" -u scripts/truedata_backfill.py pull --symbol SENSEX --include-live-days
"$PY" -u scripts/truedata_backfill.py pull-ticks --symbol NIFTY
"$PY" -u scripts/truedata_backfill.py pull-ticks --symbol SENSEX
# Official daily closes (NSE F&O bhavcopy) — the UMP engine's daily/weekly
# feed closes; TradingView's D bars close at this value, not the last trade.
"$PY" -u scripts/truedata_backfill.py pull-eod --symbol NIFTY --days 10 --years 1

# --- 4. refresh the live-day index that de-duplicates the unified view ---
# --include-live-days above deliberately re-fetches days the live feed also
# covered, so oi_archive_bars and option_oi_snapshots now hold the same contract
# minutes. oi_snapshots_unified resolves that by preferring the live arm for any
# (symbol, day) in live_days — which only works if this index knows about the day
# that just closed. Runs AFTER the pulls and CONCURRENTLY so readers never block.
# oi_day_stats FIRST — live_days is derived from it (migration 0013).
docker exec docker-timescaledb-1 psql -U postgres -d oi -c "REFRESH MATERIALIZED VIEW CONCURRENTLY oi_day_stats;"
docker exec docker-timescaledb-1 psql -U postgres -d oi -c "REFRESH MATERIALIZED VIEW CONCURRENTLY live_days;"

# --- 5. validate + FRESHNESS ASSERTION (this is what makes failure visible) ---
"$PY" scripts/truedata_backfill.py validate 2>/dev/null | grep -E '^== '
# Per-day integrity report (missing minutes, duplicates, OHLC, stale OI, tz,
# warm-up) for today + the previous session; persisted to data_integrity_runs.
"$PY" scripts/truedata_backfill.py integrity --symbol NIFTY --days 2 2>/dev/null | grep -E '^== ' || true
"$PY" scripts/truedata_backfill.py integrity --symbol SENSEX --days 2 2>/dev/null | grep -E '^== ' || true
docker exec docker-timescaledb-1 psql -U postgres -d oi -t -c "SELECT symbol, max(day) FROM oi_day_stats WHERE symbol IN ('NIFTY','SENSEX') AND winner <> 'none' GROUP BY 1;"

# Last COMPLETED trading day: previous weekday that is not in the NSE holiday
# file. Comparing against plain "yesterday" would page every Saturday and every
# holiday, and a pager that cries wolf gets muted — which is the same as not
# having one.
LTD=$(python3 - <<'PYEOF'
import datetime, json, pathlib
hol = set()
try:
    hol = set(json.loads(pathlib.Path("data/nse_holidays.json").read_text())["holidays"])
except Exception:
    pass
d = datetime.date.today()
for _ in range(14):
    d -= datetime.timedelta(days=1)
    if d.weekday() < 5 and d.isoformat() not in hol:
        print(d.isoformat()); break
PYEOF
)
NEWEST=$(docker exec docker-timescaledb-1 psql -U postgres -d oi -t -A -c   "SELECT COALESCE(max(day)::text,'') FROM oi_day_stats WHERE symbol IN ('NIFTY','SENSEX') AND winner <> 'none';")
echo "freshness: newest usable day=${NEWEST:-none} | last trading day=$LTD"
if [[ -z "$NEWEST" || "$NEWEST" < "$LTD" ]]; then
  echo "FAIL: archive is behind the last completed trading day ($NEWEST < $LTD)"
  echo "===== NIGHTLY TOPUP END (STALE) $(date -u '+%F %T UTC') ====="
  exit 1        # -> systemd OnFailure=oi-alert@ -> Telegram
fi

echo "===== NIGHTLY TOPUP END $(date -u '+%F %T UTC') ====="
} >> "$LOG" 2>&1
