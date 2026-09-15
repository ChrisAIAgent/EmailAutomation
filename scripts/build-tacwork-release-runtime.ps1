<#
.SYNOPSIS
  Prepares TACWork runtime artifacts from a temporary clean worktree.

.DESCRIPTION
  The current TACWork checkout may contain operator notes or local runtime
  state. Formal Email Automation builds never copy from that tree directly.
  This script creates a detached worktree at the approved commit, delegates to
  prepare-tacwork-runtime.ps1 with provenance gates, and removes only that
  uniquely-created temporary worktree afterwards.
#>
param(
    [string]$TacWorkRoot = $env:TACWORK_ROOT,
    [string]$ExpectedCommit = "a8a6156",
    [string]$ExpectedBranch = "Dev"
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$root = Split-Path -Parent $scriptDir
if (-not $TacWorkRoot) { $TacWorkRoot = Join-Path (Split-Path -Parent $root) "TACWork" }
$TacWorkRoot = (Resolve-Path -LiteralPath $TacWorkRoot).Path
$prepare = Join-Path $scriptDir "prepare-tacwork-runtime.ps1"

$resolvedCommit = (& git -C $TacWorkRoot rev-parse ($ExpectedCommit + "^{commit}") 2>$null | Select-Object -First 1).Trim()
if (-not $resolvedCommit) { throw "Approved TACWork commit is unavailable: $ExpectedCommit" }
if ($resolvedCommit -notlike ($ExpectedCommit + "*")) {
    throw "Approved TACWork commit mismatch. Expected prefix $ExpectedCommit, resolved $resolvedCommit."
}
$branchCommit = (& git -C $TacWorkRoot rev-parse ($ExpectedBranch + "^{commit}") 2>$null | Select-Object -First 1).Trim()
if (-not $branchCommit) { throw "Approved TACWork branch is unavailable: $ExpectedBranch" }
& git -C $TacWorkRoot merge-base --is-ancestor $resolvedCommit $branchCommit
if ($LASTEXITCODE -ne 0) { throw "Approved commit $resolvedCommit is not reachable from TACWork branch $ExpectedBranch." }

$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ("ea-tacwork-release-" + $PID + "-" + [Guid]::NewGuid().ToString("N"))
$created = $false
try {
    & git -C $TacWorkRoot worktree add --detach $tempRoot $resolvedCommit
    if ($LASTEXITCODE -ne 0) { throw "Could not create clean TACWork release worktree." }
    $created = $true
    $pnpm = (Get-Command pnpm.cmd -ErrorAction Stop).Source
    # Build every release artifact inside the clean worktree. --offline makes
    # a missing build cache a visible release failure instead of silently
    # fetching a moving dependency or borrowing ignored artifacts from the
    # operator's working checkout.
    Push-Location $tempRoot
    try {
        & $pnpm install --frozen-lockfile --offline
        if ($LASTEXITCODE -ne 0) { throw "TACWork offline dependency install failed in the clean worktree." }
        & $pnpm --filter "openwork-server" build:bin
        if ($LASTEXITCODE -ne 0) { throw "TACWork server binary build failed in the clean worktree." }
        & $pnpm --filter "@openwork/desktop" prepare:sidecar
        if ($LASTEXITCODE -ne 0) { throw "TACWork OpenCode sidecar preparation failed in the clean worktree." }
    } finally {
        Pop-Location
    }
    & $prepare -TacWorkRoot $tempRoot -ExpectedCommit $resolvedCommit -ExpectedBranch $ExpectedBranch
    if ($LASTEXITCODE -ne 0) { throw "TACWork runtime preparation failed." }
} finally {
    if ($created -and (Test-Path -LiteralPath $tempRoot)) {
        & git -C $TacWorkRoot worktree remove --force $tempRoot
        if ($LASTEXITCODE -ne 0) { Write-Warning "Temporary TACWork worktree needs manual cleanup: $tempRoot" }
    }
}
