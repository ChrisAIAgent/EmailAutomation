param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Continue"
$rootPath = (Resolve-Path $Root).Path
. (Join-Path $PSScriptRoot "tacwork-runtime.ps1")
$pidPath = Join-Path $rootPath "logs\run\services.json"

if (Test-Path $pidPath) {
    $servicePids = Get-Content -Raw -Encoding UTF8 $pidPath | ConvertFrom-Json
    $recorded = @(
        $servicePids.backend, $servicePids.consumer, $servicePids.frontend,
        $servicePids.tacwork_server, $servicePids.tacwork_engine, $servicePids.tacwork_web,
        $servicePids.launcher_backend, $servicePids.launcher_consumer, $servicePids.launcher_frontend,
        $servicePids.launcher_tacwork_server, $servicePids.launcher_tacwork_web
    ) | Where-Object { $_ } | Select-Object -Unique
    foreach ($servicePid in $recorded) {
        if ($servicePid) {
            & taskkill.exe /PID $servicePid /T /F 2>$null | Out-Null
        }
    }
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
}

# Report-only check: never kill external listeners (runbook Section 5.5 / 8).
# Only our own recorded PIDs are stopped above; any remaining listener belongs
# to an external process and must be handled by the user, not this tool.
$remaining = netstat -ano | ForEach-Object {
    $parts = ($_.Trim() -split "\s+")
    if ($parts.Count -ge 5 -and
        ($parts[1] -match ":8000$" -or $parts[1] -match ":3000$" -or
         $parts[1] -match ":$($script:TacWorkServerPort)$" -or $parts[1] -match ":$($script:TacWorkWebPort)$") -and
        $parts[-2] -eq "LISTENING") {
        "$($parts[1]) pid=$($parts[-1])"
    }
}
if ($remaining) {
    Write-Output "Warning: the following ports are still listening. These are NOT stopped automatically (runbook forbids killing external processes); stop the external process manually if needed:"
    $remaining | ForEach-Object { Write-Output "  Still listening: $_" }
} else {
    Write-Output "Email Automation and TACWork services stopped."
}
