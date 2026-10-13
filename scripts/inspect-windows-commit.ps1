# Read-only commit/pagefile investigation.
$ErrorActionPreference='Continue'
$memory=Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue
$page=Get-CimInstance Win32_PageFileUsage -ErrorAction SilentlyContinue
$setting=Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue
$perf=Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory -ErrorAction SilentlyContinue
$drives=Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" -ErrorAction SilentlyContinue
[pscustomobject]@{
  total_physical_gib=[math]::Round($memory.TotalVisibleMemorySize / 1048576,1)
  free_physical_gib=[math]::Round($memory.FreePhysicalMemory / 1048576,1)
  commit_used_gib=[math]::Round($perf.CommittedBytes / 1GB,1)
  commit_limit_gib=[math]::Round($perf.CommitLimit / 1GB,1)
  commit_used_percent=[int]$perf.PercentCommittedBytesInUse
  auto_managed_pagefile=[bool]$setting.AutomaticManagedPagefile
  pagefiles=@($page | ForEach-Object {[pscustomobject]@{file=[IO.Path]::GetFileName($_.Name);drive=($_.Name).Substring(0,1);size_mb=$_.AllocatedBaseSize;peak_mb=$_.PeakUsage;current_mb=$_.CurrentUsage}})
  disk_free=@($drives | ForEach-Object {[pscustomobject]@{drive=$_.DeviceID;free_gib=[math]::Round($_.FreeSpace / 1GB,1)}})
} | ConvertTo-Json -Depth 4 -Compress