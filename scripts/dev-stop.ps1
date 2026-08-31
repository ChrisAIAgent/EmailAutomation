param([string]$Root = (Split-Path -Parent $PSScriptRoot))

$ErrorActionPreference = "Stop"
if (-not $env:LOCALAPPDATA) { throw "LOCALAPPDATA is required for isolated development data." }
$env:EMAIL_AUTOMATION_BACKEND_PORT = "28000"
$env:EMAIL_AUTOMATION_FRONTEND_PORT = "28001"
$env:TACWORK_SERVER_PORT = "28002"
$env:TACWORK_WEB_PORT = "28003"
& (Join-Path $PSScriptRoot "stop-stack.ps1") -Root $Root -Development
exit $LASTEXITCODE
