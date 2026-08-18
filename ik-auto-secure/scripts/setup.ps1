$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot
$VenvDir = Join-Path $ProjectDir ".venv"
$ConfigPath = Join-Path $ProjectDir "config.json"
$ExampleConfigPath = Join-Path $ProjectDir "config.example.json"

function Find-Python {
    $commands = @(
        @{ Command = "py"; Args = @("-3.13") },
        @{ Command = "py"; Args = @("-3.12") },
        @{ Command = "py"; Args = @("-3.11") },
        @{ Command = "python"; Args = @() }
    )
    foreach ($candidate in $commands) {
        try {
            & $candidate.Command @($candidate.Args) -c "import sys; assert (3,11) <= sys.version_info[:2] < (3,14)" 2>$null
            if ($LASTEXITCODE -eq 0) { return $candidate }
        } catch { }
    }
    throw "Can Python 3.11, 3.12 hoac 3.13."
}

if (-not (Test-Path $VenvDir)) {
    $Python = Find-Python
    & $Python.Command @($Python.Args) -m venv $VenvDir
}

$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
& $VenvPython -m pip install --disable-pip-version-check "playwright>=1.54,<2" "pytest>=8,<10"

$DataDirs = @(
    "data\profiles",
    "data\snapshots",
    "data\screenshots",
    "data\logs"
    "data\gather"
    "data\radar"
)
foreach ($relative in $DataDirs) {
    New-Item -ItemType Directory -Force -Path (Join-Path $ProjectDir $relative) | Out-Null
}

if (-not (Test-Path $ConfigPath) -and (Test-Path $ExampleConfigPath)) {
    Copy-Item -LiteralPath $ExampleConfigPath -Destination $ConfigPath
    Write-Host "Da tao config.json tu config.example.json."
}

Write-Host "Da cai IK Chrome Auto. Chay run.cmd de mo dashboard." -ForegroundColor Green
