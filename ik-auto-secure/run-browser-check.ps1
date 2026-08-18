<#
.SYNOPSIS
Prepares IK Auto Secure, validates the browser-worker foundation, then opens
the dashboard for an authorised manual Chrome-profile check.

.EXAMPLE
.\run-browser-check.ps1
.\run-browser-check.ps1 -NoDashboard
#>

[CmdletBinding()]
param(
    [switch]$NoDashboard
)

$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $PSCommandPath
$setupScript = Join-Path $projectDir "scripts\setup.ps1"
$python = Join-Path $projectDir ".venv\Scripts\python.exe"
$config = Join-Path $projectDir "config.json"
$env:PYTHONPATH = Join-Path $projectDir "src"

if (-not (Test-Path -LiteralPath $setupScript)) {
    throw "Không tìm thấy setup script: $setupScript"
}

Write-Host "[1/4] Chuẩn bị môi trường..." -ForegroundColor Cyan
& $setupScript

if (-not (Test-Path -LiteralPath $python)) {
    throw "Không tìm thấy Python environment: $python"
}
if (-not (Test-Path -LiteralPath $config)) {
    throw "Không tìm thấy config.json. Kiểm tra config.example.json."
}

Write-Host "[2/4] Kiểm tra Chrome và cấu hình..." -ForegroundColor Cyan
& $python -m ik_chrome_auto --config $config doctor

Write-Host "[3/4] Chạy browser-session foundation tests..." -ForegroundColor Cyan
& $python -m pytest tests/test_browser_worker.py -q

if ($NoDashboard) {
    Write-Host "[4/4] Hoàn tất. Dashboard không được mở do -NoDashboard." -ForegroundColor Green
    exit 0
}

Write-Host "[4/4] Mở dashboard để kiểm tra Chrome profile..." -ForegroundColor Cyan
& $python -m ik_chrome_auto --config $config ui
