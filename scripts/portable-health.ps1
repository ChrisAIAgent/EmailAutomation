<#
.SYNOPSIS
  Report the portable workspace runtime paths and service health.

.DESCRIPTION
  Prints the workspace root, the resolved Python/Node/npm runtime paths, and the
  status of Backend, Consumer, Frontend, Gmail connection, system pause and
  real-send flag. It never prints secrets, tokens, API keys or .env contents.
#>

$ErrorActionPreference = "Continue"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$root = Split-Path -Parent $scriptDir
. (Join-Path $scriptDir "tacwork-runtime.ps1")

function Pick($rel, $cmd) {
    $bundled = Join-Path $root $rel
    if (Test-Path $bundled) { return $bundled }
    $sys = Get-Command $cmd -ErrorAction SilentlyContinue
    if ($sys) { return $sys.Source }
    return "(not found)"
}

$pyBin  = Pick "tools\python\python.exe" "python"
$nodeBin = Pick "tools\node\node.exe" "node"
$npmBin = Pick "tools\node\npm.cmd" "npm.cmd"

Write-Output "======================================================"
Write-Output " Email Automation - Portable Workspace Health"
Write-Output "======================================================"
Write-Output ("Workspace root  : " + $root)
Write-Output ("Python runtime   : " + $pyBin)
Write-Output ("Node runtime     : " + $nodeBin)
Write-Output ("npm runtime      : " + $npmBin)
Write-Output "TACWork Server   : http://127.0.0.1:8787"
Write-Output "TACWork Web      : http://127.0.0.1:5173"
try {
    $twRuntime = Resolve-TacWorkRuntime $root
    Write-Output ("TACWork runtime  : " + $twRuntime.root + " [" + $twRuntime.mode + "]")
} catch {
    Write-Output "TACWork runtime  : (not found)"
}
Write-Output ""

function Show($label, $val) { Write-Output ("{0,-18}: {1}" -f $label, $val) }

try {
    $h = Invoke-RestMethod "http://127.0.0.1:8000/api/health" -TimeoutSec 5
    Show "Backend status" $h.status
    Show "Consumer healthy" $h.consumer.healthy
    Show "Consumer state" $h.consumer.state
    Show "Real send" $h.real_send
    Show "Gmail connected" $h.gmail_connected
} catch {
    Show "Backend status" "UNREACHABLE (is portable-start running?)"
}

try {
    $tw = Get-TacWorkHealth $root
    Show "TACWork Server" $tw.server
    Show "TACWork Web" $tw.web
    Show "OpenCode Engine" $tw.engine
    Show "Workspace locked" $tw.workspace
    Show "TACWork ready" $tw.ready
} catch {
    Show "TACWork ready" "UNREACHABLE"
}

$servicesPath = Join-Path $root "logs\run\services.json"
if (Test-Path $servicesPath) {
    try {
        $services = Get-Content -Raw -Encoding UTF8 $servicesPath | ConvertFrom-Json
        Show "Recorded status" $services.status
        Show "TACWork mode" $services.tacwork_runtime_mode
    } catch { Show "services.json" "INVALID" }
}

try {
    $g = Invoke-RestMethod "http://127.0.0.1:8000/api/gmail/status" -TimeoutSec 5
    Show "Gmail account" $g.email
    Show "Gmail connected" $g.connected
} catch {
    Show "Gmail status" "UNREACHABLE"
}

try {
    $p = Invoke-RestMethod "http://127.0.0.1:8000/api/system/pause" -TimeoutSec 5
    Show "System paused" $p.global_pause
} catch {
    Show "System pause" "UNREACHABLE"
}

try {
    Invoke-RestMethod "http://127.0.0.1:8000/api/agent/health" -TimeoutSec 5 | Out-Null
    Show "Agent health" "ok"
} catch {
    Show "Agent health" "UNREACHABLE"
}

try {
    $code = (Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:3000" -TimeoutSec 5).StatusCode
    Show "Frontend (3000)" $code
} catch {
    Show "Frontend (3000)" "UNREACHABLE"
}

Write-Output ""
Write-Output "NOTE: This report never prints secrets, tokens, API keys or .env contents."
