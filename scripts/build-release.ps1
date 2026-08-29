[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$NoDesktopShortcut,
    [switch]$Archive,
    [switch]$CleanCache
)

$ErrorActionPreference = 'Stop'

# `build.cmd` normally sets code page 65001, but keep direct PowerShell runs
# readable too. Both the console and PowerShell pipeline must agree on UTF-8
# before Vietnamese progress messages are written.
& "$env:SystemRoot\System32\chcp.com" 65001 > $null
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8NoBom
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$icon = Join-Path $projectRoot 'src\ik_chrome_auto\assets\ik_auto.ico'
$assets = Join-Path $projectRoot 'src\ik_chrome_auto\assets'
$launcher = Join-Path $projectRoot 'src\ik_chrome_auto\launcher.py'
$releaseRoot = Join-Path $projectRoot 'release'
$buildCacheRoot = Join-Path $projectRoot '.build-cache'
$pyInstallerWorkRoot = Join-Path $buildCacheRoot 'pyinstaller-work'
$pyInstallerSpecRoot = Join-Path $buildCacheRoot 'pyinstaller-spec'
$pyInstallerDistRoot = Join-Path $buildCacheRoot 'pyinstaller-dist'
$profileSnapshotRoot = Join-Path $buildCacheRoot 'profile-snapshot'
$profileSnapshotManifest = Join-Path $profileSnapshotRoot 'snapshot.sha256'
$applicationName = 'IK Auto'
$devConfig = Join-Path $projectRoot 'config.json'
$devProfiles = Join-Path $projectRoot 'data\profiles'

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

if (-not (Test-Path -LiteralPath $python)) { throw 'Missing .venv. Run IKAutomation_dev.cmd once before building.' }
if (-not (Test-Path -LiteralPath $icon)) { throw "Missing icon: $icon" }

function Invoke-ProfileRobocopy {
    param(
        [string]$Source,
        [string]$Destination,
        [switch]$Mirror
    )

    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    $robocopyArgs = @(
        $Source, $Destination, $(if ($Mirror) { '/MIR' } else { '/E' }),
        '/COPY:DAT', '/DCOPY:DAT', '/R:2', '/W:1', '/XJ',
        '/XD', 'Cache', 'Code Cache', 'GPUCache', 'GrShaderCache',
        'DawnCache', 'ShaderCache', 'Crashpad',
        '/XF', '*.log', '*.tmp', 'LOCK', 'LOCKfile'
    )
    & robocopy @robocopyArgs | Out-Host
    if ($LASTEXITCODE -gt 7) {
        throw "Could not copy development profiles (robocopy exit code $LASTEXITCODE). Close Chrome and build again."
    }
}

function Get-DevelopmentProfileSignature {
    $records = [System.Collections.Generic.List[string]]::new()
    $configItem = Get-Item -LiteralPath $devConfig
    $files = Get-ChildItem -LiteralPath $devProfiles -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -notin @('LOCK', 'LOCKfile') -and
            $_.Extension -notin @('.log', '.tmp') -and
            $_.FullName -notmatch '\\(Cache|Code Cache|GPUCache|GrShaderCache|DawnCache|ShaderCache|Crashpad)\\'
        } |
        Sort-Object FullName
    foreach ($item in @($configItem) + @($files)) {
        $relativePath = if ($item.FullName -eq $configItem.FullName) {
            'config.json'
        } else {
            $item.FullName.Substring($devProfiles.Length).TrimStart('\\')
        }
        $records.Add("$relativePath|$($item.Length)|$($item.LastWriteTimeUtc.Ticks)")
    }
    $bytes = [System.Text.Encoding]::UTF8.GetBytes(($records -join "`n"))
    # SHA256.HashData/Convert.ToHexString are unavailable in Windows
    # PowerShell 5.1's .NET Framework runtime. Keep the build script portable
    # by using the long-standing instance API instead.
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($sha256.ComputeHash($bytes))).Replace('-', '')
    } finally {
        $sha256.Dispose()
    }
}

