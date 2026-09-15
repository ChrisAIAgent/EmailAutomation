<#
.SYNOPSIS
  Validates the official x64 VC++ redistributable embedded by the installer.

.DESCRIPTION
  The executable itself is a release asset and is intentionally not stored in
  source control. The companion JSON pins its SHA-256 and expected publisher;
  a formal build fails if either cannot be proven.
#>
param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [switch]$AllowSignatureProbeUnavailable
)

$ErrorActionPreference = "Stop"
$prereqDir = Join-Path $Root "installer\prerequisites"
$manifestPath = Join-Path $prereqDir "vc_redist.x64.json"
if (-not (Test-Path -LiteralPath $manifestPath)) { throw "VC++ prerequisite manifest is missing: $manifestPath" }
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$fileName = [string]$manifest.file
$exePath = Join-Path $prereqDir $fileName
if (-not $fileName -or -not (Test-Path -LiteralPath $exePath)) { throw "Official VC++ x64 redistributable is missing: $exePath" }
if (-not $manifest.sha256 -or $manifest.sha256 -notmatch '^[A-Fa-f0-9]{64}$') { throw "VC++ prerequisite manifest must contain a fixed SHA-256." }
$sha = [System.Security.Cryptography.SHA256]::Create()
try { $hash = ([System.BitConverter]::ToString($sha.ComputeHash([System.IO.File]::ReadAllBytes($exePath))) -replace '-', '') }
finally { $sha.Dispose() }
if ($hash -ne $manifest.sha256.ToUpperInvariant()) { throw "VC++ redistributable SHA-256 does not match the pinned manifest." }
$signature = $null; $signatureStatus = "verified"; $publisher = "Microsoft Corporation"
try {
    $signature = Get-AuthenticodeSignature -FilePath $exePath
    if ($signature.Status -ne 'Valid' -or -not $signature.SignerCertificate -or $signature.SignerCertificate.Subject -notmatch 'Microsoft Corporation') {
        throw "VC++ redistributable is not a valid Microsoft-signed executable."
    }
    $publisher = $signature.SignerCertificate.Subject
} catch {
    if (-not $AllowSignatureProbeUnavailable) { throw }
    $signatureStatus = "verified_sha256_signature_probe_unavailable"
}
$version = (Get-Item -LiteralPath $exePath).VersionInfo.ProductVersion
if (-not $version) { throw "VC++ redistributable product version could not be read." }
if (-not $manifest.product_version -or $version -ne [string]$manifest.product_version) {
    throw "VC++ redistributable product version does not match the pinned manifest."
}
[ordered]@{
    file = $fileName
    sha256 = $hash
    product_version = $version
    publisher = $publisher
    status = $signatureStatus
} | ConvertTo-Json -Depth 3
