<#
.SYNOPSIS
  Portable workspace bootstrap.

  Creates a machine-local Python virtual environment and regenerates the
  frontend dependency tree using the Workspace-bundled (or, as a fallback,
  system) runtimes. No business data, database, .env, OAuth tokens, or source
  code is ever touched.

.DESCRIPTION
  * Resolves the workspace root from this script's own location (scripts/), so
    it works from any unzip directory. No drive letter or user name is hardcoded.
  * Prefers tools/python and tools/node. Falls back to system python/node only
    when the bundled runtimes are absent, and reports clearly which is used.
  * Deletes ONLY renewable directories:
        backend/.venv
        frontend/node_modules
        frontend/.next  frontend/.next-dev  frontend/.next-prod
        backend/.pytest_cache
    Every path is printed and verified to live inside the workspace root before
    deletion (Assert-InRoot).
  * Installs Python deps from offline-cache/python-wheels (offline). If that
    cache is missing or empty, it reports clearly and STOPS - it never fakes a
    successful install.
  * Regenerates frontend/node_modules from package-lock.json using the offline
    npm cache (npm ci --offline). If the cache is incomplete it reports clearly
    and stops.
  * Runs a minimal backend import check and the frontend typecheck.
  * Safe to re-run: if the workspace is already provisioned on this machine and
    -Force is not given, the heavy install is skipped and only re-verified.
#>
param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"

# --- Normalize duplicate PATH/Path casing so Start-Process never chokes ---
$procPath = [Environment]::GetEnvironmentVariable("Path", "Process")
[Environment]::SetEnvironmentVariable("PATH", $null, "Process")
[Environment]::SetEnvironmentVariable("Path", $procPath, "Process")

# --- Resolve workspace root from this script's directory (scripts/) ---
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$root = Split-Path -Parent $scriptDir

Write-Output "Portable workspace root: $root"

# Sanity: confirm this is the real workspace (not some unrelated folder)
if (-not (Test-Path (Join-Path $root "backend\app\main.py"))) {
    throw "backend/app/main.py not found under $root. This does not look like the Email Automation workspace."
}
if (-not (Test-Path (Join-Path $root "frontend\package.json"))) {
    throw "frontend/package.json not found under $root. This does not look like the Email Automation workspace."
}
if (-not (Test-Path (Join-Path $root "tacwork-runtime\server\openwork-server.exe")) -or
    -not (Test-Path (Join-Path $root "tacwork-runtime\engine\opencode.exe")) -or
    -not (Test-Path (Join-Path $root "tacwork-runtime\web\index.html"))) {
    throw "Bundled tacwork-runtime is incomplete. Rebuild the portable archive on the source machine."
}

# Resolve the data directory with the shared resolver (runbook Section 4) and
# seed a default .env in it if the package (which ships without secrets) did not
# carry one. config.py loads .env from the same resolved config/ location.
. (Join-Path $PSScriptRoot "data-dir.ps1")
$envExample = Join-Path $root "backend\.env.example"
$envTarget = Join-Path $global:DataConfig ".env"
if ((Test-Path $envExample) -and -not (Test-Path $envTarget)) {
    Copy-Item -Force $envExample $envTarget
    Write-Output ("Created default .env at $envTarget (edit it to configure Gmail OAuth and AI keys).")
}

# --- Resolve runtimes (prefer bundled) ---
function Resolve-Python {
    $bundled = Join-Path $root "tools\python\python.exe"
    if (Test-Path $bundled) { return $bundled }
    $sys = Get-Command python -ErrorAction SilentlyContinue
    if ($sys) { return $sys.Source }
    $sys = Get-Command python3 -ErrorAction SilentlyContinue
    if ($sys) { return $sys.Source }
    return $null
}
function Resolve-Node {
    $bundled = Join-Path $root "tools\node\node.exe"
    if (Test-Path $bundled) { return $bundled }
    $sys = Get-Command node -ErrorAction SilentlyContinue
    if ($sys) { return $sys.Source }
    return $null
}
function Resolve-Npm {
    $bundled = Join-Path $root "tools\node\npm.cmd"
    if (Test-Path $bundled) { return $bundled }
    $sys = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if ($sys) { return $sys.Source }
    return $null
}

$pyBin = Resolve-Python
$nodeBin = Resolve-Node
$npmBin = Resolve-Npm

if (-not $pyBin) { throw "Python runtime not found. Place a portable Python in tools/python/ or install Python 3.13+ on the system PATH." }
if (-not $nodeBin) { throw "Node runtime not found. Place a portable Node in tools/node/ or install Node 22+ on the system PATH." }
if (-not $npmBin) { throw "npm not found. Place a portable Node (with npm) in tools/node/ or install Node 22+ on the system PATH." }

Write-Output ("Python : " + (& $pyBin --version 2>&1))
Write-Output ("Node   : " + (& $nodeBin --version 2>&1))
Write-Output ("npm    : " + $npmBin)

# Prepend bundled node to PATH so npm.cmd uses the bundled node
$env:PATH = (Split-Path -Parent $npmBin) + ";" + $env:PATH
# Auto-confirm npm's bulk safe-delete prompt (npm 11) in non-interactive runs
$env:npm_config_yes = "true"

