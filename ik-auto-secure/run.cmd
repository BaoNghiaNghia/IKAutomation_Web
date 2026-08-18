@echo off
setlocal EnableExtensions EnableDelayedExpansion
set "PROJECT_DIR=%~dp0"
set "VENV_PYTHON=%PROJECT_DIR%.venv\Scripts\python.exe"
set "PYTHONPATH=%PROJECT_DIR%src"
set "PYTHONUTF8=1"

echo [1/5] Checking Python environment...
if exist "%VENV_PYTHON%" (
  "%VENV_PYTHON%" -c "import sys" >nul 2>nul
  if not errorlevel 1 goto :check_dependencies
  echo Existing .venv cannot start. Recreating it...
  rmdir /s /q "%PROJECT_DIR%.venv" || goto :failed
)

call :find_python
if not defined BOOTSTRAP_PYTHON (
  echo Python 3.11-3.13 was not found. Installing Python 3.13 with winget...
  where winget >nul 2>nul || goto :missing_winget
  winget install --id Python.Python.3.13 --exact --source winget --accept-package-agreements --accept-source-agreements || goto :failed
  call :find_python
  if not defined BOOTSTRAP_PYTHON goto :python_install_not_found
)
echo Creating .venv for the first run...
%BOOTSTRAP_PYTHON% -m venv "%PROJECT_DIR%.venv" || goto :failed

:check_dependencies
echo [2/5] Checking required packages...
"%VENV_PYTHON%" -c "import customtkinter, playwright, pytest" >nul 2>nul
if not errorlevel 1 goto :check_config
echo Installing required packages for the first run...
"%VENV_PYTHON%" -m pip install --disable-pip-version-check "customtkinter>=5.2,<6" "playwright>=1.54,<2" "pytest>=8,<10" || goto :failed

:check_config
echo [3/5] Checking config...
if exist "%PROJECT_DIR%config.json" goto :doctor
copy /y "%PROJECT_DIR%config.example.json" "%PROJECT_DIR%config.json" >nul || goto :failed
echo Created config.json from config.example.json.

:doctor
echo [4/5] Running Doctor and browser-session checks...
"%VENV_PYTHON%" -m ik_chrome_auto.doctor --config "%PROJECT_DIR%config.json" || goto :failed
"%VENV_PYTHON%" -m pytest "%PROJECT_DIR%tests\test_browser_worker.py" "%PROJECT_DIR%tests\test_credential_store.py" -q || goto :failed

echo [5/5] Opening dashboard...
"%VENV_PYTHON%" -m ik_chrome_auto --config "%PROJECT_DIR%config.json" ui
if errorlevel 1 goto :failed
exit /b 0

:failed
echo.
echo The check or dashboard stopped with an error. Review the message above.
pause
exit /b 1

:missing_winget
echo winget is not available, so Python could not be installed automatically.
echo Install Python 3.13 from https://www.python.org/downloads/ then run this file again.
pause
exit /b 1

:python_install_not_found
echo Python was installed but Windows has not exposed it to this process yet.
echo Close this window and run run.cmd again.
pause
exit /b 1

:find_python
set "BOOTSTRAP_PYTHON="
for %%V in (3.13 3.12 3.11) do (
  if not defined BOOTSTRAP_PYTHON (
    py -%%V -c "import sys; raise SystemExit(0 if (3,11) ^<= sys.version_info[:2] ^< (3,14) else 1)" >nul 2>nul
    if !errorlevel! equ 0 set "BOOTSTRAP_PYTHON=py -%%V"
  )
)
if not defined BOOTSTRAP_PYTHON (
  python -c "import sys; raise SystemExit(0 if (3,11) ^<= sys.version_info[:2] ^< (3,14) else 1)" >nul 2>nul
  if !errorlevel! equ 0 set "BOOTSTRAP_PYTHON=python"
)
if not defined BOOTSTRAP_PYTHON if exist "%LocalAppData%\Programs\Python\Python313\python.exe" set "BOOTSTRAP_PYTHON="%LocalAppData%\Programs\Python\Python313\python.exe""
exit /b 0
