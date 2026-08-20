$script:TacWorkServerPort = if ($env:TACWORK_SERVER_PORT) { [int]$env:TACWORK_SERVER_PORT } else { 8787 }
$script:TacWorkWebPort = if ($env:TACWORK_WEB_PORT) { [int]$env:TACWORK_WEB_PORT } else { 5173 }
# Fixed local loopback identifier shared with the co-located TACWork server.
# Not a security credential; intentionally not overridable via user config.
$script:TacWorkClientToken = "email-automation-local-v1"

function Get-ListenerPid([int]$Port) {
    $row = netstat -ano | ForEach-Object {
        $parts = ($_.Trim() -split "\s+")
        if ($parts.Count -ge 5 -and $parts[1] -match ":$Port$" -and $parts[-2] -eq "LISTENING") {
            [pscustomobject]@{ Pid = [int]$parts[-1] }
        }
    } | Select-Object -First 1
    if ($row) { return $row.Pid }
    return $null
}

function Resolve-TacWorkRuntime([string]$WorkspaceRoot) {
    $candidates = @()
    if ($env:TACWORK_ROOT) { $candidates += $env:TACWORK_ROOT }
    $candidates += (Join-Path $WorkspaceRoot "tacwork-runtime")
    $candidates += (Join-Path (Split-Path -Parent $WorkspaceRoot) "TACWork")

    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        $resolved = (Resolve-Path -LiteralPath $candidate).Path
        $bundledServer = Join-Path $resolved "server\openwork-server.exe"
        $bundledEngine = Join-Path $resolved "engine\opencode.exe"
        $bundledWeb = Join-Path $resolved "web"
        if ((Test-Path $bundledServer) -and (Test-Path $bundledEngine) -and (Test-Path (Join-Path $bundledWeb "index.html"))) {
            return [pscustomobject]@{
                mode = "bundled"; root = $resolved; server = $bundledServer; engine = $bundledEngine
                web_root = $bundledWeb; web_working_directory = $bundledWeb
            }
        }

        $sourceServer = Join-Path $resolved "apps\server\dist\bin\openwork-server.exe"
        $sourceEngine = Join-Path $resolved "apps\desktop\resources\sidecars\opencode.exe"
        if ((Test-Path $sourceServer) -and (Test-Path $sourceEngine) -and (Test-Path (Join-Path $resolved "apps\app\package.json"))) {
            return [pscustomobject]@{
                mode = "source"; root = $resolved; server = $sourceServer; engine = $sourceEngine
                web_root = $null; web_working_directory = $resolved
            }
        }
    }
    throw "TACWork runtime not found. Run scripts\prepare-tacwork-runtime.ps1 or set TACWORK_ROOT to a valid TACWork checkout."
}

