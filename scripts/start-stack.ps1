param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [switch]$SkipFrontend,
    [switch]$Development
)

$ErrorActionPreference = "Stop"
# Some terminal hosts inject case-insensitive duplicate environment variables
# (e.g. both http_proxy and HTTP_PROXY, or both Path and PATH). Windows treats
# them as the same variable, but Windows PowerShell 5.1 Start-Process copies
# the environment through a case-sensitive dictionary and throws
# "An item with the same key has already been added" on the duplicate.
# Normalize the WHOLE process environment (not just Path/PATH) by collapsing
# each case-insensitive group to a single canonical spelling before any child
# process is launched. This mirrors build-runtime.ps1 but is generalized and
# must run here because this is the real service-launch path.
$dupGroups = [Environment]::GetEnvironmentVariables("Process").Keys | Group-Object { [string]$_.ToLowerInvariant() } | Where-Object { $_.Count -gt 1 }
foreach ($group in $dupGroups) {
    $names = @($group.Group | ForEach-Object { [string]$_ })
    $keeper = if ($names -contains 'Path') { 'Path' } else { $names[0] }
    $keepValue = [string][Environment]::GetEnvironmentVariable($keeper, "Process")
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $null, "Process") }
    [Environment]::SetEnvironmentVariable($keeper, $keepValue, "Process")
}

$rootPath = (Resolve-Path $Root).Path

