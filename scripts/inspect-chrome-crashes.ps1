# Read-only Windows crash diagnosis: never terminate/open Chrome.
$ErrorActionPreference = 'Stop'
$start = (Get-Date).AddHours(-8)
$results = @()
try {
  $events = Get-WinEvent -FilterHashtable @{LogName='Application'; StartTime=$start; Id=1000,1001,1002} -ErrorAction SilentlyContinue
  foreach($evt in $events) {
    $message = [string]$evt.Message
    if ($message -notmatch '(?i)chrome\.exe|IK Auto\.exe') { continue }
    $module = ''
    $code = ''
    if ($message -match '(?im)(?:Faulting module name):\s*([^,\r\n]+)') { $module = $matches[1] }
    if ($message -match '(?im)(?:Exception code):\s*(0x[\da-fA-F]+)') { $code = $matches[1] }
    $results += [pscustomobject]@{
      utc=$evt.TimeCreated.ToUniversalTime().ToString('o')
      id=$evt.Id
      source=[string]$evt.ProviderName
      faulting_module=$module
      exception_code=$code
      chrome_related=$true
    }
    if($results.Count -ge 35){break}
  }
} catch { $results = @([pscustomobject]@{event_query_error=$_.Exception.GetType().Name}) }
@($results) | ConvertTo-Json -Depth 3 -Compress