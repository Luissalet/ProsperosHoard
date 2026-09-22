#Requires -Version 5.1
# Starts Prospero's Hoard from the repository root (cwd matters: Faustus
# and faustus-plugin.json both assume the process working directory is
# the repo root).
$ErrorActionPreference = "Stop"
Set-Location -Path (Split-Path -Parent $PSScriptRoot)

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating virtual environment..."
    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install --upgrade pip
    .\.venv\Scripts\pip.exe install -r requirements-lock.txt
    .\.venv\Scripts\pip.exe install -e .
}

.\.venv\Scripts\python.exe -m prosperos_hoard @args
