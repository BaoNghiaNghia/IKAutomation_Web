$ErrorActionPreference='Continue'
$rows=@()
$events=Get-WinEvent -FilterHashtable @{LogName='System';Id=2004;StartTime=(Get-Date).AddHours(-12)} -ErrorAction SilentlyContinue
foreach($ev in $events | Select-Object -First 10) {
  $xml=[xml]$ev.ToXml()
  $values=@{}
  $index=0
  foreach($field in @($xml.Event.EventData.Data)) {
    $label=[string]$field.Name
    if([string]::IsNullOrWhiteSpace($label)){$label='field'+$index}
    $value=[string]$field.'#text'
    if($label -match '(?i)process|user|path|command|sid|image|file') {
      if($value -match '(?i)chrome.exe') {$value='chrome.exe'}
      elseif($value -match '(?i)IK Auto.exe') {$value='IK Auto.exe'}
      else{$value='[redacted]'}
    }
    if($value.Length -gt 120){$value=$value.Substring(0,120)}
    $values[$label]=$value
    $index++
  }
  $rows += [pscustomobject]@{
    utc=$ev.TimeCreated.ToUniversalTime().ToString('o')
    provider=$ev.ProviderName
    properties=$values
  }
}
@($rows) | ConvertTo-Json -Depth 5 -Compress