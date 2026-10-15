@echo off
rem Prints the last 20 Trend Signals flips (UTC bar open times) to compare with TradingView.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run start.bat once first to set up the Python environment.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m opus_trader flips %*
echo.
pause
