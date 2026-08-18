@echo off
setlocal
set "PORT=%~1"
set "PROFILE_NAME=%~2"
if "%PORT%"=="" set "PORT=9222"
if "%PROFILE_NAME%"=="" set "PROFILE_NAME=cdp-main"
set "CHROME_EXE=C:\Program Files\Google\Chrome\Application\chrome.exe"
set "PROFILE_DIR=%~dp0data\profiles\%PROFILE_NAME%"
if not exist "%CHROME_EXE%" (
  echo Khong tim thay Chrome tai "%CHROME_EXE%"
  exit /b 1
)
start "IK Chrome CDP" "%CHROME_EXE%" --remote-debugging-port=%PORT% --user-data-dir="%PROFILE_DIR%" https://ik.playfun.vn/play-game
echo Chrome CDP dang dung cong %PORT%, profile %PROFILE_NAME%.

