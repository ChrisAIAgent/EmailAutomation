param([string]$Root = (Split-Path -Parent $PSScriptRoot))
$ErrorActionPreference = "Stop"
$local = $env:LOCALAPPDATA
if (-not $local) { throw "LOCALAPPDATA is required for isolated development data." }
$env:EMAIL_AUTOMATION_BACKEND_PORT = "28000"
$env:EMAIL_AUTOMATION_FRONTEND_PORT = "28001"
$env:TACWORK_SERVER_PORT = "28002"
$env:TACWORK_WEB_PORT = "28003"
& (Join-Path $PSScriptRoot "start-stack.ps1") -Root $Root -Development