function Sync-DevProfilesToRelease {
    param([string]$Destination)

    if (-not (Test-Path -LiteralPath $devConfig)) {
        Write-Warning 'Không tìm thấy config.json của bản dev; release sẽ tạo config mẫu ở lần chạy đầu.'
        return
    }
    if (-not (Test-Path -LiteralPath $devProfiles)) {
        Write-Warning 'Không tìm thấy data\profiles của bản dev; chỉ đã sao chép config profile.'
        Copy-Item -LiteralPath $devConfig -Destination (Join-Path $Destination 'config.json') -Force
        return
    }

    $signature = Get-DevelopmentProfileSignature
    $cachedConfig = Join-Path $profileSnapshotRoot 'config.json'
    $cachedProfiles = Join-Path $profileSnapshotRoot 'profiles'
    $cacheHit = (
        (Test-Path -LiteralPath $cachedConfig) -and
        (Test-Path -LiteralPath $cachedProfiles) -and
        (Test-Path -LiteralPath $profileSnapshotManifest) -and
        ((Get-Content -LiteralPath $profileSnapshotManifest -Raw).Trim() -eq $signature)
    )
    if (-not $cacheHit) {
        New-Item -ItemType Directory -Path $profileSnapshotRoot -Force | Out-Null
        Copy-Item -LiteralPath $devConfig -Destination $cachedConfig -Force
        Invoke-ProfileRobocopy -Source $devProfiles -Destination $cachedProfiles -Mirror
        [System.IO.File]::WriteAllText($profileSnapshotManifest, $signature, $utf8NoBom)
        Write-Host 'Profile cache refreshed from development profiles.' -ForegroundColor DarkCyan
    } else {
        Write-Host 'Profile cache hit: development profiles are unchanged.' -ForegroundColor DarkCyan
    }

    $releaseProfiles = Join-Path $Destination 'data\profiles'
    $releaseMarker = Join-Path $Destination '.ik-auto-profile-snapshot'
    $releaseMatchesSnapshot = (
        (Test-Path -LiteralPath (Join-Path $Destination 'config.json')) -and
        (Test-Path -LiteralPath $releaseProfiles) -and
        (Test-Path -LiteralPath $releaseMarker) -and
        ((Get-Content -LiteralPath $releaseMarker -Raw).Trim() -eq $signature)
    )
    if ($releaseMatchesSnapshot) {
        Write-Host 'Release profile cache hit: keeping existing release profiles.' -ForegroundColor DarkCyan
        return
    }

    Copy-Item -LiteralPath $cachedConfig -Destination (Join-Path $Destination 'config.json') -Force
    Invoke-ProfileRobocopy -Source $cachedProfiles -Destination $releaseProfiles
    [System.IO.File]::WriteAllText($releaseMarker, $signature, $utf8NoBom)
    Write-Host 'Release profiles synchronized from the cached development snapshot.' -ForegroundColor DarkCyan
}

function Update-ReleaseApplication {
    param(
        [string]$PackageDirectory,
        [string]$Destination
    )

    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    Get-ChildItem -LiteralPath $PackageDirectory -File | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $Destination -Force
    }
    $packageInternal = Join-Path $PackageDirectory '_internal'
    if (-not (Test-Path -LiteralPath $packageInternal)) {
        throw "Build folder is missing _internal: $PackageDirectory"
    }
    $releaseInternal = Join-Path $Destination '_internal'
    & robocopy $packageInternal $releaseInternal '/MIR' '/COPY:DAT' '/DCOPY:DAT' '/R:2' '/W:1' '/XJ' | Out-Host
    if ($LASTEXITCODE -gt 7) {
        throw "Could not update release application files (robocopy exit code $LASTEXITCODE)."
    }
}

