<#
.SYNOPSIS
Stops only the historical source-checkout compatibility stack.

.DESCRIPTION
This is deliberately separate from the formal launcher. It reads the old
backend\logs\run\services.json record, verifies every PID belongs to this
workspace, then stops it. It never migrates or deletes the legacy database,
credentials, queue, logs, or TACWork state.
#>
param([string]$Root = (Split-Path -Parent $PSScriptRoot))

$ErrorActionPreference = 'Stop'
$rootPath = (Resolve-Path $Root).Path
$legacyStatus = Join-Path (Join-Path (Join-Path $rootPath 'backend') 'logs') 'run\services.json'

if (-not (Test-Path $legacyStatus)) {
    Write-Output "No legacy compatibility status file found: $legacyStatus"
    exit 0
}

try { $services = Get-Content -Raw -Encoding UTF8 $legacyStatus | ConvertFrom-Json }
catch { throw "legacy_stack_status_invalid: $legacyStatus" }

$pids = @(
    $services.backend, $services.consumer, $services.frontend,
    $services.tacwork_server, $services.tacwork_engine, $services.tacwork_web,
    $services.launcher_backend, $services.launcher_consumer, $services.launcher_frontend,
    $services.launcher_tacwork_server, $services.launcher_tacwork_web
) | Where-Object { $_ } | Select-Object -Unique

foreach ($servicePid in $pids) {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$servicePid" -ErrorAction SilentlyContinue
    if (-not $proc) { continue }
    $identity = "{0} {1}" -f $proc.ExecutablePath, $proc.CommandLine
    if ($identity -notlike "*$rootPath*") {
        Write-Output "Skipped unverified process: pid=$servicePid name=$($proc.Name)"
        continue
    }
    Stop-Process -Id ([int]$servicePid) -Force -ErrorAction SilentlyContinue
    Write-Output "Stopped legacy compatibility process: pid=$servicePid name=$($proc.Name)"
}

Write-Output "Legacy data retained at: $(Join-Path $rootPath 'backend')"