function Test-WorkspaceProcess {
    param([object]$ProcessId)
    if (-not $ProcessId) { return $false }
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if (-not $proc) { return $false }
    # Require a directory boundary, not a substring (App-old is not App).
    # Process metadata unavailable means ownership is unproven.
    $prefix = $rootPath.TrimEnd('\', '/') + '\'
    if ($proc.ExecutablePath -and ([string]$proc.ExecutablePath).StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    $pattern = '(?i)(?:^|["\s])' + [regex]::Escape($prefix)
    return [bool]($proc.CommandLine -and ([string]$proc.CommandLine -match $pattern))
}

. (Join-Path $PSScriptRoot "data-dir.ps1") -Mode $(if ($Development) { 'Development' } else { 'Formal' })
if ($global:DataMode -eq 'legacy') {
    # Legacy mode stores data in backend\app.db and run state in
    # backend\logs\run\services.json — a second state root the Electron shell
    # never reads. Every stack started this way cannot be recognised or adopted
    # by a later launcher run and collides on the shared port group.
    Write-Warning ("Data directory resolved to LEGACY mode (" + $global:DataRoot + "). " +
        "This splits state from the Electron shell's per-user data root and breaks stack adoption. " +
        "Set EMAIL_AUTOMATION_DATA_DIR to '%LOCALAPPDATA%\TAC AISolution\Email Automation' (start-stack.bat does this).")
}
. (Join-Path $PSScriptRoot "runtime-ports.ps1")
function Test-BackendIdentity {
    param([object]$Health)
    return [bool]($Health.instance -and
        $Health.instance.install_root -eq $rootPath -and
        $Health.instance.data_root -eq $global:DataRoot)
}
. (Join-Path $PSScriptRoot "tacwork-runtime.ps1")

# Unified TACWork config: the Email frontend must reach the SAME ports the
# TACWork server/web were started on. Derive the URLs from the resolved port
# variables above (which honor TACWORK_SERVER_PORT / TACWORK_WEB_PORT). The
# client token is a fixed local loopback identifier and is intentionally NOT
# passed to the browser; a backend proxy is planned to remove it from the UI.
[Environment]::SetEnvironmentVariable("NEXT_PUBLIC_TACWORK_URL", "http://127.0.0.1:$script:TacWorkWebPort", "Process")
[Environment]::SetEnvironmentVariable("NEXT_PUBLIC_TACWORK_SERVER_URL", "http://127.0.0.1:$script:TacWorkServerPort", "Process")
$env:NEXT_PUBLIC_TACWORK_URL = "http://127.0.0.1:$script:TacWorkWebPort"
$env:NEXT_PUBLIC_TACWORK_SERVER_URL = "http://127.0.0.1:$script:TacWorkServerPort"

$logsPath = $global:DataLogs
$runPath = $global:DataRun
$pidPath = Join-Path $runPath "services.json"
$pythonPath = if ($Development) { Join-Path $rootPath "backend\.venv\Scripts\python.exe" } else { Join-Path $rootPath "tools\python\python.exe" }
$nodePath = Join-Path $rootPath "tools\node\node.exe"
$pythonPackages = Join-Path $rootPath "runtime\python-packages"
$frontendServer = Join-Path $rootPath "runtime\frontend\server.js"
$frontendDevServer = Join-Path $rootPath "frontend\node_modules\next\dist\bin\next"
$runtimeManifest = Join-Path $rootPath "runtime\runtime-manifest.json"
foreach ($required in @($pythonPath, $nodePath)) {
    if (-not (Test-Path $required)) { throw "runtime_missing: $required" }
}
if ($Development) {
    if (-not $SkipFrontend -and -not (Test-Path $frontendDevServer)) { throw "dev_dependency_missing: frontend/node_modules is absent. Run npm ci in frontend on the development machine." }
} else {
    foreach ($required in @($pythonPackages, $frontendServer, $runtimeManifest)) {
        if (-not (Test-Path $required)) { throw "runtime_missing: $required" }
    }
}
$env:EMAIL_AUTOMATION_ROOT = $rootPath
$env:EMAIL_AUTOMATION_DATA_DIR = $global:DataRoot
$env:PYTHONPATH = if ($Development) { Join-Path $rootPath "backend" } else { (Join-Path $rootPath "backend") + ";" + $pythonPackages }
$env:API_URL = "http://127.0.0.1:$script:BackendPort"
$env:NEXT_PUBLIC_API_URL = $env:API_URL
$env:APP_URL = "http://127.0.0.1:$script:FrontendPort"
$env:EMAIL_AUTOMATION_API_URL = $env:API_URL
$env:TACWORK_SERVER_URL = "http://127.0.0.1:$script:TacWorkServerPort"
$env:GOOGLE_REDIRECT_URI = "$($env:API_URL)/api/gmail/oauth/callback"
$env:CORS_ORIGINS = "http://127.0.0.1:$script:FrontendPort,http://localhost:$script:FrontendPort,http://127.0.0.1:$script:TacWorkWebPort,http://localhost:$script:TacWorkWebPort,app://email-automation"
$env:PORT = [string]$script:FrontendPort
$env:HOSTNAME = "127.0.0.1"
if ($Development) { $env:NEXT_DIST_DIR = ".next-dev" }

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
        if ($svc.data_root -eq $global:DataRoot) {
            $recordedPids = @(
                $svc.backend, $svc.consumer, $svc.frontend,
                $svc.tacwork_server, $svc.tacwork_engine, $svc.tacwork_web,
                $svc.launcher_backend, $svc.launcher_consumer, $svc.launcher_frontend,
                $svc.launcher_tacwork_server, $svc.launcher_tacwork_web
            ) | Where-Object { Test-WorkspaceProcess $_ } | Select-Object -Unique
        }
    } catch {}
}

$ports = @($script:BackendPort, $script:TacWorkServerPort, $script:TacWorkWebPort)
if (-not $SkipFrontend) { $ports += $script:FrontendPort }
$listeners = @{}
foreach ($port in $ports) { $listeners[$port] = Get-ListenerPid $port }

