[CmdletBinding()]
param([switch]$Remove)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$activator = Join-Path $root 'src\ik_chrome_auto\staged_update.py'
$taskName = 'IKAutoSafeStagedUpdate'

if ($Remove) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed $taskName"
    return
}
if (-not (Test-Path -LiteralPath $python)) { throw "Python .venv not found" }
if (-not (Test-Path -LiteralPath $activator)) { throw "Staged update activator missing" }
$action = New-ScheduledTaskAction -Execute $python -Argument ('"' + $activator + '" --root "' + $root + '"') -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -StartWhenAvailable
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Description 'Apply verified IK Auto binaries after app exit; never touch Chrome or profiles' -Force | Out-Null
Write-Host "Installed $taskName. Activation requires the IK Auto executable to be stopped."