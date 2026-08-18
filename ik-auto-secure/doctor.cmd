@echo off
setlocal
set "PROJECT_DIR=%~dp0"
set "PYTHONPATH=%PROJECT_DIR%src"
set "PYTHONUTF8=1"
"%PROJECT_DIR%.venv\Scripts\python.exe" -m ik_chrome_auto.doctor --config "%PROJECT_DIR%config.json"
if errorlevel 1 pause
