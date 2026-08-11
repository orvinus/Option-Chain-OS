@echo off
rem Reaper-proof chain: wait for the SENSEX idx repair marker, then stamp+dump.
cd /d "%~dp0.."
:wait
findstr /C:"SENSEX IDX REPAIR FINISHED" logs\backfill\gapday_repair.log >nul 2>&1
if errorlevel 1 (
  timeout /t 30 /nobreak >nul
  goto wait
)
call scripts\run_spot_stamp_and_dump.cmd
