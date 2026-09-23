#Requires -Version 5.1
<#
  Starts Prospero's Hoard from the repository root. The working directory
  matters: Faustus reads faustus-plugin.json from the process cwd.

  First run (or after requirements-lock.txt changes): creates .venv with
  Python 3.13 (C:\Python313, the py launcher, or python on PATH), installs
  the exact lock, and builds the UI with npm when frontend/dist is missing
  or older than the sources. Extra arguments go to the app, e.g.
    scripts\start.ps1 --demo
    scripts\start.ps1 --port 8816 --no-browser
#>
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root

function Find-Python {
    $candidates = @()
    if (Test-Path -LiteralPath "C:\Python313\python.exe") { $candidates += ,@("C:\Python313\python.exe") }
    if (Get-Command py -ErrorAction SilentlyContinue) { $candidates += ,@("py", "-3.13") }
    if (Get-Command python -ErrorAction SilentlyContinue) { $candidates += ,@("python") }
    foreach ($c in $candidates) {
        $exe = $c[0]
        $rest = @($c | Select-Object -Skip 1)
        try {
            $ver = & $exe @rest -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and [version]$ver -ge [version]"3.11") { return ,$c }
        } catch { }
    }
    throw "Python 3.11+ was not found. Install Python 3.13 (C:\Python313) and run this again."
}

function Get-FileSha256 {
    param([string]$Path)
    # .NET SHA256 instead of Get-FileHash: Get-FileHash does not load when
    # Windows PowerShell 5.1 is started from PowerShell 7 (pwsh).
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $stream = [System.IO.File]::OpenRead($Path)
        try {
            $bytes = $sha256.ComputeHash($stream)
        } finally {
            $stream.Dispose()
        }
    } finally {
        $sha256.Dispose()
    }
    return [System.BitConverter]::ToString($bytes).Replace("-", "")
}

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$Stamp = Join-Path $Root ".venv\.lock-hash"
$LockHash = Get-FileSha256 (Join-Path $Root "requirements-lock.txt")

if (-not (Test-Path -LiteralPath $VenvPython)) {
    $py = Find-Python
    $exe = $py[0]
    $rest = @($py | Select-Object -Skip 1)
    Write-Host "Creating the virtual environment (.venv)..."
    & $exe @rest -m venv (Join-Path $Root ".venv")
    if ($LASTEXITCODE -ne 0) { throw "python -m venv failed" }
}
$installed = if (Test-Path -LiteralPath $Stamp) { (Get-Content -LiteralPath $Stamp -Raw).Trim() } else { "" }
if ($installed -ne $LockHash) {
    Write-Host "Installing the pinned dependencies (requirements-lock.txt)..."
    & $VenvPython -m pip install --disable-pip-version-check --upgrade pip
    & $VenvPython -m pip install --disable-pip-version-check -r (Join-Path $Root "requirements-lock.txt")
    if ($LASTEXITCODE -ne 0) { throw "pip install -r requirements-lock.txt failed" }
    Set-Content -LiteralPath $Stamp -Value $LockHash -Encoding ascii
}

$Dist = Join-Path $Root "frontend\dist\index.html"
$needBuild = -not (Test-Path -LiteralPath $Dist)
if (-not $needBuild) {
    $built = (Get-Item -LiteralPath $Dist).LastWriteTimeUtc
    $newest = Get-ChildItem -LiteralPath (Join-Path $Root "frontend\src") -Recurse -File |
        Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
    if ($newest -and $newest.LastWriteTimeUtc -gt $built) { $needBuild = $true }
}
if ($needBuild) {
    if (Get-Command npm.cmd -ErrorAction SilentlyContinue) {
        Write-Host "Building the interface (npm ci && npm run build)..."
        Push-Location -LiteralPath (Join-Path $Root "frontend")
        try {
            # npm.cmd, not npm: Node 22's npm.ps1 shim misreads "& npm ci" as
            # "pm ci" when invoked this way from Windows PowerShell.
            & npm.cmd ci
            if ($LASTEXITCODE -ne 0) { throw "npm ci failed" }
            & npm.cmd run build
            if ($LASTEXITCODE -ne 0) { throw "npm run build failed" }
        } finally { Pop-Location }
    } else {
        Write-Warning "npm (Node 22) was not found: the API will run, but the interface cannot be built."
    }
}

& $VenvPython -m prosperos_hoard @args
exit $LASTEXITCODE
