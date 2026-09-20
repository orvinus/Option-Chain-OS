@echo off
rem Detached runner for the remaining TrueData collection steps.
rem Launched via Start-Process so it survives independently of any IDE/session.
cd /d "%~dp0.."
set PYTHONWARNINGS=ignore::DeprecationWarning
if not exist logs\backfill mkdir logs\backfill
(
python -u scripts\truedata_backfill.py pull-ticks --symbol NIFTY 2>&1
python -u scripts\truedata_backfill.py pull-ticks --symbol SENSEX 2>&1
python -u scripts\truedata_backfill.py pull-eod --symbol NIFTY 2>&1
python -u scripts\truedata_backfill.py pull-eod --symbol SENSEX 2>&1
python -u scripts\truedata_backfill.py pull-flows --rps 0.9 2>&1
python -u scripts\truedata_backfill.py pull-news --rps 0.9 2>&1
python -u scripts\truedata_backfill.py validate 2>&1
echo === SEQUENCE FINISHED ===
) >> logs\backfill\remaining.log
