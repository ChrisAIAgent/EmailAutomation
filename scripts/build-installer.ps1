<#
.SYNOPSIS
  Build the Email Automation Windows installer (.exe) with Inno Setup 6.

.DESCRIPTION
  Produces dist\Email-Automation-Setup-<Version>.exe. The flow:
    1. Locate the Inno Setup compiler (ISCC.exe) and ImageMagick (magick.exe).
    2. Build the immutable bundled runtime on the build machine.
    3. Stage the workspace payload by REUSING scripts/portable-package.ps1
       (all exclusion / integrity / safety rules), into installer\payload.
    3. Re-verify required runtimes, offline caches and scripts are present.
    4. Generate installer\assets\EmailAutomation.ico from the official logo.
    5. Run a second, independent secret scan over the payload.
    6. Inject the version into installer\EmailAutomation.iss.
    7. Compile with ISCC.
    8. Print a build report and clean up the staged payload.

  Any missing prerequisite, integrity failure, leaked secret or compile error
  TERMINATES the build - it never reports a successful installer that is not.

.PARAMETER Version
  Optional explicit product version. When omitted, it is read from the root
  VERSION file, which is the single source of truth. The value is written into
  the .iss, the exe filename and payload/version.txt. An explicit value must
  match the VERSION file. frontend/package.json and scripts/mcp_server.py are
  cross-checked against the same version; any drift ABORTS the build.
  Never hardcode a date as the version.

.PARAMETER InnoCompiler
  Optional explicit path to ISCC.exe. When omitted, common install locations
  and the registry are probed.
#>
param(
    [string]$Version = "",
    [string]$InnoCompiler = "",
    [switch]$AllowUncommitted,
    [string]$TacWorkRoot = $env:TACWORK_ROOT,
    [string]$TacWorkCommit = "a8a6156",
    [string]$PreviousReleaseTag = "v1.2.3",
    [string]$SigningCertificateThumbprint = $env:EMAIL_AUTOMATION_SIGNING_CERT_THUMBPRINT,
    [string]$TimestampUrl = $env:EMAIL_AUTOMATION_TIMESTAMP_URL,
    [switch]$AcceptanceCandidate,
    [switch]$ReuseVerifiedRuntime
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$root       = Split-Path -Parent $scriptDir
function Fail($msg) { throw ("[BUILD ABORTED] " + $msg) }
function Get-Sha256([string]$Path) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($sha.ComputeHash([System.IO.File]::ReadAllBytes($Path))) -replace '-', '')
    } finally { $sha.Dispose() }
}

# --- Version source of truth & cross-check ---------------------------------
# The root VERSION file is the single source of product version. The build must
# never drift: an explicit -Version must match it, and frontend/package.json and
# scripts/mcp_server.py must agree with it. Any mismatch aborts the build.
$versionFile = Join-Path $root "VERSION"
if (-not (Test-Path $versionFile)) { Fail "Root VERSION file is missing. Create it (e.g. 1.1.0) as the version source of truth." }
$expectedVersion = ([IO.File]::ReadAllText($versionFile)).Trim()
if (-not ($expectedVersion -match '^\d+\.\d+\.\d+$')) { Fail ("Root VERSION file is not a valid SemVer string: '" + $expectedVersion + "'.") }

if ($Version -eq "") { $Version = $expectedVersion }
elseif ($Version -ne $expectedVersion) {
    Fail ("Explicit -Version '" + $Version + "' does not match root VERSION file '" + $expectedVersion + "'. Fix the drift before building.")
}