function Test-PyInstallerAvailable {
    # On PowerShell 7, a failed native command can be promoted to a terminating
    # NativeCommandError when ErrorActionPreference is Stop. A missing optional
    # build dependency is expected here and must instead trigger installation.
    $nativePreference = Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue
    $previousNativePreference = if ($nativePreference) { $nativePreference.Value } else { $null }
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        # Windows PowerShell converts Python's ModuleNotFoundError on stderr
        # into a NativeCommandError when the outer script uses Stop.
        $ErrorActionPreference = 'Continue'
        if ($nativePreference) {
            $PSNativeCommandUseErrorActionPreference = $false
        }
        & $python -c "import PyInstaller" 2>$null
        return $LASTEXITCODE -eq 0
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
        if ($nativePreference) {
            $PSNativeCommandUseErrorActionPreference = $previousNativePreference
        }
    }
}

Push-Location $projectRoot
try {
    if ($CleanCache -and (Test-Path -LiteralPath $buildCacheRoot)) {
        Remove-Item -LiteralPath $buildCacheRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Path $pyInstallerWorkRoot, $pyInstallerSpecRoot, $pyInstallerDistRoot -Force | Out-Null

    if (-not (Test-PyInstallerAvailable)) {
        Write-Host 'PyInstaller is missing; installing it now...' -ForegroundColor DarkCyan
        & $python -m pip install --disable-pip-version-check 'pyinstaller>=6.11,<7'
        if ($LASTEXITCODE -ne 0) { throw 'PyInstaller installation failed.' }
    }
    Write-Host "Using PyInstaller cache: $buildCacheRoot" -ForegroundColor DarkCyan
    if (-not $SkipTests) {
        & $python -m pytest -q
        if ($LASTEXITCODE -ne 0) { throw 'Tests failed; build stopped.' }
    }
    $pyInstallerArgs = @(
        '--noconfirm', '--windowed', '--onedir', '--noupx',
        '--name', $applicationName,
        '--icon', $icon,
        '--paths', (Join-Path $projectRoot 'src'),
        '--distpath', $pyInstallerDistRoot,
        '--workpath', $pyInstallerWorkRoot,
        '--specpath', $pyInstallerSpecRoot,
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
    if ($CleanCache) {
        $pyInstallerArgs += '--clean'
    }
    $pyInstallerArgs += $launcher
    & $python -m PyInstaller @pyInstallerArgs
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed to create the application.' }
    $packageDirectory = Join-Path $pyInstallerDistRoot $applicationName
    if (-not (Test-Path -LiteralPath $packageDirectory)) { throw "Build folder not found: $packageDirectory" }
    $applicationDir = Join-Path $releaseRoot $applicationName
    Update-ReleaseApplication -PackageDirectory $packageDirectory -Destination $applicationDir
    $applicationExe = Join-Path $applicationDir "$applicationName.exe"
    if (-not (Test-Path -LiteralPath $applicationExe)) { throw "Build file not found: $applicationExe" }

    Sync-DevProfilesToRelease -Destination $applicationDir

    $buildInfoPath = Join-Path $applicationDir 'build-info.json'
    $buildInfo = @{
        built_at = (Get-Date -Format 'dd/MM/yyyy HH:mm')
        diagnostic_screenshots_dir = (Join-Path $projectRoot 'data\screenshots')
    } | ConvertTo-Json -Compress
    [System.IO.File]::WriteAllText(
        $buildInfoPath,
        $buildInfo,
        [System.Text.UTF8Encoding]::new($false)
    )

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
        "Built at: $((Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz'))",
        'Excluded: unused Qt multimedia/PDF/QML/WebEngine modules and OpenCV FFmpeg video codec.',
        'Keeps the release profile snapshot and synchronizes it from development only when changed.',
        'Regenerable Chrome caches and debug logs are excluded from the copied profiles.'
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
    Write-Host 'Đã cập nhật ứng dụng; profile release dùng snapshot cache của bản dev.' -ForegroundColor Green
    Write-Host 'Next builds reuse the PyInstaller and profile caches. Use -CleanCache for a clean build.' -ForegroundColor DarkCyan
    if (-not $NoDesktopShortcut) { Write-Host "Desktop shortcut created: $applicationName" -ForegroundColor Green }
} finally { Pop-Location }
