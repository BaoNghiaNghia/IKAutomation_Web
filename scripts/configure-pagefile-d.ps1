[CmdletBinding()]
param([switch]$Apply)
# Preserve C: pagefile, add D: paging capacity after next Windows reboot.
# Do not reboot and do not close any Chrome profile.
$ErrorActionPreference='Stop'
$drive='D:'
[uint32]$initialMB=65536
[uint32]$maximumMB=98304
$computer=Get-CimInstance -ClassName Win32_ComputerSystem
$usage=@(Get-CimInstance -ClassName Win32_PageFileUsage -ErrorAction Stop)
$settings=@(Get-CimInstance -ClassName Win32_PageFileSetting -ErrorAction SilentlyContinue)
$disk=Get-CimInstance -ClassName Win32_LogicalDisk -Filter "DeviceID='D:'"
$admin=([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if($null -eq $disk -or $disk.FreeSpace -lt (100GB)) { throw 'D: requires at least 100 GB free for the planned pagefile capacity.' }
$preview=[pscustomobject]@{
 applied=$false
 run_as_administrator=$admin
 automatic_pagefile_before=[bool]$computer.AutomaticManagedPagefile
 C_current_mb=(@($usage|Where-Object{$_.Name -like 'C:*'}|Select-Object -First 1)[0].AllocatedBaseSize)
 D_free_gib=[math]::Round($disk.FreeSpace/1GB,1)
 D_initial_mb=$initialMB
 D_max_mb=$maximumMB
 reboot_required=$true
}
if(-not $Apply) { $preview | ConvertTo-Json -Compress; return }
if(-not $admin) { throw 'Run PowerShell as Administrator and pass -Apply. No changes were made.' }
if(@($settings|Where-Object { $_.Name -like 'D:*' }).Count -gt 0) { throw 'D: already has pagefile setting. Review manually; no changes were made.' }
$cUsage=@($usage|Where-Object{$_.Name -like 'C:*'}|Select-Object -First 1)
if($cUsage.Count -ne 1 -or $cUsage[0].AllocatedBaseSize -lt 1024) { throw 'C: pagefile size could not be verified. No changes were made.' }
[uint32]$cSize=$cUsage[0].AllocatedBaseSize
$wasAuto=[bool]$computer.AutomaticManagedPagefile
try {
    if($wasAuto) {
        Set-CimInstance -InputObject $computer -Property @{AutomaticManagedPagefile=$false} | Out-Null
    }
    $existing=@(Get-CimInstance -ClassName Win32_PageFileSetting -ErrorAction SilentlyContinue)
    $cSettings=@($existing|Where-Object{$_.Name -like 'C:*'})
    if($cSettings.Count -eq 0) {
        New-CimInstance -ClassName Win32_PageFileSetting -Property @{Name='C:\pagefile.sys';InitialSize=[uint32]$cSize;MaximumSize=[uint32]$cSize} | Out-Null
    } elseif($cSettings.Count -eq 1) {
        Set-CimInstance -InputObject $cSettings[0] -Property @{InitialSize=[uint32]$cSize;MaximumSize=[uint32]$cSize} | Out-Null
    } else {
        throw 'More than one C: pagefile setting was found.'
    }
    New-CimInstance -ClassName Win32_PageFileSetting -Property @{Name='D:\pagefile.sys';InitialSize=[uint32]$initialMB;MaximumSize=[uint32]$maximumMB} | Out-Null
    $verify=@(Get-CimInstance -ClassName Win32_PageFileSetting)
    if(@($verify|Where-Object{ $_.Name -like 'D:*' -and [uint32]$_.InitialSize -eq $initialMB }).Count -ne 1) {
        throw 'D: pagefile configuration verification failed.'
    }
} catch {
    if($wasAuto) {
        Set-CimInstance -InputObject (Get-CimInstance -ClassName Win32_ComputerSystem) -Property @{AutomaticManagedPagefile=$true} -ErrorAction SilentlyContinue | Out-Null
    }
    throw
}
Write-Output 'Paging file configured on D:, existing C: preserved. Restart Windows manually when Chrome work has been saved. No automatic reboot.'