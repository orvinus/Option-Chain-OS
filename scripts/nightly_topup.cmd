@echo off
rem ============================================================================
rem SUPERSEDED 2026-08-13 by scripts/nightly_topup_vps.sh (cron on the VPS,
rem through the WARP proxy) — the Task Scheduler entry is DISABLED; keep this
rem file only as a manual fallback if the VPS path ever breaks.
rem NIGHTLY TRUEDATA TOP-UP  — was scheduled 17:30 IST on this PC (Task
rem Scheduler: "OI-Nightly-Topup"). Keeps the VPS archive permanently current:
rem   1. ensures Docker + local DB are up (self-heals after a PC reboot)
rem   2. re-pulls the last ~4 days of both symbols' chains + index/futures
rem   3. captures the sliding 5-day tick window (use-it-or-lose-it)
rem   4. ships everything to the VPS and upserts, then validates there
rem Requires: PC powered on at 17:30, TrueData subscription active in .env.
rem ============================================================================
cd /d "%~dp0.."
set PYTHONWARNINGS=ignore::DeprecationWarning
set LOG=logs\backfill\nightly_%date:~-4%%date:~3,2%%date:~0,2%.log
if not exist logs\backfill mkdir logs\backfill

(
echo ===== NIGHTLY TOPUP START %date% %time% =====

rem --- 1. Docker + DB self-heal ---
docker info >nul 2>&1
if errorlevel 1 (
  echo starting Docker Desktop...
  start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"
  for /l %%i in (1,1,30) do (
    timeout /t 10 /nobreak >nul
    docker info >nul 2>&1 && goto :dockerup
  )
  echo FATAL: docker engine did not start & goto :end
)
:dockerup
docker start docker-timescaledb-1 >nul 2>&1
for /l %%i in (1,1,12) do (
  docker exec docker-timescaledb-1 pg_isready -U postgres >nul 2>&1 && goto :pgup
  timeout /t 5 /nobreak >nul
)
echo FATAL: local postgres not ready & goto :end
:pgup

rem --- 2. clear recent ledger units so the last days re-fetch (upsert-safe) ---
docker exec docker-timescaledb-1 psql -U postgres -d oi -c "DELETE FROM oi_backfill_progress WHERE (split_part(unit_key,':',2) ~ '^[0-9]{6}$' AND to_date(split_part(unit_key,':',2),'YYMMDD') >= current_date - 7) OR (split_part(unit_key,':',2) IN ('IDX','FUT') AND to_date(split_part(unit_key,':',3),'YYMMDD') >= current_date - 35);"

rem --- 3. pulls (chains + index/futures, then the tick window) ---
python -u scripts\truedata_backfill.py pull --symbol NIFTY --include-live-days 2>&1
python -u scripts\truedata_backfill.py pull --symbol SENSEX --include-live-days 2>&1
python -u scripts\truedata_backfill.py pull-ticks --symbol NIFTY 2>&1
python -u scripts\truedata_backfill.py pull-ticks --symbol SENSEX 2>&1

rem --- 4. dump the last 5 days (bars + ticks) ---
docker exec docker-timescaledb-1 psql -U postgres -d oi -c "\copy (SELECT * FROM oi_archive_bars WHERE (ts AT TIME ZONE 'Asia/Kolkata')::date >= current_date - 5) TO STDOUT WITH (FORMAT csv)" > logs\backfill\transfer\nightly_bars.csv
docker exec docker-timescaledb-1 psql -U postgres -d oi -c "\copy (SELECT * FROM oi_archive_ticks WHERE (ts AT TIME ZONE 'Asia/Kolkata')::date >= current_date - 5) TO STDOUT WITH (FORMAT csv)" > logs\backfill\transfer\nightly_ticks.csv

rem --- 5. ship + upsert on VPS ---
scp -q logs\backfill\transfer\nightly_bars.csv logs\backfill\transfer\nightly_ticks.csv root@oialgo.tech:~/
ssh root@oialgo.tech "tr -d '\r' < ~/nightly_bars.csv | grep -v '^$' | docker exec -i docker-timescaledb-1 psql -U postgres -d oi -c \"CREATE TEMP TABLE _b (LIKE oi_archive_bars INCLUDING DEFAULTS); COPY _b FROM STDIN WITH (FORMAT csv); INSERT INTO oi_archive_bars SELECT * FROM _b ON CONFLICT (ts,token) DO UPDATE SET open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, close=EXCLUDED.close, volume=EXCLUDED.volume, volume_cum=EXCLUDED.volume_cum, oi=EXCLUDED.oi, underlying=COALESCE(EXCLUDED.underlying, oi_archive_bars.underlying);\" && tr -d '\r' < ~/nightly_ticks.csv | grep -v '^$' | docker exec -i docker-timescaledb-1 psql -U postgres -d oi -c \"CREATE TEMP TABLE _t (LIKE oi_archive_ticks INCLUDING DEFAULTS); COPY _t FROM STDIN WITH (FORMAT csv); INSERT INTO oi_archive_ticks SELECT * FROM _t ON CONFLICT (ts,token) DO UPDATE SET ltp=EXCLUDED.ltp, volume=EXCLUDED.volume, oi=EXCLUDED.oi, bid=EXCLUDED.bid, bidqty=EXCLUDED.bidqty, ask=EXCLUDED.ask, askqty=EXCLUDED.askqty;\" && rm ~/nightly_bars.csv ~/nightly_ticks.csv"

rem --- 6. validate on VPS + freshness check ---
ssh root@oialgo.tech "cd ~/nifty-oi && ~/.venvs/oi/bin/python scripts/truedata_backfill.py validate 2>/dev/null | grep -E '^== ' ; docker exec docker-timescaledb-1 psql -U postgres -d oi -t -c \"SELECT symbol, max((ts AT TIME ZONE 'Asia/Kolkata')::date) FROM oi_snapshots_unified WHERE symbol IN ('NIFTY','SENSEX') GROUP BY 1;\""

echo ===== NIGHTLY TOPUP END %date% %time% =====
) >> %LOG% 2>&1
:end
