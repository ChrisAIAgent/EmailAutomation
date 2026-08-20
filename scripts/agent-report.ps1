param(
    [string]$ApiBase = "http://127.0.0.1:8000",
    [string]$OutputDir = "reports"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$target = Join-Path $root $OutputDir
New-Item -ItemType Directory -Force -Path $target | Out-Null

$health = Invoke-RestMethod "$ApiBase/api/health" -TimeoutSec 5
$gmail = Invoke-RestMethod "$ApiBase/api/gmail/status" -TimeoutSec 5
$pause = Invoke-RestMethod "$ApiBase/api/system/pause" -TimeoutSec 5
$metrics = Invoke-RestMethod "$ApiBase/api/dashboard/metrics" -TimeoutSec 5
$automations = Invoke-RestMethod "$ApiBase/api/automation" -TimeoutSec 5
$approvalResponse = Invoke-RestMethod "$ApiBase/api/approvals?status=pending" -TimeoutSec 10
$approvals = if ($null -eq $approvalResponse) { @() } else { @($approvalResponse) }

$date = Get-Date -Format "yyyy-MM-dd"
$path = Join-Path $target "agent-report-$date.md"
$lines = @(
    "# Email Automation Agent Report",
    "",
    "Generated: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz')",
    "Gmail account: $($gmail.email)",
    "Backend: $($health.status)",
    "Consumer: $($health.consumer.state), healthy=$($health.consumer.healthy)",
    "System paused: $($pause.global_pause)",
    "Real sending: $($health.real_send)",
    "",
    "## Metrics",
    "",
    "- Sent today: $($metrics.sent_today)",
    "- Replies: $($metrics.replies)",
    "- Positive replies: $($metrics.positive_replies)",
    "- Needs reply: $($metrics.needs_reply)",
    "- Pending approvals: $($metrics.pending_approvals)",
    "- Scheduled follow-ups: $($metrics.scheduled_follow_ups)",
    "- Failed tasks: $($metrics.failed_tasks)",
    "",
    "## Send Mode",
    "",
    "- Real send enabled: $($metrics.real_send_enabled)",
    "- Draft only (no real send): $($metrics.draft_only)",
    "- Restricted recipient list configured: $($metrics.restricted_allowlist_configured)  (False = no extra recipient restriction; True = only allowlisted recipients receive)",
    "",
    "## Inbox Triage",
    ""
)

# P2-1: honest inbox triage truth (read-only, reuses existing /api/inbox/stats)
try {
    $inboxStats = Invoke-RestMethod "$ApiBase/api/inbox/stats" -TimeoutSec 5
    $lines += "- Total threads: $($inboxStats.total)"
    $lines += "- Unprocessed (no intent yet): $($inboxStats.unprocessed)"
    if ($inboxStats.counts.PSObject.Properties.Count) {
        foreach ($kv in $inboxStats.counts.PSObject.Properties) {
            $lines += "-   [$($kv.Name)]: $($kv.Value)"
        }
    }
} catch {
    $lines += "- (inbox/stats unavailable: $($_.Exception.Message))"
}
$lines += ""

# P2-2: human_review must be reported separately from needs_reply
$lines += "## Needs Human Review (not sales leads)"
$lines += ""
$lines += "- human_review count: $($metrics.human_review)  (excluded from Needs Reply; Agent-owned review queue)"
$lines += ""

# P2-3: stopped / unsubscribed with reasons (honest stop accounting)
$lines += "## Stopped / Unsubscribed"
$lines += ""
if ($metrics.suppressions -and $metrics.suppressions.total -gt 0) {
    $lines += "- Total suppressions: $($metrics.suppressions.total)"
    foreach ($kv in $metrics.suppressions.by_reason.PSObject.Properties) {
        $lines += "-   [$($kv.Name)]: $($kv.Value)"
    }
} else {
    $lines += "- None"
}
$lines += ""

$lines += @(
    "## Pending Approvals",
    ""
)

if ($approvals.Count -eq 0) {
    $lines += "- None"
} else {
    foreach ($item in $approvals) {
        $identity = @($item.contact_name, $item.contact_company) |
            Where-Object { $_ } |
            Select-Object -Unique
        $who = if ($identity.Count) { $identity -join " / " } else { $item.to_email }
        $lines += "- #$($item.id) [$($item.kind)] $who <$($item.to_email)>: $($item.subject)"
    }
}

$lines += @("", "## Automations", "")
if (@($automations.items).Count -eq 0) {
    $lines += "- None"
} else {
    foreach ($item in @($automations.items)) {
        $lines += "- #$($item.id) $($item.name): $($item.status), next=$($item.next_run_at), last=$($item.last_status)"
        $detail = Invoke-RestMethod "$ApiBase/api/automation/$($item.id)" -TimeoutSec 10
        $lastRun = @($detail.runs) | Select-Object -First 1
        if ($lastRun) {
            $lines += "  - Run #$($lastRun.id): $($lastRun.status), approvals=$($lastRun.approvals_created), drafts=$($lastRun.drafts_created), stopped=$($lastRun.replies_stopped), error=$($lastRun.error)"
        }
        # P2-4: surface the full Run status distribution so a `partial` run is
        # never silently read as a fully successful one.
        $runs = @($detail.runs)
        if ($runs.Count) {
            $dist = @{}
            foreach ($r in $runs) {
                $key = "$($r.status)"
                $dist[$key] = [int]$dist[$key] + 1
            }
            $distLine = ($dist.Keys | ForEach-Object { "$_=$($dist[$_])" }) -join ", "
            $lines += "  - Run status distribution (all runs): $distLine"
        }
    }
}

$lines += @(
    "",
    "## Agent Decision",
    "",
    "- Review pending approvals before sending.",
    "- Investigate failed tasks when the count increases.",
    "- Do not report queued or running Automations as completed."
)

$lines | Set-Content -Path $path -Encoding UTF8
Write-Output $path
