@echo off
rem Detached post-close top-up: re-fetch today's live contracts (ledger rows for
rem them were cleared first), refresh the 5-day tick window, re-validate.
cd /d "%~dp0.."
set PYTHONWARNINGS=ignore::DeprecationWarning
if not exist logs\backfill mkdir logs\backfill
(
python -u scripts\truedata_backfill.py pull --symbol NIFTY 2>&1
python -u scripts\truedata_backfill.py pull --symbol SENSEX 2>&1
python -u scripts\truedata_backfill.py pull-ticks --symbol NIFTY 2>&1
python -u scripts\truedata_backfill.py pull-ticks --symbol SENSEX 2>&1
python -u scripts\truedata_backfill.py validate 2>&1
echo === TOPUP FINISHED ===
) >> logs\backfill\topup_today.log
