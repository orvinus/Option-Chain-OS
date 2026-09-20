@echo off
rem Repair pull: vendor data for the 18 partial dev-recording days + index gaps.
cd /d "%~dp0.."
set PYTHONWARNINGS=ignore::DeprecationWarning
(
python -u scripts\truedata_backfill.py pull --symbol NIFTY --include-live-days 2>&1
python -u scripts\truedata_backfill.py validate 2>&1
echo === GAPDAY REPAIR FINISHED ===
) >> logs\backfill\gapday_repair.log
