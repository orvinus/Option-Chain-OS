#!/usr/bin/env bash
# ============================================================================
# NIGHTLY TRUEDATA TOP-UP (VPS-resident) — cron 12:05 UTC (= 17:35 IST).
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
docker exec docker-timescaledb-1 psql -U postgres -d oi -c "DELETE FROM oi_backfill_progress WHERE (split_part(unit_key,':',2) ~ '^[0-9]{6}\$' AND to_date(split_part(unit_key,':',2),'YYMMDD') >= current_date - 7) OR (split_part(unit_key,':',2) IN ('IDX','FUT') AND to_date(split_part(unit_key,':',3),'YYMMDD') >= current_date - 35);"

# --- 3. pulls (chains + index/futures, then the tick window) ---
"$PY" -u scripts/truedata_backfill.py pull --symbol NIFTY --include-live-days
"$PY" -u scripts/truedata_backfill.py pull --symbol SENSEX --include-live-days
"$PY" -u scripts/truedata_backfill.py pull-ticks --symbol NIFTY
"$PY" -u scripts/truedata_backfill.py pull-ticks --symbol SENSEX

# --- 4. validate + freshness ---
"$PY" scripts/truedata_backfill.py validate 2>/dev/null | grep -E '^== '
docker exec docker-timescaledb-1 psql -U postgres -d oi -t -c "SELECT symbol, max((ts AT TIME ZONE 'Asia/Kolkata')::date) FROM oi_snapshots_unified WHERE symbol IN ('NIFTY','SENSEX') GROUP BY 1;"

echo "===== NIGHTLY TOPUP END $(date -u '+%F %T UTC') ====="
} >> "$LOG" 2>&1
