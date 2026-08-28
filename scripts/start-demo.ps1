param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Stop"
# Some terminal hosts inject both Path and PATH. Windows treats them as the
# same variable, but Windows PowerShell 5.1 Start-Process rejects the duplicate.
$processPath = [Environment]::GetEnvironmentVariable("Path", "Process")
[Environment]::SetEnvironmentVariable("PATH", $null, "Process")
[Environment]::SetEnvironmentVariable("Path", $processPath, "Process")

$rootPath = (Resolve-Path $Root).Path
. (Join-Path $PSScriptRoot "data-dir.ps1")
. (Join-Path $PSScriptRoot "tacwork-runtime.ps1")

# Unified TACWork config: the Email frontend must reach the SAME ports the
# TACWork server/web were started on. Derive the URLs from the resolved port
# variables above (which honor TACWORK_SERVER_PORT / TACWORK_WEB_PORT). The
# client token is a fixed local loopback identifier and is intentionally NOT
# passed to the browser; a backend proxy is planned to remove it from the UI.
[Environment]::SetEnvironmentVariable("NEXT_PUBLIC_TACWORK_URL", "http://127.0.0.1:$script:TacWorkWebPort", "Process")
[Environment]::SetEnvironmentVariable("NEXT_PUBLIC_TACWORK_SERVER_URL", "http://127.0.0.1:$script:TacWorkServerPort", "Process")

$logsPath = $global:DataLogs
$runPath = Join-Path $logsPath "run"
$pidPath = Join-Path $runPath "services.json"
$pythonPath = Join-Path $rootPath "tools\python\python.exe"
$nodePath = Join-Path $rootPath "tools\node\node.exe"
$pythonPackages = Join-Path $rootPath "runtime\python-packages"
$frontendServer = Join-Path $rootPath "runtime\frontend\server.js"
$runtimeManifest = Join-Path $rootPath "runtime\runtime-manifest.json"
foreach ($required in @($pythonPath, $nodePath, $pythonPackages, $frontendServer, $runtimeManifest)) {
    if (-not (Test-Path $required)) { throw "runtime_missing: $required" }
}
$env:EMAIL_AUTOMATION_ROOT = $rootPath
$env:EMAIL_AUTOMATION_DATA_DIR = $global:DataRoot
$env:PYTHONPATH = (Join-Path $rootPath "backend") + ";" + $pythonPackages

