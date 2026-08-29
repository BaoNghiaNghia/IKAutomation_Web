@echo off
chcp 65001 >nul
if /i "%~1"=="--locked" goto :locked

set "IK_AUTO_RUN_SCRIPT=%~f0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$created = $false; $mutex = [System.Threading.Mutex]::new($true, 'Local\IKAutoBrowserDashboard', [ref]$created); if (-not $created) { $mutex.Dispose(); Write-Host ''; Write-Host 'Ứng dụng IK Auto đang được mở. Hãy sử dụng dashboard hiện tại.' -ForegroundColor Yellow; Read-Host 'Nhấn Enter để đóng cửa sổ này'; exit 0 }; $exitCode = 1; try { & $env:ComSpec /d /c $env:IK_AUTO_RUN_SCRIPT --locked; $exitCode = $LASTEXITCODE } finally { $mutex.ReleaseMutex(); $mutex.Dispose() }; exit $exitCode"
exit /b %errorlevel%

:locked
shift /1
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
  echo Python 3.11-3.13 was not found. Installing Python 3.13...
  where winget >nul 2>nul
  if !errorlevel! equ 0 (
    echo Trying Windows Package Manager...
    winget install --id Python.Python.3.13 --exact --source winget --accept-package-agreements --accept-source-agreements
  ) else (
    echo winget is not available. Using the official python.org installer instead...
  )
  call :find_python
  if not defined BOOTSTRAP_PYTHON (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_DIR%scripts\install-python.ps1" -MinorVersion 3.13
    if !errorlevel! neq 0 goto :python_install_failed
    call :find_python
  )
  if not defined BOOTSTRAP_PYTHON goto :python_install_not_found
)
echo Creating .venv for the first run...
%BOOTSTRAP_PYTHON% -m venv "%PROJECT_DIR%.venv" || goto :failed

:check_dependencies
echo [2/5] Checking required packages...
"%VENV_PYTHON%" -c "import PySide6, playwright, pytest, qfluentwidgets, qrcode, cv2, winrt.windows.foundation, winrt.windows.security.credentials.ui" >nul 2>nul
if not errorlevel 1 goto :check_config
echo Installing required packages for the first run...
"%VENV_PYTHON%" -m pip install --disable-pip-version-check "PySide6>=6.8,<7" "PySide6-Fluent-Widgets>=1.7,<2" "playwright>=1.54,<2" "qrcode[pil]>=8,<9" "opencv-python-headless>=4.10,<5" "pytest>=8,<10" "winrt-Windows.Foundation>=3.2,<4" "winrt-Windows.Security.Credentials.UI>=3.2,<4" || goto :failed

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
set "IK_AUTO_MINIMIZE_CONSOLE=1"
"%VENV_PYTHON%" -m ik_chrome_auto --config "%PROJECT_DIR%config.json" ui
if errorlevel 1 goto :failed
exit /b 0

:failed
echo.
echo The check or dashboard stopped with an error. Review the message above.
pause
exit /b 1

:python_install_failed
echo Python could not be installed automatically from python.org.
echo Check your internet connection or install Python 3.13 from https://www.python.org/downloads/windows/
pause
exit /b 1

:python_install_not_found
echo Python was installed but Windows has not exposed it to this process yet.
echo Close this window and run IKAutomation_dev.cmd again.
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
for %%V in (313 312 311) do (
  if not defined BOOTSTRAP_PYTHON if exist "%LocalAppData%\Programs\Python\Python%%V\python.exe" set "BOOTSTRAP_PYTHON="%LocalAppData%\Programs\Python\Python%%V\python.exe""
)
exit /b 0