# A writable source checkout used to start its stack with backend/ as the data
# root. It is historical data, never a formal instance. Make that condition
# explicit before listener recovery can mistake it for a formal stack.
if (-not $Development -and $recordedPids.Count -eq 0) {
    $legacyStatus = Join-Path (Join-Path (Join-Path $rootPath 'backend') 'logs') 'run\services.json'
    if ((Test-Path $legacyStatus) -and @($listeners.Values | Where-Object { $_ }).Count -gt 0) {
        throw "legacy_stack_detected: an older compatibility stack is using the formal port group. Its status file is $legacyStatus. Stop it explicitly with scripts/stop-legacy-stack.ps1; its backend data is retained and is not migrated."
    }
}

# ---- Self-recognition fallback (runbook 5.5/8, probe-only) ----
# If services.json is missing/stale (lost PID record), listeners on $script:BackendPort/$script:FrontendPort
# that respond with OUR signatures are our own stack from a previous launch.
# Re-register them (never kill) so the adopt / stop-and-restart flow below can
# act on them. Unrecognized listeners still fall through to the
# external-conflict abort; the "never kill external processes" policy holds.
if ($recordedPids.Count -eq 0) {
    $recovered = @{}
    $backendPid = $listeners[$script:BackendPort]
    $frontendPid = if ($SkipFrontend) { $null } else { $listeners[$script:FrontendPort] }
    if ($backendPid -and (Test-WorkspaceProcess $backendPid)) {
        try {
            $h = Invoke-RestMethod "http://127.0.0.1:$script:BackendPort/api/health" -TimeoutSec 3
            if ($h.status -eq "ok" -and $null -ne $h.consumer -and (Test-BackendIdentity $h)) {
                $recovered.backend = $backendPid
                if (Test-WorkspaceProcess $h.consumer.pid) { $recovered.consumer = $h.consumer.pid }
            }
        } catch {}
    }
    if (-not $SkipFrontend -and $frontendPid -and (Test-WorkspaceProcess $frontendPid)) {
        try {
            $r = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$script:FrontendPort" -TimeoutSec 3
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
            if ($th.ready -and (Test-WorkspaceProcess $listeners[$script:TacWorkServerPort]) -and (Test-WorkspaceProcess $listeners[$script:TacWorkWebPort])) {
                $recovered.tacwork_server = $listeners[$script:TacWorkServerPort]
                $recovered.tacwork_web = $listeners[$script:TacWorkWebPort]
            }
        } catch {}
    }
    if ($recovered.Count -gt 0) {
        $recordedPids = @($recovered.Values) | Where-Object { $_ } | Select-Object -Unique
        $recoveredJson = @{ started_at = (Get-Date).ToString("o"); status = "recovered"; version = (Get-Content -Raw (Join-Path $rootPath 'VERSION')).Trim(); instance_id = [guid]::NewGuid().ToString(); data_root = $global:DataRoot; ports = @{ backend = $script:BackendPort; frontend = $script:FrontendPort; tacwork_server = $script:TacWorkServerPort; tacwork_web = $script:TacWorkWebPort } }
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

$existingBackendPid = $listeners[$script:BackendPort]
$existingFrontendPid = if ($SkipFrontend) { $null } else { $listeners[$script:FrontendPort] }
$existingTacWorkServerPid = $listeners[$script:TacWorkServerPort]
$existingTacWorkWebPid = $listeners[$script:TacWorkWebPort]

# If a complete, healthy stack is already running on our own PIDs, adopt it.
$allOursUp = ($existingBackendPid -and ($SkipFrontend -or $existingFrontendPid) -and $existingTacWorkServerPid -and $existingTacWorkWebPid)
if ($allOursUp) {
    $existingHealth = $null
    $existingFrontendReady = [bool]$SkipFrontend
    try {
        $existingHealth = Invoke-RestMethod "http://127.0.0.1:$script:BackendPort/api/health" -TimeoutSec 3
    } catch {}
    try {
        $existingFrontendReady = (
            Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$script:FrontendPort" -TimeoutSec 3
        ).StatusCode -eq 200
    } catch {}

    $isHealthyStack = (
        $existingHealth.status -eq "ok" -and
        (Test-BackendIdentity $existingHealth) -and
        (Test-WorkspaceProcess $existingHealth.consumer.pid) -and
        $existingHealth.consumer.healthy -eq $true -and
        $existingFrontendReady -and (Get-TacWorkHealth $rootPath).ready
    )
    if ($isHealthyStack) {
        @{
            started_at = (Get-Date).ToString("o")
            version = (Get-Content -Raw (Join-Path $rootPath 'VERSION')).Trim()
            instance_id = [guid]::NewGuid().ToString()
            data_root = $global:DataRoot
            data_mode = $global:DataMode
            ports = @{ backend = $script:BackendPort; frontend = $script:FrontendPort; tacwork_server = $script:TacWorkServerPort; tacwork_web = $script:TacWorkWebPort }
            adopted = $true
            backend = $existingBackendPid
            consumer = $existingHealth.consumer.pid
            frontend = $existingFrontendPid
            tacwork_server = $existingTacWorkServerPid
            tacwork_web = $existingTacWorkWebPid
            status = "ready"
        } | ConvertTo-Json | Set-Content -Encoding UTF8 -Path $pidPath
        Write-Output "Email Automation is already running and healthy."
        if (-not $SkipFrontend) { Write-Output "Frontend: http://127.0.0.1:$script:FrontendPort" }
        Write-Output "Backend:  http://127.0.0.1:$script:BackendPort/api/health"
        Write-Output "TACWork:  http://127.0.0.1:$script:TacWorkWebPort"
        Write-Output "PIDs: backend=$existingBackendPid consumer=$($existingHealth.consumer.pid) frontend=$existingFrontendPid"
        return
    }
}

# Our own stale/unhealthy instances: stop them, then start fresh.
& (Join-Path $PSScriptRoot "stop-stack.ps1") -Root $rootPath -Development:$Development

$stillOccupied = @()
foreach ($port in $ports) {
    $listenerPid = Get-ListenerPid $port
    if ($listenerPid) { $stillOccupied += "port $port pid=$listenerPid" }
}
if ($stillOccupied.Count -gt 0) {
    throw "service_start_failed: required ports still occupied after stopping our services: $($stillOccupied -join '; '). Stop the recorded Email Automation services, then retry."
}

$backend = Start-Process -FilePath $pythonPath `
    -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$script:BackendPort") `
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

$frontend = $null
$frontendSkippedReason = $null
if (-not $SkipFrontend) {
    # The standalone server reads its distDir (".next-prod") from server.js.
    # That directory is deliberately stripped from the shipped runtime (only the
    # static export runtime/frontend-static is packaged), so launching the
    # standalone server would fail with "Could not find a production build".
    # When the build dir is absent but the static export exists, skip the web
    # server instead of failing — the Electron shell serves the UI directly.
    $frontendDistDir = Join-Path $rootPath "runtime\frontend\.next-prod"
    $staticIndex = Join-Path $rootPath "runtime\frontend-static\index.html"
    if (-not $Development -and -not (Test-Path $frontendDistDir) -and (Test-Path $staticIndex)) {
        $frontendSkippedReason = "runtime/frontend/.next-prod is not shipped in this runtime; skipping the bundled web server."
    } else {
        $frontendArgs = if ($Development) { ('"{0}" dev --hostname 127.0.0.1 --port {1}' -f $frontendDevServer, $script:FrontendPort) } else { ('"{0}"' -f $frontendServer) }
        $frontendWorkingDirectory = if ($Development) { Join-Path $rootPath "frontend" } else { Join-Path $rootPath "runtime\frontend" }
        $frontend = Start-Process -FilePath $nodePath `
        -ArgumentList $frontendArgs `
        -WorkingDirectory $frontendWorkingDirectory `
        -RedirectStandardOutput (Join-Path $logsPath "frontend.log") `
        -RedirectStandardError (Join-Path $logsPath "frontend-error.log") `
        -WindowStyle Hidden -PassThru
    }
}
if ($frontendSkippedReason) { Write-Output ("Frontend: " + $frontendSkippedReason) }

$tacwork = Start-TacWorkRuntime -WorkspaceRoot $rootPath -LogsPath $logsPath -RunPath $runPath -PythonPath $pythonPath -PnpmPath $null

@{
    started_at = (Get-Date).ToString("o")
    version = (Get-Content -Raw (Join-Path $rootPath 'VERSION')).Trim()
    instance_id = [guid]::NewGuid().ToString()
    data_root = $global:DataRoot
    data_mode = $global:DataMode
    ports = @{ backend = $script:BackendPort; frontend = $script:FrontendPort; tacwork_server = $script:TacWorkServerPort; tacwork_web = $script:TacWorkWebPort }
    backend = $backend.Id
    consumer = $consumer.Id
    frontend = if ($frontend) { $frontend.Id } else { $null }
    tacwork_server = $tacwork.server_launcher.Id
    tacwork_web = $tacwork.web_launcher.Id
} | ConvertTo-Json | Set-Content -Encoding UTF8 -Path $pidPath

$deadline = (Get-Date).AddSeconds(75)
$backendReady = $false
$frontendReady = [bool]$SkipFrontend
$consumerReady = $false
$tacworkReady = $false
do {
    Start-Sleep -Milliseconds 750
    try {
        $health = Invoke-RestMethod "http://127.0.0.1:$script:BackendPort/api/health" -TimeoutSec 2
        $backendReady = $health.status -eq "ok"
        $consumerReady = $health.consumer.healthy -eq $true
    } catch {}
    try {
        $frontendReady = (Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$script:FrontendPort" -TimeoutSec 2).StatusCode -eq 200
    } catch {}
    try { $tacworkHealth = Get-TacWorkHealth $rootPath; $tacworkReady = $tacworkHealth.ready } catch {}
} while ((-not ($backendReady -and $frontendReady -and $consumerReady -and $tacworkReady)) -and (Get-Date) -lt $deadline)

if (-not ($backendReady -and $frontendReady -and $consumerReady -and $tacworkReady)) {
    $failureCode = if ($tacworkHealth -and -not $tacworkHealth.web_config) { "tacwork_runtime_config_mismatch" } else { "service_start_failed" }
    Write-Error "${failureCode}: backend=$backendReady frontend=$frontendReady consumer=$consumerReady tacwork=$tacworkReady. Check logs/*-error.log."
}

$actualBackendPid = Get-ListenerPid $script:BackendPort
$actualFrontendPid = if ($SkipFrontend) { $null } else { Get-ListenerPid $script:FrontendPort }
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
    version = (Get-Content -Raw (Join-Path $rootPath 'VERSION')).Trim()
    instance_id = [guid]::NewGuid().ToString()
    data_root = $global:DataRoot
    data_mode = $global:DataMode
    ports = @{ backend = $script:BackendPort; frontend = $script:FrontendPort; tacwork_server = $script:TacWorkServerPort; tacwork_web = $script:TacWorkWebPort }
    launcher_backend = $backend.Id
    launcher_consumer = $consumer.Id
    launcher_frontend = if ($frontend) { $frontend.Id } else { $null }
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
if (-not $SkipFrontend) { Write-Output "Frontend: http://127.0.0.1:$script:FrontendPort" }
Write-Output "Backend:  http://127.0.0.1:$script:BackendPort/api/health"
Write-Output "TACWork:  http://127.0.0.1:$script:TacWorkWebPort"
Write-Output "PIDs: backend=$actualBackendPid consumer=$actualConsumerPid frontend=$actualFrontendPid"
Write-Output "TACWork PIDs: server=$actualTacWorkServerPid engine=$actualTacWorkEnginePid web=$actualTacWorkWebPid mode=$($tacwork.runtime.mode)"
