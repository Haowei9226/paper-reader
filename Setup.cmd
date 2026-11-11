@echo off
setlocal
cd /d "%~dp0"
title Paper Reader Setup
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv --system-site-packages .venv
  if errorlevel 1 py -3 -m venv --without-pip --system-site-packages .venv
)
".venv\Scripts\python.exe" -m pip install --cache-dir .cache\pip -r requirements.lock
if errorlevel 1 goto failed
pushd web
call npm.cmd ci --cache ..\.cache\npm --no-audit --no-fund
if errorlevel 1 goto nodefailed
call npm.cmd run build
if errorlevel 1 goto nodefailed
popd
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Create-Shortcut.ps1"
if errorlevel 1 goto failed
echo Setup complete. Open the shortcut in this folder.
exit /b 0
:nodefailed
popd
:failed
echo Setup failed. Please check the error above.
pause
exit /b 1
