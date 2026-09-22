#Requires -Version 5.1
<#
  Stops Prospero's Hoard listening on a port (default 8815). It only stops a
  process after /api/health on that port answers service "prosperos-hoard",
  so another app that happens to use the port is never killed.
#>
param([int]$Port = 8815)
$ErrorActionPreference = "Stop"

try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 3
} catch {
    Write-Host "Prospero's Hoard is not answering on port $Port."
    exit 0
}
if ($health.service -ne "prosperos-hoard") {
    Write-Host "Port $Port belongs to '$($health.service)', not Prospero's Hoard. Nothing stopped."
    exit 1
}
$conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
foreach ($conn in $conns) {
    $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
    if ($proc) {
        Write-Host "Stopping Prospero's Hoard (process $($proc.Id), $($proc.ProcessName)) on port $Port..."
        Stop-Process -Id $proc.Id
    }
}
