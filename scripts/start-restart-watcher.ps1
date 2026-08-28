param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Stop"
$rootPath = (Resolve-Path $Root).Path
. (Join-Path $PSScriptRoot "data-dir.ps1")
. (Join-Path $PSScriptRoot "runtime-ports.ps1")
$markerPath = Join-Path $global:DataLogs "run\restart-requested.json"

# Spawned on-demand by POST /api/system/restart. The backend writes the marker
# then exits after ~1s; wait a little so the port is released before stop runs.
Start-Sleep -Seconds 3

if (-not (Test-Path $markerPath)) {
    # No restart requested (e.g. stray launch) — exit quietly, do nothing.
    exit 0
}

# Consume the marker so a second watcher cannot double-restart.
Remove-Item -LiteralPath $markerPath -Force -ErrorAction SilentlyContinue

# Stop the full stack, then start it again.
& (Join-Path $PSScriptRoot "stop-demo.ps1") -Root $rootPath

# Wait for the service ports to be free before starting (stop may need a moment).
$ports = @($script:BackendPort, $script:FrontendPort)
foreach ($port in $ports) {
    $wait = 0
    while ($wait -lt 10) {
        $listener = netstat -ano | Select-String ":$port\s" | Select-String "LISTENING"
        if (-not $listener) { break }
        Start-Sleep -Seconds 1
        $wait++
    }
}

& (Join-Path $PSScriptRoot "start-demo.ps1") -Root $rootPath
