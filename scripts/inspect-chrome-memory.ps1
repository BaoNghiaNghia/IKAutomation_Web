$ErrorActionPreference = 'Continue'
$chrome = @(Get-Process -Name chrome -ErrorAction SilentlyContinue)
$ik = @(Get-Process -Name 'IK Auto' -ErrorAction SilentlyContinue)
$total = ($chrome | Measure-Object -Property PrivateMemorySize64 -Sum).Sum
$max = ($chrome | Measure-Object -Property PrivateMemorySize64 -Maximum).Maximum
$large = @($chrome | Where-Object {$_.PrivateMemorySize64 -gt 1GB}).Count
[pscustomobject]@{
 chrome_processes=$chrome.Count
 chrome_private_commit_gib=[math]::Round($total/1GB,2)
 largest_chrome_private_gib=[math]::Round($max/1GB,2)
 chrome_processes_above_1gib=$large
 ik_auto_processes=$ik.Count
 ik_auto_private_gib=[math]::Round((($ik|Measure-Object -Property PrivateMemorySize64 -Sum).Sum)/1GB,2)
} | ConvertTo-Json -Compress