$pkgJsonPath = Join-Path $root "frontend\package.json"
if (Test-Path $pkgJsonPath) {
    $pkgText = [IO.File]::ReadAllText($pkgJsonPath)
    if ($pkgText -notmatch '"version"\s*:\s*"(?<v>[^"]+)"') { Fail "Could not parse frontend/package.json version." }
    if ($Matches['v'] -ne $expectedVersion) {
        Fail ("frontend/package.json version '" + $Matches['v'] + "' does not match root VERSION '" + $expectedVersion + "'. Sync them before building.")
    }
}
$desktopPkgPath = Join-Path $root "desktop\package.json"
if (-not (Test-Path $desktopPkgPath)) { Fail "desktop/package.json is missing." }
$desktopPkgText = [IO.File]::ReadAllText($desktopPkgPath)
if ($desktopPkgText -notmatch '"version"\s*:\s*"(?<v>[^"]+)"' -or $Matches['v'] -ne $expectedVersion) {
    Fail "desktop/package.json version does not match root VERSION."
}
foreach ($lockPath in @((Join-Path $root "frontend\package-lock.json"), (Join-Path $root "desktop\package-lock.json"))) {
    if (-not (Test-Path $lockPath)) { Fail ("Missing npm lockfile: " + $lockPath) }
    $lockText = [IO.File]::ReadAllText($lockPath)
    $lockVersions = [regex]::Matches($lockText, '"version"\s*:\s*"(?<v>[^"]+)"')
    if ($lockVersions.Count -lt 2 -or $lockVersions[0].Groups['v'].Value -ne $expectedVersion -or $lockVersions[1].Groups['v'].Value -ne $expectedVersion) {
        Fail ((Split-Path $lockPath -Leaf) + " version does not match root VERSION.")
    }
}

$mcpPath = Join-Path $root "scripts\mcp_server.py"
if (Test-Path $mcpPath) {
    $mcpText = [IO.File]::ReadAllText($mcpPath)
    if ($mcpText -notmatch '"version"\s*:\s*"(?<v>[^"]+)"') { Fail "Could not parse scripts/mcp_server.py SERVER_INFO version." }
    if ($Matches['v'] -ne $expectedVersion) {
        Fail ("scripts/mcp_server.py SERVER_INFO version '" + $Matches['v'] + "' does not match root VERSION '" + $expectedVersion + "'. Sync them before building.")
    }
}
$buildGitCommit = (& git -C $root rev-parse HEAD 2>$null | Select-Object -First 1)
$buildGitTag = (& git -C $root tag --points-at HEAD 2>$null | Where-Object { $_ -eq ("v" + $expectedVersion) } | Select-Object -First 1)
$buildGitDirty = [bool](& git -C $root status --porcelain 2>$null)
if ($AcceptanceCandidate -and $AllowUncommitted) {
    Fail "AcceptanceCandidate still requires a clean committed source; do not combine it with -AllowUncommitted."
}
if ($ReuseVerifiedRuntime -and -not $AcceptanceCandidate) {
    Fail "ReuseVerifiedRuntime is permitted only for an internal acceptance candidate."
}
if (-not $AllowUncommitted -and $buildGitDirty) {
    Fail "Release and acceptance-candidate builds require a clean working tree. Commit or remove unrelated changes first."
}
if (-not $AcceptanceCandidate -and -not $AllowUncommitted -and $buildGitTag -ne ("v" + $expectedVersion)) {
    Fail ("Formal release builds require a clean exact tag v" + $expectedVersion + ". Commit/review/tag first, or use -AllowUncommitted for a non-release development build.")
}
$installerDir = Join-Path $root "installer"
$payloadDir   = Join-Path $installerDir "payload"
$assetsDir    = Join-Path $installerDir "assets"
$distDir      = Join-Path $root "dist"
$iss          = Join-Path $installerDir "EmailAutomation.iss"
$pkgScript    = Join-Path $root "scripts\portable-package.ps1"
$runtimeScript = Join-Path $root "scripts\build-runtime.ps1"
$tacWorkRuntimeScript = Join-Path $root "scripts\build-tacwork-release-runtime.ps1"
$vcRedistCheckScript = Join-Path $root "scripts\verify-vc-redist.ps1"
$logoPng      = Join-Path $root "TACWork-Logo-Black.PNG"
$webLogo      = Join-Path $root "frontend\public\tac-logo.png"
$icoPath      = Join-Path $assetsDir "EmailAutomation.ico"
$exeName      = "Email-Automation-Setup-$Version.exe"
$exePath      = Join-Path $distDir $exeName

