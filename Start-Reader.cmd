@echo off
setlocal
cd /d "%~dp0"
title Paper Reader
if not exist ".venv\Scripts\python.exe" goto setup
if not exist "web\dist\index.html" goto setup
goto run
:setup
call Setup.cmd
if errorlevel 1 exit /b 1
:run
".venv\Scripts\python.exe" -B -m reader %*
if errorlevel 1 pause
