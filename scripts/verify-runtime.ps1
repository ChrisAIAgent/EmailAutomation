param([string]$Root = "")
$ErrorActionPreference = "Stop"
if (-not $Root) { $Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
$manifestPath = Join-Path (Join-Path $Root "runtime") "runtime-manifest.json"
if (-not (Test-Path -LiteralPath $manifestPath)) { throw "runtime_integrity_failed:manifest_missing" }
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$version = (Get-Content -LiteralPath (Join-Path $Root "VERSION") -Raw).Trim()
if ($manifest.version -ne $version) { throw "stale_runtime:manifest_version_mismatch" }
foreach ($item in $manifest.files) {
    $relative = ([string]$item.path).Replace('/', '\')
    if ($relative -match '(^|\\)(frontend\.pre-|\.frontend-repair-|\.frontend-build|\.frontend-runtime-next)') { throw "runtime_integrity_failed:forbidden_manifest_entry:$relative" }
    $target = Join-Path $Root $relative
    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { throw "runtime_integrity_failed:missing:$relative" }
    if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne $item.sha256) { throw "runtime_integrity_failed:hash_mismatch:$relative" }
}
foreach ($relative in @('runtime\frontend-static\index.html','runtime\electron\Email Automation.exe','runtime\electron\resources\app\main.cjs')) {
    if (-not (Test-Path -LiteralPath (Join-Path $Root $relative))) { throw "runtime_integrity_failed:missing:$relative" }
}
Write-Output "Runtime integrity verified: version=$version files=$($manifest.files.Count)"