# --- Renewable directories to delete (verify inside root) ---
$renewable = @(
    (Join-Path $root "backend\.venv"),
    (Join-Path $root "frontend\node_modules"),
    (Join-Path $root "frontend\.next"),
    (Join-Path $root "frontend\.next-dev"),
    (Join-Path $root "frontend\.next-prod"),
    (Join-Path $root "backend\.pytest_cache")
)

function Assert-InRoot($p) {
    $full = Resolve-Path $p
    if (-not $full.Path.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to delete $($full.Path): it is outside the workspace root $root."
    }
}

$venvPy = Join-Path $root "backend\.venv\Scripts\python.exe"
$nodeReady = Test-Path (Join-Path $root "frontend\node_modules\.bin\next.cmd")

# Import check must cover the compiled (VC++-sensitive) packages so a missing
# native wheel fails fast here instead of during backend startup (runbook Problem 4).
$importCheck = "import fastapi, sqlalchemy, huey, uvicorn, cryptography, pydantic_core, psycopg2"

# Logs for build steps. Native commands (npm/pip) write warnings to stderr;
# under $ErrorActionPreference=Stop PowerShell would turn those into a fatal
# NativeCommandError. To stay robust on any machine (some have a global .npmrc
# that triggers harmless "shell-emulator" warnings), we redirect each step's
# stdout+stderr to a log file and inspect it ONLY on failure.
$stepLogDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $stepLogDir | Out-Null

    function Run-Step($label, $exe, $argArray, $logName) {
        $logPath = Join-Path $stepLogDir $logName
        Write-Output $label
        # Native commands (npm/pip) may write harmless warnings to stderr. Under
        # $ErrorActionPreference=Stop PowerShell turns that into a fatal
        # NativeCommandError. We temporarily relax to "Continue" so warnings do
        # not abort the script, then judge success purely by $LASTEXITCODE.
        $prevEAP = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        $output = & $exe @argArray 2>&1
        $code = $LASTEXITCODE
        $ErrorActionPreference = $prevEAP
        try { $output | Out-File -Encoding UTF8 -FilePath $logPath } catch {}
        if ($code -ne 0) {
            Write-Output ("--- $logName (FAILED, exit $code) ---")
            $output | ForEach-Object { Write-Output $_ }
            throw ("$label failed (exit $code). See $logPath.")
        }
        return
    }

$alreadyProvisioned = $false
if ((-not $Force) -and (Test-Path $venvPy) -and $nodeReady) {
    $prevEAP = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    $null = & $venvPy -c $importCheck 2>&1
    $alreadyProvisioned = $?
    $ErrorActionPreference = $prevEAP
}

if ($alreadyProvisioned) {
    Write-Output "Workspace already provisioned on this machine; skipping dependency reinstall (use -Force to rebuild)."
} else {
    # Delete renewable dirs (print + verify inside root)
    foreach ($d in $renewable) {
        if (Test-Path $d) {
            Assert-InRoot $d
            Write-Output ("Deleting renewable: " + (Resolve-Path $d).Path)
            Remove-Item -Recurse -Force $d
        }
    }

    # Create venv
    Write-Output "Creating Python virtual environment (backend/.venv)..."
    & $pyBin -m venv (Join-Path $root "backend\.venv")
    if (-not (Test-Path $venvPy)) { throw "Failed to create backend/.venv" }

    # Install Python deps from offline cache (offline only)
    $wheels = Join-Path $root "offline-cache\python-wheels"
    $wheelCount = 0
    if (Test-Path $wheels) { $wheelCount = (Get-ChildItem $wheels -Filter *.whl).Count }
    if ($wheelCount -eq 0) {
        throw ("Offline Python wheel cache is empty at $wheels. " +
               "Re-run portable-package.ps1 on the source machine to populate offline-cache/python-wheels, " +
               "or connect to the internet and edit this script to allow online install. Bootstrap cannot continue.")
    }
    Run-Step ("Installing Python dependencies from offline cache ($wheelCount wheels)...") $venvPy @("-m","pip","install","--no-index","--find-links",$wheels,"-r",(Join-Path $root "backend\requirements.txt")) "pip-install.log"

    # Regenerate frontend node_modules from lockfile using offline npm cache
    $npmCache = Join-Path $root "offline-cache\npm-cache"
    Push-Location (Join-Path $root "frontend")
    try {
        Run-Step "Regenerating frontend/node_modules from package-lock.json (npm ci --offline)..." $npmBin @("ci","--offline","--cache",$npmCache) "npm-ci.log"
    } finally {
        Pop-Location
    }
}

# --- Verification (never prints secrets) ---
Write-Output "Verifying backend imports..."
$prevEAP = $ErrorActionPreference; $ErrorActionPreference = "Continue"
$importOut = & $venvPy -c "$importCheck; print('IMPORT OK')" 2>&1
$importCode = $LASTEXITCODE
$ErrorActionPreference = $prevEAP
if ($importCode -ne 0) { throw "Backend import check failed." }

Write-Output "Running frontend typecheck..."
Push-Location (Join-Path $root "frontend")
try {
    Run-Step "Running frontend typecheck..." $npmBin @("run","typecheck") "npm-typecheck.log"
} finally {
    Pop-Location
}

Write-Output ""
Write-Output "Bootstrap complete. Python venv and frontend dependencies are ready."
Write-Output "Bundled TACWork Server, OpenCode Engine and Web runtime verified."
Write-Output "Next: double-click portable-start.bat (or run scripts/portable-start.ps1)."
