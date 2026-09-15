<#
.SYNOPSIS
  Preloads the approved TACWork pnpm store for a later offline formal build.

.DESCRIPTION
  Run only on a controlled build machine before disconnecting it. The formal
  build script never calls this helper and always uses --offline. No customer
  data, OAuth credentials or application runtime files are read or written.
#>
param(
    [string]$TacWorkRoot = $env:TACWORK_ROOT,
    [string]$PnpmStore = ""
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$root = Split-Path -Parent $scriptDir
if (-not $TacWorkRoot) { $TacWorkRoot = Join-Path (Split-Path -Parent $root) "TACWork" }
$TacWorkRoot = (Resolve-Path -LiteralPath $TacWorkRoot).Path
if (-not $PnpmStore) { $PnpmStore = Join-Path $root "offline-cache\npm-cache\tacwork-pnpm-store" }
$pnpm = (Get-Command pnpm.cmd -ErrorAction Stop).Source
New-Item -ItemType Directory -Force -Path $PnpmStore | Out-Null
$commit = [string](& git -C $TacWorkRoot rev-parse HEAD 2>$null | Select-Object -First 1)
$commit = $commit.Trim()
if (-not $commit) { throw "TACWorkRoot is not a readable Git checkout: $TacWorkRoot" }
$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ("ea-tacwork-cache-" + $PID + "-" + [Guid]::NewGuid().ToString("N"))
$created = $false
try {
    & git -C $TacWorkRoot worktree add --detach $tempRoot $commit
    if ($LASTEXITCODE -ne 0) { throw "Could not create clean TACWork cache worktree." }
    $created = $true
    $oldCi = $env:CI; $env:CI = "true"
    Push-Location $tempRoot
    try {
        & $pnpm fetch --frozen-lockfile --store-dir $PnpmStore
        if ($LASTEXITCODE -ne 0) { throw "TACWork cache seed failed." }
    } finally {
        Pop-Location
        $env:CI = $oldCi
    }
} finally {
    if ($created -and (Test-Path -LiteralPath $tempRoot)) { & git -C $TacWorkRoot worktree remove --force $tempRoot }
}
Write-Output "TACWork pnpm cache seeded: $PnpmStore"
