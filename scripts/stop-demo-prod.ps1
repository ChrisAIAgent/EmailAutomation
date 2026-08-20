param([string]$Root = (Split-Path -Parent $PSScriptRoot))
$ErrorActionPreference = "SilentlyContinue"
foreach ($port in @(8000,3000)) {
  $pids = netstat -ano | Select-String ":$port\s+.*LISTENING" | ForEach-Object { ($_ -split '\s+')[-1] } | Where-Object { $_ -match '^\d+$' } | Select-Object -Unique
  foreach ($pid in $pids) { taskkill.exe /PID $pid /T /F | Out-Null }
}
