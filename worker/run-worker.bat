@echo off
setlocal
cd /d "%~dp0"
title ICE MS Project Worker

rem --- 1. Is Python installed? -------------------------------------------
where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found on this computer.
  echo Install Python 3.10 or newer from https://www.python.org/downloads/
  echo IMPORTANT: tick "Add python.exe to PATH" during install, then run this again.
  pause
  exit /b 1
)

rem --- 2. Is the settings file in place? ---------------------------------
if not exist worker.env (
  echo worker.env is missing.
  echo Copy worker.env.example to worker.env, paste your token from
  echo ICE Settings ^> Microsoft Project connector, then run this again.
  pause
  exit /b 1
)

rem --- 3. Install the required packages if they are missing --------------
python -c "import win32com.client, requests" >nul 2>nul
if errorlevel 1 (
  echo Installing required Python packages ^(one-time^)...
  python -m pip install --disable-pip-version-check -r requirements.txt
  if errorlevel 1 (
    echo Package install failed. Check your internet connection and try again.
    pause
    exit /b 1
  )
)

rem --- 4. Run the worker ---------------------------------------------------
echo Starting the ICE Microsoft Project worker. Press Ctrl+C to stop it.
python msp_worker.py %*
pause
