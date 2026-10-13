@echo off
setlocal
rem Offline support snapshot for the ACTUAL packaged IK Auto configuration.
rem Does not open Chrome or attach CDP; only reads local JSONL and writes
rem release\IK Auto\data\logs\support-summary.json.
set "ROOT=%~dp0.."
set "PYTHONPATH=%ROOT%\src"
"%ROOT%\.venv\Scripts\python.exe" -m ik_chrome_auto --config "%ROOT%\release\IK Auto\config.json" diagnostic-summary
exit /b %ERRORLEVEL%
