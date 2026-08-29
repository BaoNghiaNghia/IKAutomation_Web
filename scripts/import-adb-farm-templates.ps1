[CmdletBinding()]
param(
    [string]$AdbDataRoot = 'D:\Bot_Tool_Auto_Game\IKAutomation\ADB\Data\InfinityKingdom\1280x720\vi'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$destination = Join-Path $projectRoot 'src\ik_chrome_auto\assets\farm_templates'
$names = @(
    'world_map_anchor.png', 'city_to_world_map_button.png',
    'continent_map_title.png', 'continent_map_home_territory_anchor.png', 'continent_map_pin_button.png',
    'resource_search_panel_anchor.png', 'search_button_enabled.png',
    'level_minus_button.png', 'resource_tab_selected.png', 'resource_tab_unselected.png',
    'resource_popup_info_anchor.png', 'resource_popup_iron_title.png', 'gather_button_enabled.png',
    'team_selection_panel_anchor.png', 'team_adjust_formation_button.png', 'team_action_button_enabled.png',
    'storage_limit_dialog_anchor.png', 'storage_limit_cancel_button.png', 'resource_expiry_dialog_anchor.png'
)

if (-not (Test-Path -LiteralPath $AdbDataRoot)) { throw "ADB template root not found: $AdbDataRoot" }
New-Item -ItemType Directory -Force -Path $destination | Out-Null
foreach ($name in $names) {
    $source = Get-ChildItem -LiteralPath $AdbDataRoot -Recurse -File -Filter $name | Select-Object -First 1
    if ($null -eq $source) { throw "Required ADB template is missing: $name" }
    Copy-Item -LiteralPath $source.FullName -Destination (Join-Path $destination $name) -Force
}
Write-Host "Imported $($names.Count) ADB farm templates into $destination" -ForegroundColor Green
