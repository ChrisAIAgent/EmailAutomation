param(
    [string]$ApiBase = "",
    [switch]$RequireRealSend,
    [switch]$RequireEmptyOperationalData,
    [switch]$Json
)

$ErrorActionPreference = "Stop"
if (-not $ApiBase) {
    $port = if ($env:EMAIL_AUTOMATION_BACKEND_PORT) { $env:EMAIL_AUTOMATION_BACKEND_PORT } else { "18000" }
    $ApiBase = "http://127.0.0.1:$port"
}
try {
    $health = Invoke-RestMethod "$ApiBase/api/health" -TimeoutSec 5
    $gmail = Invoke-RestMethod "$ApiBase/api/gmail/status" -TimeoutSec 5
    $pause = Invoke-RestMethod "$ApiBase/api/system/pause" -TimeoutSec 5
    $agent = Invoke-RestMethod "$ApiBase/api/agent/health" -TimeoutSec 10
} catch {
    Write-Error "Agent preflight could not reach the API: $($_.Exception.Message)"
}

# Gmail connection truth comes from the merged /api/health (gmail_connected),
# falling back to /api/gmail/status.connected for an older backend.
$gmailConnected = if ($null -ne $health.gmail_connected) { $health.gmail_connected } else { $gmail.connected }
$gmailAccount = if ($null -ne $health.gmail_account) { $health.gmail_account } else { $gmail.email }

$report = [pscustomobject]@{
    api_status        = $health.status
    db_ok             = $health.db_ok
    consumer_healthy  = $health.consumer.healthy
    consumer_state    = $health.consumer.state
    consumer_pid      = $health.consumer.pid
    consumer_heartbeat = $health.consumer.heartbeat_at
    real_send         = $health.real_send
    gmail_configured  = $health.gmail_configured
    gmail_connected   = $gmailConnected
    gmail_account     = $gmailAccount
    llm_configured    = $health.llm_configured
    system_paused     = $pause.global_pause
    agent_backends    = @($agent).Count
}

if ($Json) {
    $report | ConvertTo-Json -Depth 4
} else {
    $report | Format-List
}

$failures = @()
if ($health.status -ne "ok") { $failures += "backend is not healthy" }
if ($health.consumer.healthy -ne $true) { $failures += "Huey consumer is not healthy" }
if ($gmailConnected -ne $true) { $failures += "Gmail is not connected" }
if ($pause.global_pause -eq $true) { $failures += "system is globally paused" }
if (@($agent).Count -eq 0) { $failures += "no Agent backend is available" }
if ($RequireRealSend -and $health.real_send -ne $true) {
    $failures += "real sending is disabled"
}
if ($RequireEmptyOperationalData) {
    $contacts = Invoke-RestMethod "$ApiBase/api/contacts" -TimeoutSec 5
    $campaigns = Invoke-RestMethod "$ApiBase/api/campaigns" -TimeoutSec 5
    $automations = Invoke-RestMethod "$ApiBase/api/automation" -TimeoutSec 5
    $approvals = Invoke-RestMethod "$ApiBase/api/approvals?status=pending" -TimeoutSec 5
    $operationalCounts = [ordered]@{
        contacts = if ($null -eq $contacts) { 0 } else { @($contacts).Count }
        campaigns = if ($null -eq $campaigns) { 0 } else { @($campaigns).Count }
        automations = if ($null -eq $automations.items) { 0 } else { @($automations.items).Count }
        pending_approvals = if ($null -eq $approvals) { 0 } else { @($approvals).Count }
    }
    if ($Json) {
        [pscustomobject]$operationalCounts | ConvertTo-Json -Depth 2
    } else {
        [pscustomobject]$operationalCounts | Format-List
    }
    if (($operationalCounts.Values | Measure-Object -Sum).Sum -ne 0) {
        $failures += "operational data is not empty"
    }
}
if ($failures.Count) {
    throw "Agent preflight failed: $($failures -join '; ')"
}

Write-Output "Agent preflight passed."
