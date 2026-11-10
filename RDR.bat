@echo off
title RDR - Radio Distribution Room
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (set PY=py -3) else (set PY=python)

if not exist ".venv\Scripts\python.exe" (
  echo Creating Python environment...
  %PY% -m venv .venv || goto :nopython
)
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt
".venv\Scripts\python.exe" run.py %*
if errorlevel 1 pause
exit /b

:nopython
echo.
echo Python 3.11 or newer was not found. Install it from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" during setup, then run RDR.bat again.
pause
