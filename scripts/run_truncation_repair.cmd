@echo off
rem Detached repair for the vendor's silent ~3.8k-row getbars response cap:
rem 1) clear ledger rows for capped option tokens (>=3500 bars) + all IDX/FUT chunks
rem 2) re-pull both symbols (client now auto-splits ranges that hit the cap)
rem 3) re-stamp underlying from the completed index series
rem 4) re-validate
cd /d "%~dp0.."
set PYTHONWARNINGS=ignore::DeprecationWarning
(
echo === truncation repair started ===
docker exec docker-timescaledb-1 psql -U postgres -d oi -c "DELETE FROM oi_backfill_progress WHERE unit_key IN (SELECT replace(token,'td:','') FROM (SELECT token FROM oi_archive_bars WHERE option_type IN ('CE','PE') GROUP BY token HAVING count(*) >= 3500) t) OR unit_key ~ '^(NIFTY|SENSEX):(IDX|FUT):';"
python -u scripts\truedata_backfill.py pull --symbol NIFTY 2>&1
python -u scripts\truedata_backfill.py pull --symbol SENSEX 2>&1
echo --- re-stamping underlying from index series ---
docker exec docker-timescaledb-1 psql -U postgres -d oi -c "UPDATE oi_archive_bars o SET underlying = i.close FROM oi_archive_bars i WHERE i.symbol = o.symbol AND i.option_type = 'IDX' AND i.ts = o.ts AND o.option_type IN ('CE','PE') AND o.underlying IS NULL AND i.close IS NOT NULL;"
python -u scripts\truedata_backfill.py validate 2>&1
echo === truncation repair finished ===
) >> logs\backfill\repair_truncation.log
