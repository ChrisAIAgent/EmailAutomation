<#
.SYNOPSIS
    Pre-flight diagnostics for the Email Automation customer package (runbook Section 5/8/10).

.DESCRIPTION
    Runs a checklist covering the failure modes the onboarding flow must surface
    BEFORE launching services, and prints a red/green report. Each failing item
    maps to a runbook error code so the Web Setup / support can act precisely.

    Checks:
      1. x64 architecture (ARM is not a default-supported target)
      2. Bundled Python >= 3.12
      3. Bundled Node   >= 22
      4. Microsoft Visual C++ Redistributable (x64) present
      5. Service port group 18000 / 18001 / 18002 / 18003 free (external holders -> port_conflict)
    6. DPAPI credentials container state (no secret values printed)
      7. Data directory writable
    8. Prebuilt Python package and Next standalone runtime manifest

    This script never kills processes and never reports a false success.
#>
param()

$ErrorActionPreference = "Continue"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$root = Split-Path -Parent $scriptDir

# Resolve the shared data directory (mirrors backend/app/config.py).
. (Join-Path $PSScriptRoot "data-dir.ps1")

$pass = 0
$fail = 0
$pending = @()

function Write-Check($name, $ok, $detail, $code) {
    if ($ok) {
        Write-Output ("[ OK ] " + $name + " - " + $detail)
        $script:pass++
    } else {
        $line = "[FAIL] " + $name + " - " + $detail
        if ($code) { $line += "  => error_code: $code" }
        Write-Output $line
        $script:fail++
        if ($code) { $script:pending += $code }
    }
}

# 1. Architecture
$arch = $env:PROCESSOR_ARCHITECTURE
$isX64 = ($arch -eq 'AMD64')
Write-Check "Architecture" $isX64 "detected $arch" $(if (-not $isX64) { 'runtime_missing' })

# 2. Python >= 3.12
$pyBin = Join-Path $root "tools\python\python.exe"
$pyOk = $false; $pyVer = "missing"
if (Test-Path $pyBin) {
    try {
        $pv = & $pyBin --version 2>&1
        if ($pv -match 'Python (\d+)\.(\d+)') {
            $pyVer = $Matches[0]
            $pyOk = ([int]$Matches[1] -ge 3 -and [int]$Matches[2] -ge 12) -or ([int]$Matches[1] -ge 4)
        }
    } catch {}
}
Write-Check "Python runtime" $pyOk "version $pyVer" $(if (-not $pyOk) { 'runtime_missing' })

# 3. Node >= 22
$nodeBin = Join-Path $root "tools\node\node.exe"
$nodeOk = $false; $nodeVer = "missing"
if (Test-Path $nodeBin) {
    try {
        $nv = & $nodeBin --version 2>&1
        if ($nv -match 'v(\d+)\.') {
            $nodeVer = $nv
            $nodeOk = [int]$Matches[1] -ge 22
        }
    } catch {}
}
Write-Check "Node runtime" $nodeOk "version $nodeVer" $(if (-not $nodeOk) { 'runtime_missing' })

# 4. VC++ Redistributable (x64). The formal installer runs the official
# bundled redist before the application; app-local DLLs are also a valid
# fallback for the bundled Python native wheels.
$vcOk = $false; $vcSource = "missing"
try {
    $vcKey = Get-ItemProperty "HKLM:\Software\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" -ErrorAction SilentlyContinue
    if ($vcKey -and $vcKey.Installed -eq 1) { $vcOk = $true; $vcSource = "system registry" }
} catch {}
if (-not $vcOk) {
    # Fallback: a system vcruntime140.dll indicates the redist is installed.
    if (Test-Path (Join-Path $env:SystemRoot "System32\vcruntime140.dll")) { $vcOk = $true; $vcSource = "system DLL" }
}
if (-not $vcOk) {
    $localVc = @(
        (Join-Path $root "tools\python\vcruntime140.dll"),
        (Join-Path $root "tools\python\vcruntime140_1.dll")
    )
    if (@($localVc | Where-Object { Test-Path $_ }).Count -gt 0) { $vcOk = $true; $vcSource = "bundled app-local DLL" }
}
Write-Check "VC++ Redistributable (x64)" $vcOk ("required by cryptography/psycopg2 native wheels; source=" + $vcSource) $(if (-not $vcOk) { 'runtime_missing' })

# 5. Ports free (external holders => port_conflict)
# Use the same coordinated port group as the runtime (scripts/runtime-ports.ps1).
# This avoids the prior defect where the check probed 8000/3000/8787/5173 and
# reported a false "safe to launch" while the real 18000-18003 group was blocked.
. (Join-Path $PSScriptRoot "runtime-ports.ps1")
$ports = @(
  [int]$env:EMAIL_AUTOMATION_BACKEND_PORT,
  [int]$env:EMAIL_AUTOMATION_FRONTEND_PORT,
  [int]$env:TACWORK_SERVER_PORT,
  [int]$env:TACWORK_WEB_PORT
)
foreach ($port in $ports) {
    $holders = netstat -ano | ForEach-Object {
        $parts = ($_.Trim() -split "\s+")
        if ($parts.Count -ge 5 -and $parts[1] -match ":$port`$" -and $parts[-2] -eq "LISTENING") {
            $parts[-1]
        }
    } | Where-Object { $_ -match "^\d+$" } | Select-Object -Unique
    if ($holders) {
        $names = @()
        foreach ($h in $holders) {
            $nm = "unknown"
            try { $nm = (Get-Process -Id $h -ErrorAction SilentlyContinue).ProcessName } catch {}
            $names += "pid=$h($nm)"
        }
        Write-Check "Port $port free" $false ("held by external: " + ($names -join ", ")) "port_conflict"
    } else {
        Write-Check "Port $port free" $true "no listener"
    }
}

# 6. The protected credential file is optional on a fresh install. Its absence
# means "Web Setup required", not a runtime failure; malformed state is surfaced
# by /api/system/diagnose after backend startup.
$credentialTarget = Join-Path $global:DataConfig "credentials.dat"
Write-Check "Credential container" $true $(if (Test-Path $credentialTarget) { "existing protected configuration" } else { "not created yet; Web Setup will create it" })

# 7. Data directory writable
$dataWritable = $false
try {
    $probe = Join-Path $global:DataRoot ".ea_diag_test"
    [System.IO.File]::WriteAllText($probe, "ok")
    Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
    $dataWritable = $true
} catch {}
Write-Check "Data directory writable" $dataWritable $global:DataRoot $(if (-not $dataWritable) { 'permission_denied' })

# 8. Prebuilt runtime integrity. Customer startup must not need offline caches,
# venvs, node_modules, pip or npm.
$runtimeFiles = @(
    (Join-Path $root "runtime\python-packages"),
    (Join-Path $root "runtime\frontend\server.js"),
    (Join-Path $root "runtime\runtime-manifest.json")
)
$runtimeOk = @($runtimeFiles | Where-Object { -not (Test-Path $_) }).Count -eq 0
Write-Check "Prebuilt application runtime" $runtimeOk "Python packages + Next standalone server + manifest" $(if (-not $runtimeOk) { 'runtime_missing' })

# Summary
Write-Output ""
Write-Output ("==== DIAGNOSIS: $pass passed, $fail failed ====")
if ($pending.Count -gt 0) {
    Write-Output ("Applicable error codes: " + ($pending | Select-Object -Unique -join ", "))
} else {
    Write-Output "No blocking error codes. Safe to launch services."
}
if ($fail -gt 0) { exit 1 } else { exit 0 }
