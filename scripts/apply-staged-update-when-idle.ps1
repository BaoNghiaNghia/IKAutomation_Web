[CmdletBinding()]
param()
# Run after closing IK Auto manually. Never stops Chrome or a running tool.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$installer = Join-Path $root 'src\ik_chrome_auto\staged_update.py'
$exe = Join-Path $root 'release\IK Auto\IK Auto.exe'
if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath $installer)) {
    throw 'Update runtime is missing.'
}
if (@(Get-Process -Name 'IK Auto' -ErrorAction SilentlyContinue).Count -gt 0) {
    Write-Output 'WAITING: IK Auto is running. No files changed. Close the tool when convenient, then run this again.'
    return
}
$result = (& $python $installer --root $root | ConvertFrom-Json)
if ($LASTEXITCODE -ne 0) { throw "Update verification failed: $($result.result)" }
if ($result.result -eq 'application_running') {
    Write-Output 'WAITING: IK Auto is still running. Chrome is untouched.'
    return
}
if ($result.result -notin @('activated', 'nothing_staged')) {
    throw "Update status requires inspection: $($result.result)"
}
if (-not (Test-Path -LiteralPath $exe)) { throw 'Installed IK Auto executable missing.' }
Start-Process -FilePath $exe -WorkingDirectory (Split-Path -Parent $exe)
Write-Output "RESULT: $($result.result). IK Auto launched. Chrome and profile data unchanged."