function Get-TacWorkHealth([string]$WorkspaceRoot) {
    $serverReady = $false; $webReady = $false; $engineReady = $false; $workspaceReady = $false; $status = $null
    $headers = @{ Authorization = "Bearer $script:TacWorkClientToken" }
    try {
        $serverHealth = Invoke-RestMethod "http://127.0.0.1:$script:TacWorkServerPort/health" -TimeoutSec 2
        $serverReady = $serverHealth.ok -eq $true
    } catch {}
    try {
        $status = Invoke-RestMethod "http://127.0.0.1:$script:TacWorkServerPort/status" -Headers $headers -TimeoutSec 2
        if ($status.workspace.path) {
            $expected = [IO.Path]::GetFullPath($WorkspaceRoot).TrimEnd('\')
            $actual = [IO.Path]::GetFullPath([string]$status.workspace.path).TrimEnd('\')
            $workspaceReady = $actual.Equals($expected, [StringComparison]::OrdinalIgnoreCase)
        }
    } catch {}
    try {
        $engineHealth = Invoke-RestMethod "http://127.0.0.1:$script:TacWorkServerPort/opencode/global/health" -Headers $headers -TimeoutSec 2
        $engineReady = $engineHealth.healthy -eq $true
    } catch {}
    try { $webReady = (Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$script:TacWorkWebPort" -TimeoutSec 2).StatusCode -eq 200 } catch {}
    return [pscustomobject]@{
        server = $serverReady; web = $webReady; engine = $engineReady; workspace = $workspaceReady
        ready = ($serverReady -and $webReady -and $engineReady -and $workspaceReady); status = $status
    }
}

function Set-EmailAutomationMcpCommand([string]$WorkspaceRoot) {
    # OpenCode starts local MCP processes from an engine-owned working
    # directory. Normalize this one Workspace-local command before each start
    # so it remains portable when the installation folder changes.
    $configPath = Join-Path $WorkspaceRoot "opencode.jsonc"
    $pythonPath = Join-Path $WorkspaceRoot "tools\python\python.exe"
    $serverPath = Join-Path $WorkspaceRoot "scripts\mcp_server.py"
    if (-not (Test-Path $configPath) -or -not (Test-Path $pythonPath) -or -not (Test-Path $serverPath)) {
        throw "email_automation_mcp_config_missing"
    }
    $commandJson = @($pythonPath, $serverPath) | ConvertTo-Json -Compress
    $raw = [IO.File]::ReadAllText($configPath)
    $pattern = '("email_automation"\s*:\s*\{\s*"type"\s*:\s*"local"\s*,\s*"enabled"\s*:\s*true\s*,\s*"command"\s*:\s*)\[[^\]]*\]'
    $updated = $raw -replace $pattern, ('$1' + $commandJson)
    # A previous clean start may already have written the same absolute command.
    # That is valid and must not prevent a later restart.
    if ($updated -eq $raw) {
        if ($raw -notmatch $pattern) { throw "email_automation_mcp_config_unrecognized" }
        return
    }
    [IO.File]::WriteAllText($configPath, $updated, [Text.UTF8Encoding]::new($false))
}

function Start-TacWorkRuntime(
    [string]$WorkspaceRoot, [string]$LogsPath, [string]$RunPath,
    [string]$PythonPath, [string]$PnpmPath
) {
    $runtime = Resolve-TacWorkRuntime $WorkspaceRoot
    Set-EmailAutomationMcpCommand $WorkspaceRoot
    $hostToken = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
    $oldManage = $env:OPENWORK_MANAGE_OPENCODE; $oldEngine = $env:OPENWORK_OPENCODE_BIN
    $oldConfig = $env:OPENWORK_SERVER_CONFIG; $oldTokenStore = $env:OPENWORK_TOKEN_STORE
    $env:OPENWORK_MANAGE_OPENCODE = "1"; $env:OPENWORK_OPENCODE_BIN = $runtime.engine
    $env:OPENWORK_SERVER_CONFIG = Join-Path $RunPath "tacwork-server.json"
    $env:OPENWORK_TOKEN_STORE = Join-Path $RunPath "tacwork-tokens.json"
    try {
        $serverArgs = "--workspace `"$WorkspaceRoot`" --host 127.0.0.1 --port $script:TacWorkServerPort --token $script:TacWorkClientToken --host-token $hostToken --approval auto --cors http://127.0.0.1:$script:TacWorkWebPort,http://localhost:$script:TacWorkWebPort,http://127.0.0.1:3000,http://localhost:3000"
        $server = Start-Process -FilePath $runtime.server -ArgumentList $serverArgs -WorkingDirectory $runtime.root `
            -RedirectStandardOutput (Join-Path $LogsPath "tacwork-server.log") `
            -RedirectStandardError (Join-Path $LogsPath "tacwork-server-error.log") -WindowStyle Hidden -PassThru
    } finally {
        $env:OPENWORK_MANAGE_OPENCODE = $oldManage; $env:OPENWORK_OPENCODE_BIN = $oldEngine
        $env:OPENWORK_SERVER_CONFIG = $oldConfig; $env:OPENWORK_TOKEN_STORE = $oldTokenStore
    }

    if ($runtime.mode -eq "bundled") {
        $spaScript = Join-Path $PSScriptRoot "serve-spa.py"
        $webArgs = "`"$spaScript`" --root `"$($runtime.web_root)`" --host 127.0.0.1 --port $script:TacWorkWebPort"
        $web = Start-Process -FilePath $PythonPath -ArgumentList $webArgs -WorkingDirectory $WorkspaceRoot `
            -RedirectStandardOutput (Join-Path $LogsPath "tacwork-web.log") `
            -RedirectStandardError (Join-Path $LogsPath "tacwork-web-error.log") -WindowStyle Hidden -PassThru
    } else {
        if (-not $PnpmPath) { throw "pnpm.cmd is required to run TACWork from source." }
        $oldUrl = $env:VITE_OPENWORK_URL; $oldPort = $env:VITE_OPENWORK_PORT; $oldToken = $env:VITE_OPENWORK_TOKEN
        $env:VITE_OPENWORK_URL = "http://127.0.0.1:$script:TacWorkServerPort"
        $env:VITE_OPENWORK_PORT = [string]$script:TacWorkServerPort; $env:VITE_OPENWORK_TOKEN = $script:TacWorkClientToken
        try {
            $web = Start-Process -FilePath $PnpmPath `
                -ArgumentList @("--filter", "@openwork/app", "exec", "vite", "--host", "127.0.0.1", "--port", [string]$script:TacWorkWebPort, "--strictPort") `
                -WorkingDirectory $runtime.web_working_directory `
                -RedirectStandardOutput (Join-Path $LogsPath "tacwork-web.log") `
                -RedirectStandardError (Join-Path $LogsPath "tacwork-web-error.log") -WindowStyle Hidden -PassThru
        } finally {
            $env:VITE_OPENWORK_URL = $oldUrl; $env:VITE_OPENWORK_PORT = $oldPort; $env:VITE_OPENWORK_TOKEN = $oldToken
        }
    }
    return [pscustomobject]@{ runtime = $runtime; server_launcher = $server; web_launcher = $web }
}
