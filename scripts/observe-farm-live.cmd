@echo off
setlocal
rem Pure log observer; never launches or controls Chrome/IK Auto.
set "ROOT=%~dp0.."
"%ROOT%\.venv\Scripts\python.exe" "%ROOT%\src\ik_chrome_auto\farm_observer.py" --logs "%ROOT%\release\IK Auto\data\logs" %*
exit /b %ERRORLEVEL%