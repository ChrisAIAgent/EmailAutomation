param([string]$Root = "")
$ErrorActionPreference = "Stop"
if (-not $Root) { $Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
$manifestPath = Join-Path (Join-Path $Root "runtime") "runtime-manifest.json"
if (-not (Test-Path -LiteralPath $manifestPath)) { throw "runtime_integrity_failed:manifest_missing" }
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$version = (Get-Content -LiteralPath (Join-Path $Root "VERSION") -Raw).Trim()
if ($manifest.version -ne $version) { throw "stale_runtime:manifest_version_mismatch" }
function Get-Sha256([string]$Path) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($sha.ComputeHash([System.IO.File]::ReadAllBytes($Path))) -replace '-', '')
    } finally { $sha.Dispose() }
}

# Accumulate up to 5 missing and 5 hash-mismatched files before failing,
# so the customer sees the full set of broken files in a single error
# report instead of a slow one-at-a-time iteration. Forbidden manifest
# entries (disposable build trees that must never ship) still fail-fast
# because they indicate a manifest-generation bug, not a transport gap.
$missing = New-Object System.Collections.Generic.List[string]
$hashMismatch = New-Object System.Collections.Generic.List[string]
foreach ($item in $manifest.files) {
    $relative = ([string]$item.path).Replace('/', '\')
    if ($relative -match '(^|\\)(frontend\.pre-|\.frontend-repair-|\.frontend-build|\.frontend-runtime-next)') {
        throw "runtime_integrity_failed:forbidden_manifest_entry:$relative"
    }
    $target = Join-Path $Root $relative
    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
        if ($missing.Count -lt 5) { [void]$missing.Add($relative) }
        continue
    }
    if ((Get-Sha256 $target) -ne $item.sha256) {
        if ($hashMismatch.Count -lt 5) { [void]$hashMismatch.Add($relative) }
    }
}
if ($missing.Count -gt 0 -or $hashMismatch.Count -gt 0) {
    $parts = @()
    if ($missing.Count -gt 0) { $parts += ("missing:" + ($missing -join ",")) }
    if ($hashMismatch.Count -gt 0) { $parts += ("hash_mismatch:" + ($hashMismatch -join ",")) }
    throw ("runtime_integrity_failed:" + ($parts -join ";"))
}
foreach ($relative in @('runtime\frontend-static\index.html','runtime\electron\Email Automation.exe','runtime\electron\resources\app\main.cjs')) {
    if (-not (Test-Path -LiteralPath (Join-Path $Root $relative))) { throw "runtime_integrity_failed:missing:$relative" }
}
Write-Output "Runtime integrity verified: version=$version files=$($manifest.files.Count)"
