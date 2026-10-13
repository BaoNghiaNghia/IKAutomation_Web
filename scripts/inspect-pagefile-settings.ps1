$ErrorActionPreference='Continue'
$computer=Get-CimInstance Win32_ComputerSystem -ErrorAction Stop
$settings=@(Get-CimInstance Win32_PageFileSetting -ErrorAction SilentlyContinue)
$usage=@(Get-CimInstance Win32_PageFileUsage -ErrorAction SilentlyContinue)
[pscustomobject]@{
 automatic=[bool]$computer.AutomaticManagedPagefile
 settings=@($settings|ForEach-Object{[pscustomobject]@{drive=([string]$_.Name).Substring(0,1);initial_mb=$_.InitialSize;max_mb=$_.MaximumSize}})
 usage=@($usage|ForEach-Object{[pscustomobject]@{drive=([string]$_.Name).Substring(0,1);allocated_mb=$_.AllocatedBaseSize}})
 elevated=([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}|ConvertTo-Json -Depth 4 -Compress