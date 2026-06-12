@echo off
REM ===========================================================================
REM  NIFTY OI Platform - ONE-CLICK setup + run for a fresh Windows machine.
REM
REM  Unzip this product folder anywhere, then double-click THIS file.
REM  It installs Docker (if missing), then starts the whole system:
REM  database + backend + frontend, and opens the dashboard automatically.
REM
REM  You may see a "User Account Control" prompt - click Yes (admin is needed
REM  to install Docker / WSL2 the first time only).
REM ===========================================================================
title NIFTY OI Platform - Setup and Run
powershell -NoExit -ExecutionPolicy Bypass -File "%~dp0scripts\bootstrap.ps1"
