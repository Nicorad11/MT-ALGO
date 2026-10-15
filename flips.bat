@echo off
rem Prints the last 20 Trend Signals flips (UTC bar open times) to compare with TradingView.
setlocal
cd /d "%~dp0"
title Opus Trader flips
set "PYTHONUTF8=1"
call start.bat --setup-only || exit /b 1
".venv\Scripts\python.exe" -m opus_trader flips %*
echo.
pause
