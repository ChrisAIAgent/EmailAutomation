param(
    [string]$TacWorkRoot = $env:TACWORK_ROOT,
    [string]$ExpectedCommit = "",
    [string]$ExpectedBranch = "Dev",
    [switch]$SkipWebBuild
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$root = Split-Path -Parent $scriptDir
if (-not $TacWorkRoot) { $TacWorkRoot = Join-Path (Split-Path -Parent $root) "TACWork" }
$TacWorkRoot = (Resolve-Path -LiteralPath $TacWorkRoot).Path

# A release runtime is only meaningful when its exact upstream source can be
# reproduced.  Passing -ExpectedCommit turns these into hard gates; developers
# may still use the script locally without it.
$sourceCommit = [string](& git -C $TacWorkRoot rev-parse HEAD 2>$null | Select-Object -First 1)
$sourceCommit = $sourceCommit.Trim()
if (-not $sourceCommit) { throw "TACWorkRoot is not a readable Git checkout: $TacWorkRoot" }
$sourceBranchValue = (& git -C $TacWorkRoot branch --show-current 2>$null | Select-Object -First 1)
$sourceBranch = if ($null -eq $sourceBranchValue) { "" } else { ([string]$sourceBranchValue).Trim() }
$sourceDirty = [bool](& git -C $TacWorkRoot status --porcelain 2>$null)
if ($ExpectedCommit) {
    if ($sourceCommit -ne $ExpectedCommit) {
        throw "TACWork source commit mismatch. Expected $ExpectedCommit, found $sourceCommit. Build from a clean worktree at the approved commit."
    }
    if ($sourceDirty) { throw "TACWork source is dirty. Formal runtime builds require a clean worktree." }
    # A formal builder uses a detached temporary worktree at ExpectedCommit.
    # A detached checkout has no current branch, so validate a non-empty branch
    # when present and record the approved source branch in the manifest.
    if ($ExpectedBranch -and $sourceBranch -and $sourceBranch -ne $ExpectedBranch) {
        throw "TACWork source branch mismatch. Expected $ExpectedBranch, found $sourceBranch."
    }
}
$manifestBranch = if ($ExpectedCommit -and $ExpectedBranch) { $ExpectedBranch } else { $sourceBranch }

$serverSource = Join-Path $TacWorkRoot "apps\server\dist\bin\openwork-server.exe"
$engineSource = Join-Path $TacWorkRoot "apps\desktop\resources\sidecars\opencode.exe"
$webSource = Join-Path $TacWorkRoot "apps\app\dist"
$licenseSource = Join-Path $TacWorkRoot "LICENSE"
$pnpm = (Get-Command pnpm.cmd -ErrorAction Stop).Source

foreach ($required in @($serverSource, $engineSource, $licenseSource)) {
    if (-not (Test-Path -LiteralPath $required)) { throw "Required TACWork runtime artifact not found: $required" }
}

if (-not $SkipWebBuild) {
    $oldUrl = $env:VITE_OPENWORK_URL; $oldPort = $env:VITE_OPENWORK_PORT; $oldToken = $env:VITE_OPENWORK_TOKEN
    $env:VITE_OPENWORK_URL = "http://127.0.0.1:8787"; $env:VITE_OPENWORK_PORT = "8787"
    $env:VITE_OPENWORK_TOKEN = "email-automation-local-v1"
    try {
        Push-Location $TacWorkRoot
        & $pnpm --filter "@openwork/app" build
        if ($LASTEXITCODE -ne 0) { throw "TACWork Web build failed (exit $LASTEXITCODE)." }
    } finally {
        Pop-Location
        $env:VITE_OPENWORK_URL = $oldUrl; $env:VITE_OPENWORK_PORT = $oldPort; $env:VITE_OPENWORK_TOKEN = $oldToken
    }
}
if (-not (Test-Path (Join-Path $webSource "index.html"))) { throw "TACWork Web build output is missing: $webSource" }

$target = Join-Path $root "tacwork-runtime"
$serverTarget = Join-Path $target "server"; $engineTarget = Join-Path $target "engine"; $webTarget = Join-Path $target "web"
New-Item -ItemType Directory -Force -Path $serverTarget, $engineTarget | Out-Null
if (Test-Path $webTarget) { Remove-Item -LiteralPath $webTarget -Recurse -Force }
New-Item -ItemType Directory -Force -Path $webTarget | Out-Null

Copy-Item -LiteralPath $serverSource -Destination (Join-Path $serverTarget "openwork-server.exe") -Force
Copy-Item -LiteralPath $engineSource -Destination (Join-Path $engineTarget "opencode.exe") -Force
Copy-Item -Path (Join-Path $webSource "*") -Destination $webTarget -Recurse -Force
Copy-Item -LiteralPath $licenseSource -Destination (Join-Path $target "LICENSE-TACWORK.txt") -Force

[ordered]@{
    prepared_at = (Get-Date).ToString("o")
    source_branch = $manifestBranch
    source_commit = $sourceCommit
    source_dirty = $sourceDirty
    server_version = "0.18.12"
    server_port = 8787; web_port = 5173; workspace = "Email Automation root (resolved at launch)"
    contents = @("server/openwork-server.exe", "engine/opencode.exe", "web/", "LICENSE-TACWORK.txt")
} | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 (Join-Path $target "runtime-manifest.json")

$bytes = (Get-ChildItem $target -Recurse -File | Measure-Object Length -Sum).Sum
Write-Output "TACWork portable runtime prepared: $target"
Write-Output ("Runtime size: " + [math]::Round($bytes / 1MB, 1) + " MB")
