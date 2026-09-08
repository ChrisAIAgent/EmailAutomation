param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [switch]$Development
)

$ErrorActionPreference = "Continue"
$rootPath = (Resolve-Path $Root).Path
. (Join-Path $PSScriptRoot "data-dir.ps1") -Mode $(if ($Development) { 'Development' } else { 'Formal' })
. (Join-Path $PSScriptRoot "runtime-ports.ps1")
. (Join-Path $PSScriptRoot "tacwork-runtime.ps1")
$pidPath = Join-Path $global:DataRun "services.json"

function Test-WorkspaceProcess {
    param([object]$ProcessId)
    if (-not $ProcessId) { return $false }
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if (-not $proc) { return $false }
    $prefix = $rootPath.TrimEnd('\', '/') + '\'
    if ($proc.ExecutablePath -and ([string]$proc.ExecutablePath).StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    $pattern = '(?i)(?:^|["\s])' + [regex]::Escape($prefix)
    return [bool]($proc.CommandLine -and ([string]$proc.CommandLine -match $pattern))
}

if (Test-Path $pidPath) {
    $servicePids = Get-Content -Raw -Encoding UTF8 $pidPath | ConvertFrom-Json
    $recorded = @(
        $servicePids.backend, $servicePids.consumer, $servicePids.frontend,
        $servicePids.tacwork_server, $servicePids.tacwork_engine, $servicePids.tacwork_web,
        $servicePids.launcher_backend, $servicePids.launcher_consumer, $servicePids.launcher_frontend,
        $servicePids.launcher_tacwork_server, $servicePids.launcher_tacwork_web
    ) | Where-Object { $_ } | Select-Object -Unique
    $stateMatchesDirectory = $servicePids.data_root -eq $global:DataRoot
    foreach ($servicePid in $recorded) {
        if ($servicePid) {
            if ($stateMatchesDirectory -and (Test-WorkspaceProcess $servicePid)) {
                Stop-Process -Id ([int]$servicePid) -Force -ErrorAction SilentlyContinue
            } else {
                Write-Output "Skipped unverified recorded process: pid=$servicePid"
            }
        }
    }
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
}

# Round C fallback: services.json may be missing or stale while our own service
# processes still hold the port group (e.g. after an abnormal app exit). Identify
# each listener by port, verify the process identity (executable path OR command
# line contains this workspace root) and stop only verified own services.
# External holders are never killed; they are reported below.
$groupPorts = @(
    @{ Name = "backend";        Port = $script:BackendPort },
    @{ Name = "frontend";       Port = $script:FrontendPort },
    @{ Name = "tacwork_server"; Port = $script:TacWorkServerPort },
    @{ Name = "tacwork_web";    Port = $script:TacWorkWebPort }
)
$checkedPids = @{}
foreach ($entry in $groupPorts) {
    $listenerPid = Get-ListenerPid $entry.Port
    if (-not $listenerPid) { continue }
    if ($checkedPids.ContainsKey($listenerPid)) { continue }
    $checkedPids[$listenerPid] = $true
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$listenerPid" -ErrorAction SilentlyContinue
    if (-not $proc) { continue }
    $identity = ("{0} {1}" -f $proc.ExecutablePath, $proc.CommandLine)
    $expectedDataRoot = $global:DataRoot
    $dataPattern = '(?i)(?:^|["\s=])' + [regex]::Escape($expectedDataRoot.TrimEnd('\', '/')) + '(?:[\\/"\s]|$)'
    if ((Test-WorkspaceProcess $listenerPid) -and ($identity -match $dataPattern)) {
        Stop-Process -Id $listenerPid -Force -ErrorAction SilentlyContinue
        Write-Output "Stopped stale own service ($($entry.Name)): pid=$listenerPid name=$($proc.Name)"
    }
}

# Report-only check: never kill external listeners (runbook Section 5.5 / 8).
# Own recorded PIDs (services.json) and verified stale own listeners were stopped
# above; any remaining listener belongs to an external process and must be
# handled by the user, not this tool.
$remaining = netstat -ano | ForEach-Object {
    $parts = ($_.Trim() -split "\s+")
    if ($parts.Count -ge 5 -and
        ($parts[1] -match ":$script:BackendPort$" -or $parts[1] -match ":$script:FrontendPort$" -or
         $parts[1] -match ":$($script:TacWorkServerPort)$" -or $parts[1] -match ":$($script:TacWorkWebPort)$") -and
        $parts[-2] -eq "LISTENING") {
        "$($parts[1]) pid=$($parts[-1])"
    }
}
if ($remaining) {
    Write-Output "Warning: the following ports are still listening. These are NOT stopped automatically (runbook forbids killing external processes); stop the external process manually if needed:"
    $remaining | ForEach-Object { Write-Output "  Still listening: $_" }
} else {
    Write-Output "Email Automation services stopped."
}
