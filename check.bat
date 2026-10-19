@echo off
rem Opus Trader check: verifies the MT5 terminal and account, then runs one dry-run
rem decision cycle (real snapshot, real Opus call, order_check). Nothing is sent to MT5.
setlocal
cd /d "%~dp0"
title Opus Trader check
set "PYTHONUTF8=1"
call start.bat --setup-only
if not "%errorlevel%"=="0" exit /b 1
".venv\Scripts\python.exe" -m opus_trader check %*
echo.
pause
