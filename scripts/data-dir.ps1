<#
.SYNOPSIS
    Single source of truth for the Email Automation data-directory layout.

.DESCRIPTION
    Mirrors backend/app/config.py::resolve_data_dir() so PowerShell launchers,
    bootstrap, stop/start and diagnostics all resolve the SAME paths the Python
    backend uses. Runbook Section 4 requires every script (start/stop/health/
    backup/upgrade/uninstall) to share one resolver and never hard-code a drive
    or user name.

    Resolution order:
      1. $env:EMAIL_AUTOMATION_DATA_DIR      (explicit override; tests/launchers)
      2. Installed under a protected path (e.g. C:\Program Files) OR not writable
         -> %LOCALAPPDATA%\TAC AISolution\Email Automation   (app mode)
      3. Otherwise the backend directory itself                    (legacy mode)

    In app mode the data dir has subfolders: config/ database/ queue/ logs/ tacwork/.
    In legacy mode it maps onto the existing backend layout so dev/portable
    behaviour is unchanged.
#>
param(
    [ValidateSet('Formal', 'Development', 'Legacy', 'Auto')]
    [string]$Mode = 'Auto'
)

$ErrorActionPreference = "Stop"

$backendDir = (Resolve-Path (Join-Path (Join-Path $PSScriptRoot '..') 'backend')).Path

function Test-ProtectedInstall {
    param([string]$Path)
    if ($Path -replace '/', '\' -match 'program files') { return $true }
    try {
        $testFile = Join-Path $Path ".ea_writetest_$([guid]::NewGuid().ToString('N'))"
        [System.IO.File]::WriteAllText($testFile, '')
        Remove-Item -LiteralPath $testFile -Force -ErrorAction SilentlyContinue
        return $false
    } catch {
        return $true
    }
}

if ($Mode -eq 'Formal') {
    $local = $env:LOCALAPPDATA
    if (-not $local) { $local = $env:APPDATA }
    if (-not $local) { $local = $env:TEMP }
    if (-not $local) { throw 'LOCALAPPDATA, APPDATA, or TEMP is required for formal application data.' }
    $script:DataRoot = Join-Path (Join-Path $local 'TAC AISolution') 'Email Automation'
    $script:DataMode = 'formal'
} elseif ($Mode -eq 'Development') {
    $local = $env:LOCALAPPDATA
    if (-not $local) { throw 'LOCALAPPDATA is required for isolated development data.' }
    $script:DataRoot = Join-Path (Join-Path $local 'TAC AISolution') 'Email Automation Dev'
    $script:DataMode = 'development'
} elseif ($Mode -eq 'Legacy') {
    $script:DataRoot = $backendDir
    $script:DataMode = 'legacy'
} elseif ($env:EMAIL_AUTOMATION_DATA_DIR) {
    $script:DataRoot = $env:EMAIL_AUTOMATION_DATA_DIR
    $script:DataMode = 'app'
} elseif (Test-ProtectedInstall -Path $backendDir) {
    $local = $env:LOCALAPPDATA
    if (-not $local) { $local = $env:APPDATA }
    if (-not $local) { $local = $env:TEMP }
    $script:DataRoot = Join-Path (Join-Path $local 'TAC AISolution') 'Email Automation'
    $script:DataMode = 'app'
} else {
    $script:DataRoot = $backendDir
    $script:DataMode = 'legacy'
}

if ($script:DataMode -ne 'legacy') {
    $script:DataConfig   = Join-Path $script:DataRoot 'config'
    $script:DataDatabase = Join-Path $script:DataRoot 'database'
    $script:DataQueue    = Join-Path $script:DataRoot 'queue'
    $script:DataLogs     = Join-Path $script:DataRoot 'logs'
    $script:DataTacwork  = Join-Path $script:DataRoot 'tacwork'
    $script:DataRun      = Join-Path $script:DataRoot 'run'
} else {
    $script:DataConfig   = $script:DataRoot
    $script:DataDatabase = $script:DataRoot
    $script:DataQueue    = Join-Path $script:DataRoot 'data'
    $script:DataLogs     = Join-Path $script:DataRoot 'logs'
    $script:DataTacwork  = Join-Path (Join-Path $script:DataRoot 'data') 'tacwork'
    $script:DataRun      = Join-Path $script:DataLogs 'run'
}

foreach ($d in @($script:DataConfig, $script:DataDatabase, $script:DataQueue, $script:DataLogs, $script:DataTacwork, $script:DataRun)) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}

# Export as global script-scope variables for dot-sourcing scripts.
$global:DataMode      = $script:DataMode
$global:DataRoot      = $script:DataRoot
$global:DataConfig    = $script:DataConfig
$global:DataDatabase  = $script:DataDatabase
$global:DataQueue     = $script:DataQueue
$global:DataLogs      = $script:DataLogs
$global:DataTacwork   = $script:DataTacwork
$global:DataRun       = $script:DataRun
