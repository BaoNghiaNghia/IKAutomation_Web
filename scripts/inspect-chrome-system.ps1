# Read-only Windows System event summaries for Chrome crash investigation.
$ErrorActionPreference='Continue'
$out=@()
$start=(Get-Date).AddHours(-8)
foreach($log in @('System','Application')) {
  $events=Get-WinEvent -FilterHashtable @{LogName=$log;StartTime=$start} -ErrorAction SilentlyContinue
  foreach($ev in $events) {
    $provider=[string]$ev.ProviderName
    if ($provider -notmatch '(?i)Display|nvlddmkm|WHEA|Resource-Exhaustion|Kernel-PnP|Application Error') { continue }
    $msg=[string]$ev.Message
    $code=''
    if($msg -match '(?im)Exception code:\s*(0x[0-9a-f]+)'){$code=$Matches[1]}
    $out += [pscustomobject]@{
      utc=$ev.TimeCreated.ToUniversalTime().ToString('o')
      log=$log
      provider=$provider
      id=[int]$ev.Id
      code=$code
      gpu_or_display=$provider -match '(?i)Display|nvlddmkm|WHEA'
    }
    if($out.Count -ge 100){break}
  }
}
@($out) | ConvertTo-Json -Depth 3 -Compress