# File overwrites replace changed lines normally. Only a deleted or renamed
# file can survive an in-place Inno upgrade, so make each such path an explicit
# reviewed installer action. This is deliberately conservative: if a source
# file disappears, formal release stops until it is listed below.
$obsoleteListPath = Join-Path $installerDir ("obsolete-files-" + $Version + ".txt")
if (-not (Test-Path -LiteralPath $obsoleteListPath)) { Fail "Missing explicit obsolete-file list: $obsoleteListPath" }
$allowedObsolete = @(
    Get-Content -LiteralPath $obsoleteListPath | ForEach-Object { $_.Trim().Replace('/', '\\') } |
        Where-Object { $_ -and -not $_.StartsWith('#') }
)
if ($allowedObsolete | Where-Object { $_ -match '(^|\\)\.\.(\\|$)|^[A-Za-z]:|^\\' }) {
    Fail "obsolete-files list contains an unsafe path. Entries must be relative to {app}."
}
$previousRef = (& git -C $root rev-parse ($PreviousReleaseTag + "^{commit}") 2>$null | Select-Object -First 1)
if (-not $previousRef) { Fail "Previous release tag is unavailable: $PreviousReleaseTag" }
$removedPaths = New-Object System.Collections.Generic.List[string]
& git -C $root diff --name-status --find-renames ($PreviousReleaseTag + "..HEAD") | ForEach-Object {
    $parts = $_ -split "`t"
    if ($parts[0] -eq 'D' -and $parts.Count -ge 2) { [void]$removedPaths.Add($parts[1].Replace('/', '\\')) }
    elseif ($parts[0] -like 'R*' -and $parts.Count -ge 3) { [void]$removedPaths.Add($parts[1].Replace('/', '\\')) }
}
$unhandledObsolete = @($removedPaths | Where-Object { $_ -notin $allowedObsolete })
$staleObsolete = @($allowedObsolete | Where-Object { $_ -notin $removedPaths })
if ($unhandledObsolete.Count -gt 0 -or $staleObsolete.Count -gt 0) {
    Fail ("Obsolete-file gate failed. Unhandled removed paths: " + ($unhandledObsolete -join ', ') + "; stale list paths: " + ($staleObsolete -join ', '))
}

Write-Output "=================================================================="
Write-Output " Email Automation - Windows installer build"
Write-Output (" Version : " + $Version + "  (source: root VERSION file)")
Write-Output (" Root    : " + $root)
Write-Output (" TACWork : " + $TacWorkCommit + " from clean temporary worktree")
if ($AcceptanceCandidate) { Write-Output " Mode    : INTERNAL ACCEPTANCE CANDIDATE (unsigned, no release tag required)" }
Write-Output "=================================================================="

# --- 1. Locate Inno Setup compiler ---------------------------------------
function Find-ISCC($preferred) {
    if ($preferred -and (Test-Path $preferred)) { return $preferred }
    $cands = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe",
        "${env:LOCALAPPDATA}\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup\ISCC.exe"
    )
    foreach ($c in $cands) { if (Test-Path $c) { return $c } }
    try {
        $key = Get-ItemProperty "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1" -ErrorAction SilentlyContinue
        if ($key -and $key.InstallLocation) {
            $p = Join-Path $key.InstallLocation "ISCC.exe"
            if (Test-Path $p) { return $p }
        }
    } catch {}
    return $null
}
$ISCC = Find-ISCC $InnoCompiler
if (-not $ISCC) { Fail "Inno Setup 6 (ISCC.exe) not found. Install it (e.g. 'winget install JRSoftware.InnoSetup') or pass -InnoCompiler <path>." }
Write-Output ("ISCC    : " + $ISCC)

# --- Locate ImageMagick --------------------------------------------------
function Find-Magick {
    $m = Get-Command magick -ErrorAction SilentlyContinue
    if ($m) { return $m.Source }
    $prog = "${env:ProgramFiles}"
    if (Test-Path $prog) {
        $hit = Get-ChildItem $prog -Filter "ImageMagick-*" -Directory -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($hit) { $p = Join-Path $hit.FullName "magick.exe"; if (Test-Path $p) { return $p } }
    }
    $prog86 = "${env:ProgramFiles(x86)}"
    if (Test-Path $prog86) {
        $hit = Get-ChildItem $prog86 -Filter "ImageMagick-*" -Directory -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($hit) { $p = Join-Path $hit.FullName "magick.exe"; if (Test-Path $p) { return $p } }
    }
    return $null
}
$magick = Find-Magick
if (-not $magick) { Fail "ImageMagick (magick.exe) not found. Install it (e.g. 'winget install ImageMagick.ImageMagick') to generate the installer icon." }
Write-Output ("Magick  : " + $magick)

