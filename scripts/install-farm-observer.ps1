[CmdletBinding()]
param([switch]$Remove)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$observer = Join-Path $root 'src\ik_chrome_auto\farm_observer.py'
$logs = Join-Path $root 'release\IK Auto\data\logs'
$taskName = 'IKAutoFarmObserver'

if ($Remove) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed $taskName"
    return
}
if (-not (Test-Path -LiteralPath $python)) { throw "Python .venv not found" }
if (-not (Test-Path -LiteralPath $observer)) { throw "Farm observer script not found" }
New-Item -ItemType Directory -Path $logs -Force | Out-Null
# The task only reads JSONL and writes a small sanitized status snapshot.
# Run in the logged-in user's context, not SYSTEM or an elevated account.
$action = New-ScheduledTaskAction -Execute $python -Argument ('"' + $observer + '" --logs "' + $logs + '"') -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -StartWhenAvailable
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Description 'IK Auto read-only Farm health snapshot; no CDP input or Chrome operations' -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Host "Installed $taskName; local snapshot: $logs\farm-observer-snapshot.json"