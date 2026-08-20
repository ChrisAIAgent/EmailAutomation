param(
    [switch]$Apply,
    [string]$Confirm = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot "backend\.venv\Scripts\python.exe"
$tool = Join-Path $projectRoot "backend\scripts\reset_operational_data.py"

$arguments = @($tool)
if ($Apply) {
    $arguments += "--apply"
    $arguments += "--confirm"
    $arguments += $Confirm
}

Push-Location (Join-Path $projectRoot "backend")
try {
    & $python @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Operational reset failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
