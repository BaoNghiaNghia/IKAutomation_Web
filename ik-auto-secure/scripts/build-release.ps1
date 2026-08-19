[CmdletBinding()]
param([switch]$SkipTests, [switch]$NoDesktopShortcut)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$icon = Join-Path $projectRoot 'src\ik_chrome_auto\assets\ik_auto.ico'
$assets = Join-Path $projectRoot 'src\ik_chrome_auto\assets'
$launcher = Join-Path $projectRoot 'src\ik_chrome_auto\launcher.py'
$releaseRoot = Join-Path $projectRoot 'release'
$buildRoot = Join-Path $projectRoot 'build'
$applicationName = 'IK Auto'

if (-not (Test-Path -LiteralPath $python)) { throw 'Missing .venv. Run run.cmd once before building.' }
if (-not (Test-Path -LiteralPath $icon)) { throw "Missing icon: $icon" }

Push-Location $projectRoot
try {
    & $python -m pip install --disable-pip-version-check 'pyinstaller>=6.11,<7'
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller installation failed.' }
    if (-not $SkipTests) {
        & $python -m pytest -q
        if ($LASTEXITCODE -ne 0) { throw 'Tests failed; build stopped.' }
    }
    foreach ($path in @($releaseRoot, $buildRoot)) {
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force }
    }
    & $python -m PyInstaller --noconfirm --clean --windowed --onedir `
        --name $applicationName --icon $icon --paths (Join-Path $projectRoot 'src') `
        --distpath $releaseRoot --workpath $buildRoot `
        --add-data "$(Join-Path $projectRoot 'config.example.json');." `
        --add-data "$assets;ik_chrome_auto/assets" `
        --collect-all qfluentwidgets `
        --hidden-import winrt.windows.foundation `
        --hidden-import winrt.windows.security.credentials.ui $launcher
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed to create the application.' }
    $applicationDir = Join-Path $releaseRoot $applicationName
    $applicationExe = Join-Path $applicationDir "$applicationName.exe"
    if (-not (Test-Path -LiteralPath $applicationExe)) { throw "Build file not found: $applicationExe" }
    if (-not $NoDesktopShortcut) {
        $shell = New-Object -ComObject WScript.Shell
        $desktop = $shell.SpecialFolders.Item('Desktop')
        if ([string]::IsNullOrWhiteSpace($desktop) -or -not (Test-Path -LiteralPath $desktop)) {
            $oneDriveDesktop = Join-Path $env:OneDrive 'Desktop'
            $desktop = if ($env:OneDrive -and (Test-Path -LiteralPath $oneDriveDesktop)) { $oneDriveDesktop } else { Join-Path $env:USERPROFILE 'Desktop' }
        }
        if (-not (Test-Path -LiteralPath $desktop)) { New-Item -ItemType Directory -Path $desktop -Force | Out-Null }
        $shortcut = $shell.CreateShortcut((Join-Path $desktop "$applicationName.lnk"))
        $shortcut.TargetPath = $applicationExe
        $shortcut.WorkingDirectory = $applicationDir
        $shortcut.IconLocation = "$applicationExe,0"
        $shortcut.Description = 'IK Auto – Browser Control'
        $shortcut.Save()
    }
    Write-Host "Build complete: $applicationExe" -ForegroundColor Green
    if (-not $NoDesktopShortcut) { Write-Host "Desktop shortcut created: $applicationName" -ForegroundColor Green }
} finally { Pop-Location }
