param(
    [switch]$Apply,
    [string]$Confirm = ""
)

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot "backend\.venv\Scripts\python.exe"
$tool = Join-Path $projectRoot "backend\scripts\reset_e2e_test_data.py"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python virtual environment not found: $python"
}

$arguments = @($tool)
if ($Apply) {
    $arguments += "--apply"
    $arguments += "--confirm"
    $arguments += $Confirm
}

Push-Location (Join-Path $projectRoot "backend")
try {
    & $python @arguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
