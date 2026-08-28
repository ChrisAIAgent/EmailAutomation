<#
.SYNOPSIS
  Build a portable Email Automation .zip from the current workspace.

.DESCRIPTION
  Stages the workspace (excluding renewable/cache/runtime-noise and all secrets)
  into a temp folder with robocopy, prints the file inventory and total size,
  then creates a dated .zip next to the workspace.

  The archive contains ONE top-level folder named "Email Automation", so the
  target machine just extracts it anywhere and double-clicks portable-start.bat.

  INCLUDED by default: source, docs, scripts, bundled Python/Node, TACWork and
  the prebuilt runtime/ directory. Customer startup never installs dependencies.

  EXCLUDED by default: .git, backend/.venv, frontend/node_modules, frontend/.next*,
  every __pycache__ (stale .pyc files embed the build machine's absolute paths),
  .pytest_cache, logs, reports, QA artifacts, generated debug files, and ALL
  runtime data: config, databases, queues, OAuth/AI credentials, TACWork sessions,
  plus .env files, smoke/test databases and other generated state.

  SECURITY: this script never writes .env, OAuth tokens, the database or API keys
  into the archive unless you explicitly pass -IncludeData (which then bundles the
  runtime data dir as-is). Even then, secrets stay inside the archive - they are
  never printed and never committed to Git.

.PARAMETER IncludeData
  Opt-in. Also bundles backend/data (Huey state) and the SQLite databases. Use
  only when you intentionally want to migrate operational data to another machine.
#>
param(
    [switch]$IncludeData,
    # Installer mode: stage directly into this directory (no "Email Automation"
    # wrapper folder, no .zip). build-installer.ps1 uses this to produce the
    # Inno Setup payload while reusing every exclusion / integrity / safety rule
    # below. Cleanup of this directory is owned by the caller.
    [string]$StageDir = ""
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$root = Split-Path -Parent $scriptDir
Write-Output ("Packaging portable workspace from: " + $root)

$staging = Join-Path $root ".portable_stage"
if ($StageDir) {
    # Installer mode: stage straight into the caller-supplied directory.
    $payload = $StageDir
} else {
    $payload = Join-Path $staging "Email Automation"
}

if (-not $StageDir) {
    if (Test-Path $staging) {
        Write-Output "Removing stale staging folder..."
        try {
            Remove-Item -Recurse -Force $staging -ErrorAction Stop
        } catch {
            # A leftover file with a reserved device name (nul/con/aux/prn) cannot be
            # removed by Remove-Item. Rename it through the \\?\ namespace first.
            Get-ChildItem $staging -Recurse -Force -File -ErrorAction SilentlyContinue |
                Where-Object { $_.BaseName -in @("nul", "con", "aux", "prn") } |
                ForEach-Object {
                    $tmp = $_.FullName + ".reserved.tmp"
                    [System.IO.File]::Move("\\?\" + $_.FullName, "\\?\" + $tmp)
                    [System.IO.File]::Delete("\\?\" + $tmp)
                }
            [System.IO.Directory]::Delete("\\?\" + $staging, $true)
        }
    }
}
New-Item -ItemType Directory -Force -Path $payload | Out-Null

# --- Excluded directories -------------------------------------------------
# Bare names match at ANY depth; absolute paths pin a single location.
# WARNING: never exclude the bare name "node_modules" here. The bundled npm CLI
# itself lives in tools/node/node_modules/npm, and a bare-name rule would strip
# it, producing an archive whose npm cannot start on the target machine.
# Only the project's own frontend/node_modules is excluded, by full path below.
$xdNames = @(
    ".git",
    "__pycache__",
    ".venv",
    ".pytest_cache",
    ".pytest-tmp",
    ".next",
    ".next-dev",
    ".next-prod",
    # Interrupted local installer builds may leave disposable frontend repair
    # trees or a rollback copy. They are never application runtime payload.
    ".frontend-repair-*",
    "frontend.pre-*",
    ".mypy_cache",
    ".ruff_cache",
    "*.bak*"
)
$xdPaths = @(
    # Agent working memory: internal notes about servers, hosts and local paths.
    # Not part of the deliverable and must not travel with the archive.
    (Join-Path $root ".workbuddy"),
    (Join-Path $root ".agents"),
    # opencode's local cache/state (regenerated at runtime, not a deliverable).
    # Also keeps its bundled node_modules out of the secret scan.
    (Join-Path $root ".opencode"),
    (Join-Path $root "logs"),
    # Historical debug/monitor scripts left in backend/logs still hardcode the
    # original build machine's absolute paths - they must never ship.
    (Join-Path $root "backend\logs"),
    (Join-Path $root "reports"),
    (Join-Path $root "audit"),
    (Join-Path $root "qa-e2e"),
    (Join-Path $root "qa_scripts"),
    (Join-Path $root ".pytest-final-all"),
    (Join-Path $root ".portable_stage"),
    # Workspace reset/demo backups contain stale DB / OAuth / .env and a
    # duplicate opencode.jsonc with a real key - never ship them.
    (Join-Path $root "Email-Automation-Reset-Backup"),
    (Join-Path $root "TACWork-Reset-Backup"),
    # Stray pytest temp dir left by local test runs (no secret, just noise).
    (Join-Path $root "backend\.pytest-temp"),
    # Local test suite + fixtures are not part of the customer deliverable.
    (Join-Path $root "backend\tests"),
    (Join-Path $root "offline-cache"),
    (Join-Path $root "runtime\.frontend-build")
)
if (-not $IncludeData) {
    # Runtime data can exist under these app-mode directories even when the
    # source checkout itself is writable. They may contain live DB/queue locks,
    # protected credentials and TACWork sessions, so a customer installer must
    # exclude the directories wholesale rather than relying only on filenames.
    $xdPaths += (Join-Path $root "backend\config")
    $xdPaths += (Join-Path $root "backend\database")
    $xdPaths += (Join-Path $root "backend\queue")
    $xdPaths += (Join-Path $root "backend\tacwork")
    $xdPaths += (Join-Path $root "backend\data")
    $xdPaths += (Join-Path $root "data")
}

# The project's own frontend deps are renewable (rebuilt by portable-bootstrap).
# robocopy /XD does NOT accept wildcards inside a full path, so resolve the real
# directory names now - this also catches leftovers like node_modules.broken-e-drive.
$xdPaths += (Join-Path $root "desktop\node_modules")
Get-ChildItem (Join-Path $root "frontend") -Directory -Force -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like "node_modules*" } |
    ForEach-Object { $xdPaths += $_.FullName }

# In installer (StageDir) mode the workspace root itself contains the installer
# project dir and the dist/ output; never let those recurse into the payload.
if ($StageDir) {
    $xdPaths += (Join-Path $root "installer")
    $xdPaths += (Join-Path $root "dist")
    $xdNames += ".portable_stage"
}

# --- Excluded files -------------------------------------------------------
# "nul"/"con"/"aux"/"prn" are reserved Windows device names. A stray file with
# such a name cannot be deleted or extracted by normal tooling, so it must never
# enter the archive.
$xf = @("nul", "con", "aux", "prn", "*.log", "*.bak", "*.pyc", "*.zip", "*.docx", "debug_*.py", "_procs.txt", "_start_out.txt", ".env", ".env.local", "smoke*.db", "test.db", "*.tsbuildinfo")
if (-not $IncludeData) { $xf += @("app.db*", "huey.db*", "*.sqlite", "*.sqlite3") }

# Keep the robocopy log under logs/ (which is itself excluded from the archive),
# so a staging failure stays diagnosable but never ships inside the zip.
$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$rcLog = Join-Path $logDir "portable-package-robocopy.log"
$rcArgs = @($root, $payload, "/E", "/R:1", "/W:1", "/NFL", "/NDL", "/NJH", "/XJ", "/LOG:$rcLog")
$rcArgs += "/XD"
$rcArgs += $xdNames
$rcArgs += $xdPaths
$rcArgs += "/XF"
$rcArgs += $xf

Write-Output "Staging workspace (excluding renewable dirs, caches, logs, QA artifacts and secrets)..."
& robocopy.exe @rcArgs | Out-Null
# robocopy exit codes: 0-7 are success/informational, >=8 means files could not be copied.
if ($LASTEXITCODE -ge 8) {
    Write-Output ("robocopy reported failures (exit $LASTEXITCODE). First lines of $rcLog :")
    Get-Content $rcLog -TotalCount 25 | ForEach-Object { Write-Output ("  " + $_) }
    throw ("robocopy staging failed (exit $LASTEXITCODE). See the details above and $rcLog.")
}

# Ship only customer-facing neutral templates/runbooks from audit; never package
# local analysis reports, sample contacts or QA evidence.
$templateTarget = Join-Path (Join-Path $payload "docs") "templates"
New-Item -ItemType Directory -Force -Path $templateTarget | Out-Null
foreach ($templateName in @("kb-content-template.md", "WINDOWS_CUSTOMER_PACKAGING_RUNBOOK.md")) {
    $templateSource = Join-Path (Join-Path $root "audit") $templateName
    if (Test-Path -LiteralPath $templateSource) { Copy-Item -LiteralPath $templateSource -Destination $templateTarget -Force }
}

# --- Integrity sweep: the bundled runtimes must be complete ---------------
# A broken exclusion rule can silently gut the bundled npm/Node/Python and the
# damage only shows up on the target machine. Fail loudly here instead.
$mustExist = @(
    "tools\python\python.exe",
    "tools\node\node.exe",
    "tools\node\npm.cmd",
    "tools\node\node_modules\npm\bin\npm-cli.js",
    "tools\node\node_modules\npm\bin\npm-prefix.js",
    "backend\requirements.txt",
    "runtime\python-packages",
    "runtime\frontend\server.js",
    "runtime\frontend-static\index.html",
    "runtime\electron\Email Automation.exe",
    "runtime\runtime-manifest.json",
    "scripts\portable-start.ps1",
    "scripts\portable-start-unified.ps1",
    "scripts\tacwork-runtime.ps1",
    "scripts\serve-spa.py",
    "scripts\mcp_server.py",
    "docs\AGENT_CAPABILITIES.md",
    "opencode.jsonc",
    "portable-start.bat",
    "portable-bootstrap.bat",
    "tacwork-runtime\server\openwork-server.exe",
    "tacwork-runtime\engine\opencode.exe",
    "tacwork-runtime\web\index.html",
    "tacwork-runtime\LICENSE-TACWORK.txt",
    "tacwork-runtime\runtime-manifest.json"
)
$missing = @()
foreach ($rel in $mustExist) {
    if (-not (Test-Path (Join-Path $payload $rel))) { $missing += $rel }
}
if ($missing.Count -gt 0) {
    throw ("Refusing to package: the staged payload is missing required files: " + ($missing -join ", "))
}
Write-Output "Integrity OK: bundled Python/Node/TACWork and prebuilt application runtime complete."

# --- Redact any real provider API keys from opencode.jsonc -----------------
# opencode.jsonc MUST ship (it is in mustExist and drives the TACWork runtime),
# but it must NOT carry the build operator's personal LLM key - shipping it would
# leak a credential and hand the customer a key that is not theirs anyway.
# Replace the apiKey with a placeholder so the installer secret scan passes; the
# customer supplies their own model key via Web Setup / opencode.jsonc at first
# run. Only the STAGED copy is touched - the source workspace key is never read
# or modified (packaging Agent boundary).
Get-ChildItem $payload -Recurse -Force -File -Filter "opencode.jsonc" -ErrorAction SilentlyContinue |
    ForEach-Object {
        try {
            $t = [System.IO.File]::ReadAllText($_.FullName)
            $redacted = $t -replace '("apiKey"\s*:\s*")sk-[A-Za-z0-9]+(")', '$1REPLACE_WITH_YOUR_API_KEY$2'
            # Strip the build machine's absolute workspace path from the mcp
            # command (path disclosure + non-portable for the customer's install).
            # Matches the escaped form "F:\\Project\\Email Automation\\..." (JSON)
            # and the forward-slash form "F:/Project/Email Automation/...".
            $redacted = $redacted -replace 'F:\\\\Project\\\\Email Automation\\\\', '' -replace 'F:/Project/Email Automation/', ''
            if ($redacted -ne $t) {
                [System.IO.File]::WriteAllText($_.FullName, $redacted, [System.Text.Encoding]::UTF8)
                Write-Output ("Redacted provider apiKey / build path in " + $_.FullName.Substring($payload.Length))
            }
        } catch {}
    }

# --- Safety sweep: assert no secret slipped into the payload --------------
$leaks = Get-ChildItem $payload -Recurse -Force -File |
    Where-Object {
        $_.Name -eq ".env" -or $_.Name -eq ".env.local" -or
        $_.Extension -eq ".pyc" -or
        $_.Name -like "app.db*" -or $_.Name -like "huey.db*"
    }
if (-not $IncludeData -and $leaks) {
    $names = ($leaks | Select-Object -First 10 | ForEach-Object { $_.FullName.Substring($payload.Length) }) -join ", "
    throw ("Refusing to package: excluded files were found in the staging payload: " + $names)
}

# --- Inventory ------------------------------------------------------------
$items = Get-ChildItem $payload -Recurse -Force -File
$fileCount = $items.Count
$bytes = ($items | Measure-Object -Property Length -Sum).Sum
$totalMB = [math]::Round($bytes / 1MB, 1)
Write-Output ("Staged files: " + $fileCount)
Write-Output ("Staged size : " + $totalMB + " MB")
Write-Output "Top-level entries:"
Get-ChildItem $payload -Force | ForEach-Object {
    if ($_.PSIsContainer) {
        $sub = (Get-ChildItem $_.FullName -Recurse -Force -File | Measure-Object -Property Length -Sum).Sum
        Write-Output ("  - " + $_.Name + "/  (" + [math]::Round($sub / 1MB, 1) + " MB)")
    } else {
        Write-Output ("  - " + $_.Name + "  (" + [math]::Round($_.Length / 1MB, 2) + " MB)")
    }
}

if (-not $IncludeData) {
    Write-Output ""
    Write-Output "SECURITY: config, credentials, databases, queues, OAuth tokens and TACWork sessions were EXCLUDED."
    Write-Output "On the target machine, complete Web Setup to create per-user protected credentials and authorize Gmail OAuth."
    Write-Output "Do NOT commit the archive (or any .env / DB / token) to Git."
}

# --- Installer mode: no archive, caller owns the staged directory --------
if ($StageDir) {
    Write-Output ""
    Write-Output ("Staged payload (installer mode): " + $payload)
    if (-not $IncludeData) {
        Write-Output "SECURITY: config, credentials, databases, queues, OAuth tokens and TACWork sessions were EXCLUDED."
    }
    Write-Output "Source workspace untouched. The installer build script owns cleanup of this directory."
    # Installer mode: the caller (build-installer.ps1) inspects $LASTEXITCODE, so
    # force a clean 0 here. (robocopy may have left a "1 = files copied" code, which
    # is success, but must not be mistaken for a staging failure by the caller.)
    if ($StageDir) { exit 0 }
    return
}

# --- Zip name (dated; never overwrite) ------------------------------------
$stamp = (Get-Date -Format "yyyyMMdd")
$zip = Join-Path $root ("Email-Automation-portable-" + $stamp + ".zip")
if (Test-Path $zip) {
    $stamp2 = (Get-Date -Format "yyyyMMdd-HHmmss")
    $zip = Join-Path $root ("Email-Automation-portable-" + $stamp2 + ".zip")
}
Write-Output ("Creating archive: " + $zip)

# ZipFile is dramatically faster than Compress-Archive on trees this large.
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory(
    $staging,
    $zip,
    [System.IO.Compression.CompressionLevel]::Fastest,
    $false
)
if (-not (Test-Path $zip)) { throw "Archive creation did not produce the zip." }

$zipMB = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Output ("Archive ready: " + $zip + " (" + $zipMB + " MB)")
Write-Output "Archive root folder: 'Email Automation'"

# --- Clean staging --------------------------------------------------------
Remove-Item -Recurse -Force $staging
Write-Output "Staging folder removed. Source workspace untouched."
