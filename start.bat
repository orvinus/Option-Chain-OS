@echo off
REM One-click launcher for the trading-algo stack.
REM Double-click this file (or pin it to taskbar / make a Desktop shortcut).
powershell -NoExit -ExecutionPolicy Bypass -File "%~dp0scripts\start-all.ps1"
