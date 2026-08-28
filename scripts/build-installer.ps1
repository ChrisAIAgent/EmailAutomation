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
    [string]$InnoCompiler = ""
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$root       = Split-Path -Parent $scriptDir

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

$mcpPath = Join-Path $root "scripts\mcp_server.py"
if (Test-Path $mcpPath) {
    $mcpText = [IO.File]::ReadAllText($mcpPath)
    if ($mcpText -notmatch '"version"\s*:\s*"(?<v>[^"]+)"') { Fail "Could not parse scripts/mcp_server.py SERVER_INFO version." }
    if ($Matches['v'] -ne $expectedVersion) {
        Fail ("scripts/mcp_server.py SERVER_INFO version '" + $Matches['v'] + "' does not match root VERSION '" + $expectedVersion + "'. Sync them before building.")
    }
}
$installerDir = Join-Path $root "installer"
$payloadDir   = Join-Path $installerDir "payload"
$assetsDir    = Join-Path $installerDir "assets"
$distDir      = Join-Path $root "dist"
$iss          = Join-Path $installerDir "EmailAutomation.iss"
$pkgScript    = Join-Path $root "scripts\portable-package.ps1"
$runtimeScript = Join-Path $root "scripts\build-runtime.ps1"
$logoPng      = Join-Path $root "TACWork-Logo-Black.PNG"
$webLogo      = Join-Path $root "frontend\public\tac-logo.png"
$icoPath      = Join-Path $assetsDir "EmailAutomation.ico"
$exeName      = "Email-Automation-Setup-$Version.exe"
$exePath      = Join-Path $distDir $exeName

function Fail($msg) { throw ("[BUILD ABORTED] " + $msg) }

Write-Output "=================================================================="
Write-Output " Email Automation - Windows installer build"
Write-Output (" Version : " + $Version + "  (source: root VERSION file)")
Write-Output (" Root    : " + $root)
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
# Cleanup is best-effort: some sandboxed environments install a safe-delete
# guard that blocks Remove-Item on large trees. A blocked cleanup must NOT fail
# an otherwise-good build, so swallow the error and warn instead.
if (Test-Path $payloadDir) { try { Remove-Item -Recurse -Force $payloadDir } catch { Write-Output ("WARN: could not remove stale payload ($($_.Exception.Message)); staging will overwrite it.") } }
if (Test-Path $exePath)    { try { Remove-Item -Force $exePath } catch { Write-Output ("WARN: could not remove stale installer ($($_.Exception.Message)).") } }
New-Item -ItemType Directory -Force -Path $distDir | Out-Null

# --- 3. Build the runtime before staging ---------------------------------
Write-Output "Building prebuilt runtime (customer startup will not use pip/npm)..."
& $runtimeScript
if ($LASTEXITCODE -ne 0 -or -not (Test-Path (Join-Path $root "runtime\runtime-manifest.json"))) {
    Fail "Prebuilt runtime build failed."
}

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
& $ISCC $iss
if ($LASTEXITCODE -ne 0) { Fail ("ISCC compilation failed (exit " + $LASTEXITCODE + ").") }
if (-not (Test-Path $exePath)) { Fail ("ISCC did not produce " + $exePath + ".") }

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
Write-Output ""
Write-Output "==================== BUILD REPORT ===================="
Write-Output ("Build time     : " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
Write-Output ("Version        : " + $Version)
Write-Output ("Installer      : " + $exePath)
Write-Output ("Installer size : " + $exeSizeMB + " MB")
Write-Output ("Payload files  : " + $payloadFiles)
Write-Output ("Python runtime : " + $pyVer)
Write-Output ("Node runtime   : " + $nodeVer)
Write-Output ("Secrets scanned: clean (name + content, first-party)")
Write-Output ("Validation     : staged OK, integrity OK, icon OK, compile OK")
Write-Output "======================================================"

# --- 11. Cleanup staged payload (keep dist/ and installer/assets) --------
# Best-effort: see the step-2 note about safe-delete guards in sandboxes.
try { Remove-Item -Recurse -Force $payloadDir; Write-Output "Staged payload removed." }
catch { Write-Output ("WARN: staged payload not removed by cleanup ($($_.Exception.Message)); remove installer\payload manually if disk space matters.") }
Write-Output "Build complete."
