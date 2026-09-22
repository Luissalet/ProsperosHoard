#Requires -Version 5.1
# Stops any Prospero's Hoard process bound to the given port (default 8815).
param([int]$Port = 8815)
$ErrorActionPreference = "SilentlyContinue"

$conns = Get-NetTCPConnection -LocalPort $Port -State Listen
if (-not $conns) {
    Write-Host "Nothing listening on port $Port."
    exit 0
}
foreach ($conn in $conns) {
    Write-Host "Stopping process $($conn.OwningProcess) on port $Port..."
    Stop-Process -Id $conn.OwningProcess -Force
}