# Decrypt the Email Automation LLM configuration into this trusted parent process.
# Child services inherit it; secrets are never written to logs or project config.
# TACWork's AI provider is configured inside the TACWork web UI and is not injected here.
$aiRuntimeJson = & $pythonPath (Join-Path $rootPath "scripts\export-ai-config.py")
if ($LASTEXITCODE -ne 0) { throw "Failed to load AI configuration." }
$aiRuntime = $aiRuntimeJson | ConvertFrom-Json
foreach ($name in @("LLM_PROVIDER", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY")) {
    [Environment]::SetEnvironmentVariable($name, [string]$aiRuntime.$name, "Process")
}

New-Item -ItemType Directory -Force -Path $logsPath, $runPath | Out-Null

# ---- Port conflict policy (runbook Section 5.5 / 8) ----
# NEVER kill external processes. We may only stop our own recorded service PIDs.
# If an external process holds a required port, report `port_conflict` with the
# PID + process name and abort; do not attempt to kill it.
$recordedPids = @()
$svcJson = Join-Path $runPath "services.json"
if (Test-Path $svcJson) {
    try {
        $svc = Get-Content -Raw -Encoding UTF8 $svcJson | ConvertFrom-Json
        $recordedPids = @(
            $svc.backend, $svc.consumer, $svc.frontend,
            $svc.tacwork_server, $svc.tacwork_engine, $svc.tacwork_web,
            $svc.launcher_backend, $svc.launcher_consumer, $svc.launcher_frontend,
            $svc.launcher_tacwork_server, $svc.launcher_tacwork_web
        ) | Where-Object { $_ } | Select-Object -Unique
    } catch {}
}

$ports = @(8000, 3000, $script:TacWorkServerPort, $script:TacWorkWebPort)
$listeners = @{}
foreach ($port in $ports) { $listeners[$port] = Get-ListenerPid $port }

# ---- Self-recognition fallback (runbook 5.5/8, probe-only) ----
# If services.json is missing/stale (lost PID record), listeners on 8000/3000
# that respond with OUR signatures are our own stack from a previous launch.
# Re-register them (never kill) so the adopt / stop-and-restart flow below can
# act on them. Unrecognized listeners still fall through to the
# external-conflict abort; the "never kill external processes" policy holds.
if ($recordedPids.Count -eq 0) {
    $recovered = @{}
    $backendPid = $listeners[8000]
    $frontendPid = $listeners[3000]
    if ($backendPid) {
        try {
            $h = Invoke-RestMethod "http://127.0.0.1:8000/api/health" -TimeoutSec 3
            if ($h.status -eq "ok" -and $null -ne $h.consumer) {
                $recovered.backend = $backendPid
                if ($h.consumer.pid) { $recovered.consumer = $h.consumer.pid }
            }
        } catch {}
    }
    if ($frontendPid) {
        try {
            $r = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:3000" -TimeoutSec 3
            if ($r.StatusCode -eq 200 -and $r.Content -match "Email Automation") {
                $recovered.frontend = $frontendPid
            }
        } catch {}
    }
    if ($listeners[$script:TacWorkServerPort] -and $listeners[$script:TacWorkWebPort]) {
        try {
            $th = Get-TacWorkHealth $rootPath
            # ready=true already proves server + web + engine + workspace match
            # ours; register BOTH TACWork listeners so the adopt path can act.
            if ($th.ready) {
                $recovered.tacwork_server = $listeners[$script:TacWorkServerPort]
                $recovered.tacwork_web = $listeners[$script:TacWorkWebPort]
            }
        } catch {}
    }
    if ($recovered.Count -gt 0) {
        $recordedPids = @($recovered.Values) | Where-Object { $_ } | Select-Object -Unique
        $recoveredJson = @{ started_at = (Get-Date).ToString("o"); status = "recovered" }
        foreach ($k in $recovered.Keys) { $recoveredJson[$k] = $recovered[$k] }
        $recoveredJson | ConvertTo-Json | Set-Content -Encoding UTF8 -Path $pidPath
        Write-Output ("Recovered own stack (services.json missing, re-registered from live listeners): " + (($recovered.Keys | ForEach-Object { "$_=$($recovered[$_])" }) -join ', '))
    }
}

$externalConflicts = @()
foreach ($port in $ports) {
    $listenerPid = $listeners[$port]
    if ($listenerPid -and $recordedPids -notcontains $listenerPid) {
        $procName = "unknown"
        try { $procName = (Get-Process -Id $listenerPid -ErrorAction SilentlyContinue).ProcessName } catch {}
        $externalConflicts += "port $port pid=$listenerPid process=$procName"
    }
}
if ($externalConflicts.Count -gt 0) {
    throw "port_conflict: $($externalConflicts -join '; '). An external process holds a required port. Stop or reconfigure that process (this tool will not kill it)."
}

$existingBackendPid = $listeners[8000]
$existingFrontendPid = $listeners[3000]
$existingTacWorkServerPid = $listeners[$script:TacWorkServerPort]
$existingTacWorkWebPid = $listeners[$script:TacWorkWebPort]

# If a complete, healthy stack is already running on our own PIDs, adopt it.
$allOursUp = ($existingBackendPid -and $existingFrontendPid -and $existingTacWorkServerPid -and $existingTacWorkWebPid)
if ($allOursUp) {
    $existingHealth = $null
    $existingFrontendReady = $false
    try {
        $existingHealth = Invoke-RestMethod "http://127.0.0.1:8000/api/health" -TimeoutSec 3
    } catch {}
    try {
        $existingFrontendReady = (
            Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:3000" -TimeoutSec 3
        ).StatusCode -eq 200
    } catch {}

    $isHealthyDemo = (
        $existingHealth.status -eq "ok" -and
        $existingHealth.consumer.healthy -eq $true -and
        $existingFrontendReady -and (Get-TacWorkHealth $rootPath).ready
    )
    if ($isHealthyDemo) {
        @{
            started_at = (Get-Date).ToString("o")
            adopted = $true
            backend = $existingBackendPid
            consumer = $existingHealth.consumer.pid
            frontend = $existingFrontendPid
            tacwork_server = $existingTacWorkServerPid
            tacwork_web = $existingTacWorkWebPid
            status = "ready"
        } | ConvertTo-Json | Set-Content -Encoding UTF8 -Path $pidPath
        Write-Output "Email Automation is already running and healthy."
        Write-Output "Frontend: http://127.0.0.1:3000"
        Write-Output "Backend:  http://127.0.0.1:8000/api/health"
        Write-Output "TACWork:  http://127.0.0.1:$script:TacWorkWebPort"
        Write-Output "PIDs: backend=$existingBackendPid consumer=$($existingHealth.consumer.pid) frontend=$existingFrontendPid"
        return
    }
}

# Our own stale/unhealthy instances: stop them, then start fresh.
& (Join-Path $PSScriptRoot "stop-demo.ps1") -Root $rootPath

$stillOccupied = @()
foreach ($port in $ports) {
    $listenerPid = Get-ListenerPid $port
    if ($listenerPid) { $stillOccupied += "port $port pid=$listenerPid" }
}
if ($stillOccupied.Count -gt 0) {
    throw "service_start_failed: required ports still occupied after stopping our services: $($stillOccupied -join '; '). Run stop-demo.bat as Administrator, then retry."
}

$backend = Start-Process -FilePath $pythonPath `
    -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000") `
    -WorkingDirectory (Join-Path $rootPath "backend") `
    -RedirectStandardOutput (Join-Path $logsPath "backend.log") `
    -RedirectStandardError (Join-Path $logsPath "backend-error.log") `
    -WindowStyle Hidden -PassThru

$consumer = Start-Process -FilePath $pythonPath `
    -ArgumentList @("run_consumer.py") `
    -WorkingDirectory (Join-Path $rootPath "backend") `
    -RedirectStandardOutput (Join-Path $logsPath "consumer.log") `
    -RedirectStandardError (Join-Path $logsPath "consumer-error.log") `
    -WindowStyle Hidden -PassThru

$frontend = Start-Process -FilePath $nodePath `
    -ArgumentList ('"{0}"' -f $frontendServer) `
    -WorkingDirectory (Join-Path $rootPath "runtime\frontend") `
    -RedirectStandardOutput (Join-Path $logsPath "frontend.log") `
    -RedirectStandardError (Join-Path $logsPath "frontend-error.log") `
    -WindowStyle Hidden -PassThru

$tacwork = Start-TacWorkRuntime -WorkspaceRoot $rootPath -LogsPath $logsPath -RunPath $runPath -PythonPath $pythonPath -PnpmPath $null

@{
    started_at = (Get-Date).ToString("o")
    backend = $backend.Id
    consumer = $consumer.Id
    frontend = $frontend.Id
    tacwork_server = $tacwork.server_launcher.Id
    tacwork_web = $tacwork.web_launcher.Id
} | ConvertTo-Json | Set-Content -Encoding UTF8 -Path $pidPath

$deadline = (Get-Date).AddSeconds(75)
$backendReady = $false
$frontendReady = $false
$consumerReady = $false
$tacworkReady = $false
do {
    Start-Sleep -Milliseconds 750
    try {
        $health = Invoke-RestMethod "http://127.0.0.1:8000/api/health" -TimeoutSec 2
        $backendReady = $health.status -eq "ok"
        $consumerReady = $health.consumer.healthy -eq $true
    } catch {}
    try {
        $frontendReady = (Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:3000" -TimeoutSec 2).StatusCode -eq 200
    } catch {}
    try { $tacworkHealth = Get-TacWorkHealth $rootPath; $tacworkReady = $tacworkHealth.ready } catch {}
} while ((-not ($backendReady -and $frontendReady -and $consumerReady -and $tacworkReady)) -and (Get-Date) -lt $deadline)

if (-not ($backendReady -and $frontendReady -and $consumerReady -and $tacworkReady)) {
    Write-Error "Startup failed: backend=$backendReady frontend=$frontendReady consumer=$consumerReady tacwork=$tacworkReady. Check logs/*-error.log."
}

$actualBackendPid = Get-ListenerPid 8000
$actualFrontendPid = Get-ListenerPid 3000
$actualConsumerPid = $health.consumer.pid
$actualTacWorkServerPid = Get-ListenerPid $script:TacWorkServerPort
$actualTacWorkWebPid = Get-ListenerPid $script:TacWorkWebPort
$actualTacWorkEnginePid = $null
if ($tacworkHealth.status.workspace.baseUrl) {
    try {
        $enginePort = ([uri]$tacworkHealth.status.workspace.baseUrl).Port
        $actualTacWorkEnginePid = Get-ListenerPid $enginePort
    } catch {}
}
@{
    started_at = (Get-Date).ToString("o")
    launcher_backend = $backend.Id
    launcher_consumer = $consumer.Id
    launcher_frontend = $frontend.Id
    launcher_tacwork_server = $tacwork.server_launcher.Id
    launcher_tacwork_web = $tacwork.web_launcher.Id
    backend = $actualBackendPid
    consumer = $actualConsumerPid
    frontend = $actualFrontendPid
    tacwork_server = $actualTacWorkServerPid
    tacwork_web = $actualTacWorkWebPid
    tacwork_engine = $actualTacWorkEnginePid
    tacwork_runtime_mode = $tacwork.runtime.mode
    status = "ready"
} | ConvertTo-Json | Set-Content -Encoding UTF8 -Path $pidPath

Write-Output "READY: Email Automation services are healthy."
Write-Output "Frontend: http://127.0.0.1:3000"
Write-Output "Backend:  http://127.0.0.1:8000/api/health"
Write-Output "TACWork:  http://127.0.0.1:$script:TacWorkWebPort"
Write-Output "PIDs: backend=$actualBackendPid consumer=$actualConsumerPid frontend=$actualFrontendPid"
Write-Output "TACWork PIDs: server=$actualTacWorkServerPid engine=$actualTacWorkEnginePid web=$actualTacWorkWebPid mode=$($tacwork.runtime.mode)"
