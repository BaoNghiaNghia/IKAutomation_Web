param(
    [ValidatePattern('^3\.1[1-3]$')]
    [string]$MinorVersion = '3.13'
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

    $minorFolder = $MinorVersion.Replace('.', '')
    $installDirectory = Join-Path $env:LocalAppData "Programs\Python\Python$minorFolder"
    $installedPython = Join-Path $installDirectory 'python.exe'

    if (Test-Path -LiteralPath $installedPython) {
        & $installedPython -c "import sys; raise SystemExit(0 if sys.version_info[:2] == tuple(map(int, '$MinorVersion'.split('.'))) else 1)"
        if ($LASTEXITCODE -eq 0) {
            Write-Host "[OK] Python $MinorVersion is already installed at $installedPython" -ForegroundColor Green
            exit 0
        }
    }

    Write-Host "Finding the latest Python $MinorVersion installer on python.org..."
    $index = Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/'
    $versionPattern = [regex]::Escape($MinorVersion) + '\.\d+'
    $releaseRegex = 'href=["''](' + $versionPattern + ')/["'']'
    $versions = [regex]::Matches($index.Content, $releaseRegex) |
        ForEach-Object { [version]$_.Groups[1].Value } |
        Sort-Object -Descending -Unique

    if (-not $versions) {
        throw "python.org did not return a Python $MinorVersion release."
    }

    $version = $versions[0].ToString()
    $architecture = if ([Environment]::Is64BitOperatingSystem) { 'amd64' } else { 'win32' }
    $installerName = "python-$version-$architecture.exe"
    $downloadUri = "https://www.python.org/ftp/python/$version/$installerName"
    $installerPath = Join-Path ([IO.Path]::GetTempPath()) $installerName

    try {
        Write-Host "Downloading Python $version from $downloadUri"
        Invoke-WebRequest -UseBasicParsing -Uri $downloadUri -OutFile $installerPath

        $signature = Get-AuthenticodeSignature -LiteralPath $installerPath
        if ($signature.Status -ne 'Valid' -or
            $signature.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
            throw 'The downloaded installer does not have a valid Python Software Foundation signature.'
        }

        Write-Host "Installing Python $version for the current Windows user..."
        $arguments = @(
            '/quiet'
            'InstallAllUsers=0'
            "TargetDir=$installDirectory"
            'PrependPath=0'
            'Include_launcher=1'
            'InstallLauncherAllUsers=0'
            'Include_pip=1'
            'Include_test=0'
            'Include_doc=0'
            'Include_debug=0'
            'Include_symbols=0'
            'Shortcuts=0'
            'AssociateFiles=0'
        )
        $process = Start-Process -FilePath $installerPath -ArgumentList $arguments -Wait -PassThru
        if ($process.ExitCode -ne 0) {
            throw "The Python installer exited with code $($process.ExitCode)."
        }
    }
    finally {
        Remove-Item -LiteralPath $installerPath -Force -ErrorAction SilentlyContinue
    }

    if (-not (Test-Path -LiteralPath $installedPython)) {
        throw "Python installation completed but python.exe was not found at $installedPython."
    }

    & $installedPython -c "import sys; print('[OK] Python', sys.version.split()[0])"
    if ($LASTEXITCODE -ne 0) {
        throw 'The newly installed Python executable could not be started.'
    }
    exit 0
}
catch {
    Write-Host "[ERROR] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
