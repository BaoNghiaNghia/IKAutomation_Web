[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$NoDesktopShortcut,
    [switch]$Archive
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$icon = Join-Path $projectRoot 'src\ik_chrome_auto\assets\ik_auto.ico'
$assets = Join-Path $projectRoot 'src\ik_chrome_auto\assets'
$launcher = Join-Path $projectRoot 'src\ik_chrome_auto\launcher.py'
$releaseRoot = Join-Path $projectRoot 'release'
$buildRoot = Join-Path $projectRoot 'build'
$applicationName = 'IK Auto'

# These modules are bundled by ``--collect-all qfluentwidgets`` although the
# dashboard only uses Qt Widgets, SVG icons and XML. Collecting them inflates a
# release by tens of MB and makes startup less predictable on low-end PCs.
$excludedModules = @(
    'qfluentwidgets.multimedia',
    'PySide6.QtMultimedia',
    'PySide6.QtMultimediaWidgets',
    'PySide6.QtPdf',
    'PySide6.QtPdfWidgets',
    'PySide6.QtQml',
    'PySide6.QtQuick',
    'PySide6.QtQuick3D',
    'PySide6.QtQuickControls2',
    'PySide6.QtQuickWidgets',
    'PySide6.QtVirtualKeyboard',
    'PySide6.QtWebChannel',
    'PySide6.QtWebEngineCore',
    'PySide6.QtWebEngineQuick',
    'PySide6.QtWebEngineWidgets'
)

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
    $pyInstallerArgs = @(
        '--noconfirm', '--clean', '--windowed', '--onedir', '--noupx',
        '--name', $applicationName,
        '--icon', $icon,
        '--paths', (Join-Path $projectRoot 'src'),
        '--distpath', $releaseRoot,
        '--workpath', $buildRoot,
        '--specpath', $buildRoot,
        '--optimize', '2',
        '--add-data', "$(Join-Path $projectRoot 'config.example.json');.",
        '--add-data', "$assets;ik_chrome_auto/assets",
        # Resource files are needed by Fluent widgets; importing the package
        # itself lets PyInstaller collect only the Python modules actually
        # used by the dashboard.
        '--collect-data', 'qfluentwidgets',
        '--hidden-import', 'winrt.windows.foundation',
        '--hidden-import', 'winrt.windows.security.credentials.ui'
    )
    foreach ($module in $excludedModules) {
        $pyInstallerArgs += @('--exclude-module', $module)
    }
    $pyInstallerArgs += $launcher
    & $python -m PyInstaller @pyInstallerArgs
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed to create the application.' }
    $applicationDir = Join-Path $releaseRoot $applicationName
    $applicationExe = Join-Path $applicationDir "$applicationName.exe"
    if (-not (Test-Path -LiteralPath $applicationExe)) { throw "Build file not found: $applicationExe" }

    # Farm matching uses still images only. OpenCV's bundled FFmpeg bridge is
    # only required for VideoCapture/VideoWriter and otherwise costs ~30 MB.
    $opencvDirectory = Join-Path $applicationDir '_internal\cv2'
    if (Test-Path -LiteralPath $opencvDirectory) {
        Get-ChildItem -LiteralPath $opencvDirectory -Filter 'opencv_videoio_ffmpeg*.dll' -File |
            Remove-Item -Force
    }

    $buildSize = (Get-ChildItem -LiteralPath $applicationDir -Recurse -File |
        Measure-Object -Property Length -Sum).Sum
    $sizeText = "Build size: {0:N1} MB" -f ($buildSize / 1MB)
    Set-Content -LiteralPath (Join-Path $applicationDir 'BUILD-SIZE.txt') -Value @(
        'IK Auto compact Windows release',
        $sizeText,
        'Excluded: unused Qt multimedia/PDF/QML/WebEngine modules and OpenCV FFmpeg video codec.',
        'Browser profiles, logs and screenshots are created outside the release folder for new installations.'
    ) -Encoding utf8

    if ($Archive) {
        $archivePath = Join-Path $releaseRoot 'IK-Auto-portable.zip'
        if (Test-Path -LiteralPath $archivePath) { Remove-Item -LiteralPath $archivePath -Force }
        Compress-Archive -LiteralPath $applicationDir -DestinationPath $archivePath -CompressionLevel Optimal
        $archiveSize = (Get-Item -LiteralPath $archivePath).Length
        Write-Host ("Portable archive: {0} ({1:N1} MB)" -f $archivePath, ($archiveSize / 1MB)) -ForegroundColor Cyan
    }
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
    Write-Host "Build complete: $applicationExe ($sizeText)" -ForegroundColor Green
    if (-not $NoDesktopShortcut) { Write-Host "Desktop shortcut created: $applicationName" -ForegroundColor Green }
} finally { Pop-Location }
