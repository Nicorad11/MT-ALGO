@echo off
rem Opus Trader: double-click to start the bot and open the app.
rem   start.bat --setup-only   only prepare the Python environment (used by check.bat and flips.bat)
setlocal
cd /d "%~dp0"
title Opus Trader
set "PYTHONUTF8=1"
call :setup
if errorlevel 1 exit /b 1
if /i "%~1"=="--setup-only" exit /b 0
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
rem An existing environment must be 64-bit Python 3.11 or newer (MetaTrader5 is 64-bit only).
rem No %%VARIABLES%% inside parenthesised blocks here (cmd expands those before the block runs),
rem and every step tests errorlevel on its own line rather than chaining commands.
if not exist ".venv\Scripts\python.exe" goto :makeenv
".venv\Scripts\python.exe" -c "import struct,sys; sys.exit(0 if struct.calcsize('P')==8 and sys.version_info>=(3,11) else 1)" >nul 2>nul
if errorlevel 1 goto :badvenv
goto :deps

:makeenv
call :findpython
if errorlevel 1 goto :nopython
echo Creating the Python environment with %PY% (first run only)...
%PY% -m venv .venv
if errorlevel 1 goto :nopython

:deps
rem Install dependencies on the first run and whenever requirements.txt changes.
".venv\Scripts\python.exe" -c "import sys; sys.exit(open('requirements.txt','rb').read() != open('.venv/requirements.installed','rb').read())" >nul 2>nul
if not errorlevel 1 exit /b 0
echo Installing dependencies (about a minute)...
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
if errorlevel 1 goto :pipfail
copy /y requirements.txt .venv\requirements.installed >nul
exit /b 0

:findpython
rem Prefer 64-bit Python 3.13, 3.12, 3.11, then 3.14 through the py launcher; fall back to python on PATH.
set "PY="
for %%V in (3.13 3.12 3.11 3.14) do if not defined PY call :trypython py -%%V-64
if not defined PY call :trypython python
if not defined PY exit /b 1
exit /b 0

:trypython
rem Use this command only if it runs a 64-bit Python 3.11 or newer.
%* -c "import struct,sys; sys.exit(0 if struct.calcsize('P')==8 and sys.version_info>=(3,11) else 1)" >nul 2>nul
if errorlevel 1 exit /b 1
set "PY=%*"
exit /b 0

:badvenv
echo.
echo  The .venv folder was made with a 32-bit or too old Python.
echo  Delete the .venv folder, install 64-bit Python 3.13 from python.org, and start again.
pause
exit /b 1

:nopython
echo.
echo  64-bit Python 3.11 to 3.14 is required: https://www.python.org/downloads/windows/
echo  Choose the "Windows installer (64-bit)" and tick "Add python.exe to PATH", then start again.
pause
exit /b 1

:pipfail
echo.
echo  Installing dependencies failed. Check your internet connection and try again.
pause
exit /b 1
