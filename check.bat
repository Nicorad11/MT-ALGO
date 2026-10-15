@echo off
rem Opus Trader check: verifies the MT5 terminal and account, then runs one dry-run
rem decision cycle (real snapshot, real Opus call, order_check). Nothing is sent to MT5.
setlocal
cd /d "%~dp0"
title Opus Trader check
if not exist ".venv\Scripts\python.exe" (
  echo Run start.bat once first to set up the Python environment.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m opus_trader check %*
echo.
pause
