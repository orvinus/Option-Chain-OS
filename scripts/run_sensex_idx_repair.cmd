@echo off
rem SENSEX index/futures re-pull (spot-gap repair) — chains stay untouched.
cd /d "%~dp0.."
set PYTHONWARNINGS=ignore::DeprecationWarning
(
python -u scripts\truedata_backfill.py pull --symbol SENSEX --include-live-days --dry-run 2>&1
echo === SENSEX IDX REPAIR FINISHED ===
) >> logs\backfill\gapday_repair.log
