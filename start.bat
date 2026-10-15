@echo off
rem Opus Trader: double-click to start the bot and open the app.
setlocal
cd /d "%~dp0"
title Opus Trader
call :setup || exit /b 1
if not exist ".env" (
  echo.
  echo  No .env file found.
  echo  Copy .env.example to .env and put your ANTHROPIC_API_KEY in it, then start again.
  echo.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m opus_trader %*
if errorlevel 1 pause
exit /b

:setup
where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
if not exist ".venv\Scripts\python.exe" (
  echo Creating the Python environment ^(first run only^)...
  %PY% -m venv .venv || goto :nopython
)
rem Install dependencies on the first run and whenever requirements.txt changes.
fc /b requirements.txt .venv\requirements.installed >nul 2>nul
if errorlevel 1 (
  echo Installing dependencies...
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || goto :pipfail
  copy /y requirements.txt .venv\requirements.installed >nul
)
exit /b 0

:nopython
echo.
echo  Python 3.11 to 3.13 ^(64-bit^) is required: https://www.python.org/downloads/windows/
echo  Tick "Add python.exe to PATH" in the installer, then start again.
pause
exit /b 1

:pipfail
echo.
echo  Installing dependencies failed. Check your internet connection and try again.
pause
exit /b 1
