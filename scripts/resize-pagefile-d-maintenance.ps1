[CmdletBinding()]
param(
    [switch]$Apply,
    [int]$InitialGiB = 128,
    [int]$MaximumGiB = 160
)
# Stage a larger D: Windows paging file only during a saved maintenance window.
# Never reboot, close Chrome, resize current pagefile, or touch C: settings.
$ErrorActionPreference = 'Stop'
if ($InitialGiB -lt 96 -or $MaximumGiB -lt $InitialGiB -or $MaximumGiB -gt 192) {
    throw 'Expected D: pagefile Initial >=96 GiB and Maximum <=192 GiB.'
}
$disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='D:'"
$computer = Get-CimInstance Win32_ComputerSystem
$settings = @(Get-CimInstance Win32_PageFileSetting)
$dpage = @($settings | Where-Object { $_.Name -ieq 'D:\pagefile.sys' })
$cpage = @($settings | Where-Object { $_.Name -ieq 'C:\pagefile.sys' })
$active = @(Get-CimInstance Win32_PageFileUsage)
$chromeCount = @(Get-Process -Name chrome -ErrorAction SilentlyContinue).Count
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$preview = [pscustomobject]@{
    applied = $false
    chrome_process_count = $chromeCount
    is_administrator = $admin
    D_current_initial_mb = $(if ($dpage.Count -eq 1) { $dpage[0].InitialSize } else { $null })
    D_current_maximum_mb = $(if ($dpage.Count -eq 1) { $dpage[0].MaximumSize } else { $null })
    D_current_allocated_mb = @($active | Where-Object { $_.Name -ieq 'D:\pagefile.sys' } | Select-Object -First 1)[0].AllocatedBaseSize
    D_free_gib = [math]::Round($disk.FreeSpace / 1GB, 1)
    D_target_initial_gib = $InitialGiB
    D_target_maximum_gib = $MaximumGiB
    C_settings_unchanged = $true
    reboot_required = $true
}
if (-not $Apply) {
    $preview | ConvertTo-Json -Compress
    return
}
if (-not $admin) { throw 'Administrator required; nothing changed.' }
if ($chromeCount -gt 0) { throw "Chrome is running ($chromeCount processes). Save your sessions and close Chrome before applying; nothing changed." }
if ($null -eq $disk -or $disk.FreeSpace -lt (($MaximumGiB + 32) * 1GB)) {
    throw 'Insufficient D: free space for maximum pagefile + 32 GiB buffer; nothing changed.'
}
if ($computer.AutomaticManagedPagefile) { throw 'Automatic pagefile mode active; no changes made.' }
if ($dpage.Count -ne 1 -or $cpage.Count -ne 1) { throw 'Expected exactly one C: and D: pagefile setting; nothing changed.' }
$prior = @{
    InitialSize = [uint32]$dpage[0].InitialSize
    MaximumSize = [uint32]$dpage[0].MaximumSize
}
try {
    Set-CimInstance -InputObject $dpage[0] -Property @{
        InitialSize = [uint32]($InitialGiB * 1024)
        MaximumSize = [uint32]($MaximumGiB * 1024)
    } | Out-Null
    $after = @(Get-CimInstance Win32_PageFileSetting | Where-Object { $_.Name -ieq 'D:\pagefile.sys' })
    if ($after.Count -ne 1 -or [uint32]$after[0].InitialSize -ne ($InitialGiB * 1024) -or [uint32]$after[0].MaximumSize -ne ($MaximumGiB * 1024)) {
        throw 'Pagefile configuration verification failed.'
    }
} catch {
    $rollback = @(Get-CimInstance Win32_PageFileSetting | Where-Object { $_.Name -ieq 'D:\pagefile.sys' })
    if ($rollback.Count -eq 1) {
        Set-CimInstance -InputObject $rollback[0] -Property $prior -ErrorAction SilentlyContinue | Out-Null
    }
    throw
}
Write-Output "D: pagefile configured for next reboot ($InitialGiB-$MaximumGiB GiB). C: unchanged. No automatic reboot."