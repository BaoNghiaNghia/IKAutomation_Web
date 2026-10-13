param(
    [Parameter(Mandatory = $true)][string]$ProjectRoot,
    [Parameter(Mandatory = $true)][int]$ParentPid,
    [Parameter(Mandatory = $true)][ValidateSet('dev', 'release')][string]$LaunchMode,
    [Parameter(Mandatory = $true)][string]$ExecutablePath
)

# Detached bootstrapper: the running exe must exit before replacing package files.
$ErrorActionPreference = 'Stop'
$project = (Resolve-Path -LiteralPath $ProjectRoot).Path
$logDir = Join-Path $project 'data\logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$logFile = Join-Path $logDir 'updater.jsonl'
$updated = $false
$mutex = $null
$mutexAcquired = $false

function Write-UpdateEvent {
    param([string]$Name, [string]$Reason = '')
    $row = @{
        timestamp = (Get-Date).ToUniversalTime().ToString('o')
        event = $Name
        mode = $LaunchMode
        reason = $Reason
    } | ConvertTo-Json -Compress
    if ((Test-Path -LiteralPath $logFile) -and (Get-Item -LiteralPath $logFile).Length -gt 2000000) {
        $backup = "$logFile.1"
        Move-Item -LiteralPath $logFile -Destination $backup -Force
    }
    Add-Content -LiteralPath $logFile -Value $row -Encoding UTF8
}

try {
    $deadline = (Get-Date).AddSeconds(120)
    while (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue) {
        if ((Get-Date) -gt $deadline) {
            throw 'parent_still_running'
        }
        Start-Sleep -Milliseconds 500
    }
    Start-Sleep -Milliseconds 1000

    # The development CMD holds the same mutex for the entire Python child
    # lifetime. Waiting only for ParentPid can race its final release.
    # Hold the mutex while updating so no second dashboard can start against
    # files being replaced. Never terminate a Chrome process.
    $mutex = [System.Threading.Mutex]::new($false, 'Local\IKAutoBrowserDashboard')
    try {
        $mutexAcquired = $mutex.WaitOne(30000)
    } catch [System.Threading.AbandonedMutexException] {
        $mutexAcquired = $true
    }
    if (-not $mutexAcquired) {
        throw 'dashboard_mutex_busy'
    }
    Write-UpdateEvent 'startup_update_mutex_acquired'

    $branch = (& git -C $project branch --show-current)
    if ($LASTEXITCODE -ne 0 -or $branch.Trim() -ne 'main') {
        throw 'branch_not_main'
    }
    $dirty = (& git -C $project status --porcelain --untracked-files=normal)
    if ($LASTEXITCODE -ne 0 -or $dirty) {
        throw 'working_tree_dirty'
    }
    & git -C $project fetch --quiet origin
    if ($LASTEXITCODE -ne 0) {
        throw 'fetch_failed'
    }
    & git -C $project merge-base --is-ancestor HEAD origin/main
    if ($LASTEXITCODE -ne 0) {
        throw 'non_fast_forward'
    }
    & git -C $project pull --ff-only origin main
    if ($LASTEXITCODE -ne 0) {
        throw 'pull_failed'
    }
    if ($LaunchMode -eq 'release') {
        # Never copy the active dev Chrome profiles over an existing release.
        $build = Join-Path $project 'scripts\build-release.ps1'
        & $build -NoDesktopShortcut -SkipProfileSync
        if ($LASTEXITCODE -ne 0) {
            throw 'build_failed'
        }
    }
    $updated = $true
    Write-UpdateEvent 'startup_update_applied'
} catch {
    # Do not serialize exception messages: URLs and external Git output can
    # contain credentials. Fall back to launching the last available version.
    $reason = $_.Exception.Message
    if ($reason -notin @(
        'parent_still_running', 'branch_not_main', 'working_tree_dirty',
        'fetch_failed', 'non_fast_forward', 'pull_failed', 'build_failed',
        'dashboard_mutex_busy'
    )) {
        $reason = 'unexpected_error'
    }
    Write-UpdateEvent 'startup_update_failed' $reason
} finally {
    # Release before relaunch: the new instance must acquire the singleton.
    if ($mutexAcquired -and $null -ne $mutex) {
        $mutex.ReleaseMutex()
        $mutexAcquired = $false
    }
    if ($null -ne $mutex) {
        $mutex.Dispose()
    }
    $env:IK_AUTO_SKIP_STARTUP_UPDATE = '1'
    try {
        if ($LaunchMode -eq 'release') {
            $exe = Join-Path $project 'release\IK Auto\IK Auto.exe'
            if (-not (Test-Path -LiteralPath $exe)) {
                $exe = $ExecutablePath
            }
            Start-Process -FilePath $exe -WorkingDirectory (Split-Path -Parent $exe)
        } else {
            # Launch through the existing single-instance mutex/bootstrapper.
            $cmd = Join-Path $project 'IKAutomation_dev.cmd'
            Start-Process -FilePath $cmd -WorkingDirectory $project
        }
        Write-UpdateEvent 'startup_update_relaunch' $(if ($updated) {'updated'} else {'fallback'})
    } catch {
        Write-UpdateEvent 'startup_update_relaunch_failed' 'process_start_failed'
    }
}