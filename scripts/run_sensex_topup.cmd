@echo off
cd /d "%~dp0.."
set PYTHONWARNINGS=ignore::DeprecationWarning
(
python -u scripts\truedata_backfill.py pull --symbol SENSEX --include-live-days 2>&1
docker exec docker-timescaledb-1 psql -U postgres -d oi -c "\copy (SELECT * FROM oi_archive_bars WHERE symbol='SENSEX' AND (ts AT TIME ZONE 'Asia/Kolkata')::date >= '2026-08-11') TO STDOUT WITH (FORMAT csv)" > logs\backfill\transfer\sensex_topup.csv
echo === SENSEX TOPUP DONE ===
) >> logs\backfill\gapday_repair.log 2>&1