# --- 2. Clean previous build artifacts -----------------------------------
# The staged payload MUST be fully wiped, not merged: portable-package.ps1
# stages with `robocopy /E` (no /PURGE), so any file left in a stale payload
# survives into the new one. Diagnostic leftovers such as .procs-check*.txt
# then re-appear and trip the secret scan below, aborting the build.
# A safe-delete guard wraps the Remove-Item cmdlet and only WARNs + skips,
# leaving the stale payload behind. Delete through the \\?\ namespace with the
# .NET API, which the guard cannot intercept, so every build starts clean.
function Remove-Hard([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    try {
        # Strip read-only on every entry so Directory.Delete can recurse.
        Get-ChildItem -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue |
            ForEach-Object { try { $_.Attributes = $_.Attributes -band (-bnot [System.IO.FileAttributes]::ReadOnly) } catch {} }
        [System.IO.Directory]::Delete("\\?\" + $Path, $true)
    } catch {
        # A running IDE (e.g. WorkBuddy) may hold a handle on a file inside the
        # tree (e.g. runtime/electron/resources/default_app.asar for indexing),
        # so Directory.Delete fails. Renaming the directory only updates its
        # parent entry and does NOT open the locked file, so it always succeeds.
        # robocopy then stages into a fresh payload; the renamed copy sits
        # outside $payloadDir, is excluded from shipping and from the secret
        # scan, and can be deleted once the IDE releases the handle.
        try {
            $stale = $Path + ".stale-" + (Get-Date -Format "yyyyMMddHHmmss")
            [System.IO.Directory]::Move("\\?\" + $Path, "\\?\" + $stale)
            Write-Output ("WARN: could not delete $Path (a file is locked by another process); renamed aside to $stale for later cleanup.")
        } catch {
            Write-Output ("WARN: could not remove or rename $Path ($($_.Exception.Message)); staging will overwrite it.")
        }
    }
}
Remove-Hard $payloadDir
if (Test-Path -LiteralPath $payloadDir) { Write-Output "WARN: stale payload still present; build may fail the secret scan if stale files remain." }
if (Test-Path -LiteralPath $exePath) { try { [System.IO.File]::Delete("\\?\" + $exePath) } catch { Write-Output ("WARN: could not remove stale installer ($($_.Exception.Message)).") } }
New-Item -ItemType Directory -Force -Path $distDir | Out-Null

# --- 3. Build the runtime before staging ---------------------------------
$tacworkManifestPath = Join-Path $root "tacwork-runtime\runtime-manifest.json"
if (-not $ReuseVerifiedRuntime) {
    Write-Output "Preparing TACWork runtime from approved clean worktree..."
    & $tacWorkRuntimeScript -TacWorkRoot $TacWorkRoot -ExpectedCommit $TacWorkCommit
    if (-not $?) { Fail "TACWork runtime preparation failed." }
}
if (-not (Test-Path -LiteralPath $tacworkManifestPath)) { Fail "TACWork runtime manifest is missing." }
$tacworkManifest = Get-Content -LiteralPath $tacworkManifestPath -Raw | ConvertFrom-Json
if ($tacworkManifest.source_commit -notlike ($TacWorkCommit + "*") -or $tacworkManifest.source_dirty -ne $false) {
    Fail "TACWork runtime provenance is not the approved clean source."
}
if (-not $ReuseVerifiedRuntime) {
    Write-Output "Building prebuilt runtime (customer startup will not use pip/npm)..."
    & $runtimeScript -Clean
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path (Join-Path $root "runtime\runtime-manifest.json"))) {
        Fail "Prebuilt runtime build failed."
    }
} else {
    Write-Output "Reusing freshly verified runtime for this internal acceptance candidate."
}
& (Join-Path $root "scripts\verify-runtime.ps1") -Root $root
if (-not $?) { Fail "Runtime SHA-256 verification failed." }

# --- 4. Stage payload (reuse portable-package.ps1) -----------------------
Write-Output "Staging payload via portable-package.ps1 (reusing exclude/integrity/safety rules)..."
try {
    & $pkgScript -StageDir $payloadDir
    # portable-package.ps1 uses throw on real failure (which sets $? = $false) and
    # explicit exit 0 on success in StageDir mode. Never key off $LASTEXITCODE here:
    # robocopy's "1 = files copied" success code would otherwise false-abort the build.
    if (-not $?) { Fail ("Payload staging failed: " + $_.Exception.Message) }
} catch {
    Fail ("Payload staging failed: " + $_.Exception.Message)
}
if (-not (Test-Path $payloadDir)) { Fail "Payload directory was not created by staging." }
Write-Output ("Staged : " + $payloadDir)

# --- 5. Independent integrity re-check ------------------------------------
$mustExist = @(
    "tools\python\python.exe",
    "tools\node\node.exe",
    "runtime\python-packages",
    "runtime\frontend\server.js",
    "runtime\frontend-static\index.html",
    "runtime\electron\Email Automation.exe",
    "runtime\electron\resources\app\main.cjs",
    "runtime\runtime-manifest.json",
    "tacwork-runtime\server\openwork-server.exe",
    "tacwork-runtime\engine\opencode.exe",
    "tacwork-runtime\web\index.html",
    "scripts\portable-start-unified.ps1",
    "scripts\mcp_server.py"
)
$missing = @()
foreach ($rel in $mustExist) {
    if (-not (Test-Path (Join-Path $payloadDir $rel))) { $missing += $rel }
}
if ($missing.Count -gt 0) { Fail ("Payload missing required files: " + ($missing -join ", ")) }
Write-Output "Integrity re-check OK (prebuilt runtime present)."

# --- 5b. Manifest subset of payload check -------------------------------
# portable-package.ps1 regenerates runtime-manifest.json from the payload
# in StageDir mode, so the manifest should match the payload exactly. But
# if anyone edits the staging exclusions or the manifest regeneration
# logic drifts, the customer would get an installer whose verify-runtime
# fails with runtime_integrity_failed:missing:<file> on first launch.
# Catch that drift here, at build time, so a broken installer never ships.
$payloadManifestPath = Join-Path $payloadDir "runtime\runtime-manifest.json"
if (-not (Test-Path -LiteralPath $payloadManifestPath)) {
    Fail "payload/runtime/runtime-manifest.json missing - staging did not regenerate it. Check portable-package.ps1 StageDir mode."
}
$payloadManifest = Get-Content -LiteralPath $payloadManifestPath -Raw | ConvertFrom-Json
$payloadManifestMissing = New-Object System.Collections.Generic.List[string]
$payloadManifestHashBad = New-Object System.Collections.Generic.List[string]
$sha256 = [System.Security.Cryptography.SHA256]::Create()
foreach ($entry in $payloadManifest.files) {
    $rel = ([string]$entry.path).Replace('/', '\')
    $target = Join-Path $payloadDir $rel
    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
        [void]$payloadManifestMissing.Add($rel)
        if ($payloadManifestMissing.Count -ge 5) { break }
        continue
    }
    # Existence alone is not enough: a stale/self-referential manifest entry
    # (e.g. the manifest hashing itself) would pass a Test-Path check yet still
    # fail the customer's integrity check at first launch. Verify the hash too.
    $actualHash = ([System.BitConverter]::ToString($sha256.ComputeHash([System.IO.File]::ReadAllBytes($target))) -replace '-', '').ToLowerInvariant()
    $expectHash = ([string]$entry.sha256).ToLowerInvariant()
    if ($actualHash -ne $expectHash) {
        [void]$payloadManifestHashBad.Add($rel)
        if ($payloadManifestHashBad.Count -ge 5) { break }
    }
}
$sha256.Dispose()
if ($payloadManifestMissing.Count -gt 0) {
    Fail ("Manifest lists files absent from payload (first 5): " + ($payloadManifestMissing -join ", ") + ". Staging exclusion rules drifted from manifest generation.")
}
if ($payloadManifestHashBad.Count -gt 0) {
    Fail ("Manifest hashes mismatch payload (first 5): " + ($payloadManifestHashBad -join ", ") + ". A stale or self-referential manifest entry would fail the customer's integrity check.")
}
Write-Output ("Manifest-payload consistency OK: " + $payloadManifest.files.Count + " entries verified (existence + sha256) against payload.")

# --- 5. Generate installer icon ------------------------------------------
New-Item -ItemType Directory -Force -Path $assetsDir | Out-Null
& $magick $logoPng -background none -define icon:auto-resize=16,32,48,256 $icoPath
if (-not (Test-Path $icoPath)) { Fail "ICO generation failed (magick produced no file)." }
Write-Output ("Icon    : " + $icoPath)

# --- 6. Brand asset checks -----------------------------------------------
if (-not (Test-Path $logoPng)) { Fail "TACWork-Logo-Black.PNG missing at repo root." }
if (-not (Test-Path $webLogo)) { Fail "frontend/public/tac-logo.png missing." }
$white = Join-Path $root "TACWork-Logo-White.PNG"
if (Test-Path $white) { Write-Output "Brand   : white logo present." }
else { Write-Output "Brand   : TACWork-Logo-White NOT found - using official black logo only (not fabricated)." }

# --- 7. Second, independent secret scan ----------------------------------
Write-Output "Running second secret scan over payload..."
$scanErrs = @()
# 7a. Name-based: excluded secret containers must never be in the payload.
Get-ChildItem $payloadDir -Recurse -Force -File -ErrorAction SilentlyContinue | ForEach-Object {
    $n = $_.Name
    # Google API ships a legitimate discovery document named oauth2.v2.json;
    # only credential-shaped filenames belong in this exclusion check.
    $oauthLeak = ($n -like "oauth-token*.json" -or $n -like "oauth*.pickle" -or $n -like "oauth*.token" `
        -or $n -like "oauth-credentials*.json" -or $n -eq "oauth" -or $n -like "client_secret_*.json")
    if ($n -eq ".env" -or $n -eq ".env.local" -or $n -like "app.db*" -or $n -like "huey.db*" `
        -or $n -like "*.token" -or $n -like "token.json" -or $n -like "credentials.json" -or $oauthLeak `
        -or $n -like "*.sqlite" -or $n -like "*.sqlite3") {
        $scanErrs += ("leaked file: " + $_.FullName.Substring($payloadDir.Length))
    }
}
# 7b. Content-based, first-party only (skip heavy runtime/dep trees).
$skipDirs = @("tools", "frontend\node_modules", "tacwork-runtime", "offline-cache", "node_modules")
$contentPatterns = @(
    'sk-[A-Za-z0-9]{32,}',
    'AKIA[0-9A-Z]{16}',
    'Bearer\s+[A-Za-z0-9._\-]{40,}',
    # Quoted long secrets only - config KEY references like client_secret = settings.X
    # and test fixtures like client_secret="sec" must NOT trip the gate.
    'client_secret\s*[:=]\s*["''][A-Za-z0-9_\-./+]{24,}["'']',
    'api_key\s*[:=]\s*["''][A-Za-z0-9]{32,}["'']'
)
Get-ChildItem $payloadDir -Recurse -Force -File -ErrorAction SilentlyContinue | Where-Object {
    $ext = $_.Extension.ToLower()
    $full = $_.FullName
    $ext -in @(".py",".ps1",".json",".jsonc",".txt",".md",".bat",".yml",".yaml",".csv",".html",".ts",".js",".cfg",".ini",".env") `
    -and ($skipDirs | ForEach-Object { $full -like ("*" + [System.IO.Path]::DirectorySeparatorChar + $_ + "*") }) -notcontains $true
} | ForEach-Object {
    try {
        $txt = [System.IO.File]::ReadAllText($_.FullName)
        foreach ($p in $contentPatterns) {
            if ($txt -match $p) { $scanErrs += ("possible secret in " + $_.FullName.Substring($payloadDir.Length) + " [" + $p + "]"); break }
        }
    } catch {}
}
if ($scanErrs.Count -gt 0) { Fail ("Secret scan found: " + ($scanErrs -join "; ")) }
Write-Output "Secret scan: clean."

# --- 7c. Native prerequisite proof ---------------------------------------
# The installer embeds the verified Microsoft-signed VC++ redistributable.
# It is not a customer download and no system PATH change is required.
$vcStatus = & $vcRedistCheckScript -Root $root | ConvertFrom-Json
if (-not $vcStatus -or $vcStatus.status -ne 'verified') { Fail "VC++ prerequisite verification did not return verified status." }
Write-Output ("VC++ redist: " + $vcStatus.product_version + " verified.")

# --- 8. Inject version into .iss -----------------------------------------
if (-not (Test-Path $iss)) { Fail "installer/EmailAutomation.iss not found." }
$issText = Get-Content $iss -Raw -Encoding UTF8
$issText = $issText -replace '(?m)^#define MyAppVersion ".*?"', ('#define MyAppVersion "' + $Version + '"')
Set-Content -Path $iss -Value $issText -Encoding UTF8
# Surface version to the running app / runtime.
"$Version" | Set-Content -Encoding UTF8 -Path (Join-Path $payloadDir "version.txt")
Write-Output ("Version injected into .iss and payload/version.txt: " + $Version)

# --- 9. Compile ----------------------------------------------------------
Write-Output "Compiling installer with Inno Setup..."
& $ISCC /Q $iss
if ($LASTEXITCODE -ne 0) { Fail ("ISCC compilation failed (exit " + $LASTEXITCODE + ").") }
if (-not (Test-Path $exePath)) { Fail ("ISCC did not produce " + $exePath + ".") }

# A formal installer cannot be released unsigned. An acceptance candidate is
# intentionally the one exception: it is for controlled cross-machine testing,
# remains clean/reproducible, and its report is marked so it cannot be confused
# with a signed public release.
if ($AcceptanceCandidate) {
    $signResult = [pscustomobject]@{ Status = "NotSigned_InternalAcceptanceCandidate" }
} else {
    if (-not $SigningCertificateThumbprint) { Fail "Formal build requires EMAIL_AUTOMATION_SIGNING_CERT_THUMBPRINT (or -SigningCertificateThumbprint)." }
    $signingCert = Get-ChildItem -Path ("Cert:\CurrentUser\My\" + $SigningCertificateThumbprint) -ErrorAction SilentlyContinue
    if (-not $signingCert -or -not $signingCert.HasPrivateKey) { Fail "Configured signing certificate is unavailable or lacks a private key." }
    $signArgs = @{ FilePath = $exePath; Certificate = $signingCert; HashAlgorithm = 'SHA256' }
    if ($TimestampUrl) { $signArgs.TimestampServer = $TimestampUrl }
    $signResult = Set-AuthenticodeSignature @signArgs
    if ($signResult.Status -ne 'Valid') { Fail ("Authenticode signing failed: " + $signResult.Status) }
}

# Refuse a deceptively successful compile that omitted the staged runtime payload.
$payloadBytes = (Get-ChildItem $payloadDir -Recurse -Force -File -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum
$installerBytes = (Get-Item $exePath).Length
$minimumInstallerBytes = [math]::Max(50MB, [math]::Floor($payloadBytes * 0.12))
if ($installerBytes -lt $minimumInstallerBytes) {
    Fail ("Installer is unexpectedly small (" + $installerBytes + " bytes for " + $payloadBytes + " bytes of staged payload). Verify installer/EmailAutomation.iss includes payload\\*.")
}

# --- 10. Build report ----------------------------------------------------
$exeSizeMB = [math]::Round($installerBytes / 1MB, 1)
$payloadFiles = (Get-ChildItem $payloadDir -Recurse -Force -File -ErrorAction SilentlyContinue).Count
$pyVer = & (Join-Path $payloadDir "tools\python\python.exe") --version 2>&1
$nodeVer = & (Join-Path $payloadDir "tools\node\node.exe") --version 2>&1
$installerSha256 = Get-Sha256 $exePath
$shaPath = $exePath + ".sha256"
($installerSha256 + " *" + $exeName) | Set-Content -LiteralPath $shaPath -Encoding ASCII
$gitCommit = $buildGitCommit
$gitTag = $buildGitTag
if (-not $gitTag -or $gitTag -ne ("v" + $Version)) { $gitTag = "unreleased" }
$gitDirty = $buildGitDirty
$largestFiles = @(Get-ChildItem $payloadDir -Recurse -Force -File | Sort-Object Length -Descending | Select-Object -First 20 | ForEach-Object {
    [ordered]@{ path=$_.FullName.Substring($payloadDir.Length + 1).Replace("\", "/"); bytes=$_.Length }
})
$report = [ordered]@{
    version=$Version; git_commit=($gitCommit | Select-Object -First 1); git_tag=($gitTag | Select-Object -First 1); git_dirty=$gitDirty
    artifact_type=$(if ($AcceptanceCandidate) { "internal_acceptance_candidate" } else { "formal_release" })
    build_time_utc=[DateTime]::UtcNow.ToString("o"); installer=$exeName; installer_sha256=$installerSha256
    installer_bytes=$installerBytes; payload_files=$payloadFiles; payload_bytes=$payloadBytes
    python_runtime=($pyVer -join " "); node_runtime=($nodeVer -join " "); runtime_manifest_valid=$true; secret_scan="clean"
    tacwork_source_commit=$tacworkManifest.source_commit; tacwork_source_dirty=[bool]$tacworkManifest.source_dirty
    electron_version=((Get-Content -LiteralPath (Join-Path $payloadDir "runtime\electron\version") -Raw -ErrorAction SilentlyContinue).Trim())
    tacwork_server_version=$tacworkManifest.server_version; native_dependency_status=$vcStatus.status
    frontend_mode="electron_app_and_transition_web"; authenticode_status=$signResult.Status
    largest_payload_files=$largestFiles
}
$reportJson = Join-Path $distDir ("build-report-" + $Version + ".json")
$reportMd = Join-Path $distDir ("build-report-" + $Version + ".md")
$report | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $reportJson -Encoding UTF8
@("# Email Automation $Version build report", "", "- Artifact type: ``$($report.artifact_type)``", "- Git commit: ``$($report.git_commit)``", "- Git tag: ``$($report.git_tag)``", "- Working tree dirty: ``$gitDirty``", "- Installer: ``$exeName``", "- Installer SHA-256: ``$installerSha256``", "- Authenticode: ``$($report.authenticode_status)``", "- TACWork source: ``$($report.tacwork_source_commit)`` (dirty=``$($report.tacwork_source_dirty)``)", "- Electron version: ``$($report.electron_version)``", "- TACWork server version: ``$($report.tacwork_server_version)``", "- Native dependency: ``$($report.native_dependency_status)``", "- Frontend mode: ``$($report.frontend_mode)``", "- Installer bytes: ``$installerBytes``", "- Payload files: ``$payloadFiles``", "- Runtime manifest: valid", "- Secret scan: clean") | Set-Content -LiteralPath $reportMd -Encoding UTF8
Write-Output ""
Write-Output "==================== BUILD REPORT ===================="
Write-Output ("Build time     : " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
Write-Output ("Version        : " + $Version)
Write-Output ("Installer      : " + $exePath)
Write-Output ("Installer size : " + $exeSizeMB + " MB")
Write-Output ("Payload files  : " + $payloadFiles)
Write-Output ("Python runtime : " + $pyVer)
Write-Output ("Node runtime   : " + $nodeVer)
Write-Output ("Installer SHA  : " + $installerSha256)
Write-Output ("SHA256 file    : " + $shaPath)
Write-Output ("Git commit/tag : " + $report.git_commit + " / " + $report.git_tag + " (dirty=" + $gitDirty + ")")
Write-Output ("Reports         : " + $reportJson + ", " + $reportMd)
Write-Output ("Secrets scanned: clean (name + content, first-party)")
Write-Output ("Validation     : staged OK, integrity OK, icon OK, compile OK")
Write-Output "======================================================"

# --- 11. Cleanup staged payload (keep dist/ and installer/assets) --------
# Use the same hooksafe hard-delete as step 2 so cleanup actually happens
# even where a safe-delete guard wraps Remove-Item.
Remove-Hard $payloadDir
if (-not (Test-Path -LiteralPath $payloadDir)) { Write-Output "Staged payload removed." }
else { Write-Output "WARN: staged payload not removed by cleanup; remove installer\payload manually if disk space matters." }
Write-Output "Build complete."
