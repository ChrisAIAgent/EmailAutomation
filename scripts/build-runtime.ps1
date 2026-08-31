<#
.SYNOPSIS
  Create the immutable Windows runtime shipped by the installer.

.DESCRIPTION
  This is a BUILD-MACHINE command, never a customer startup command.  It
  installs Python packages into a relocatable target directory and builds the
  Next standalone server from the bundled offline caches.  The resulting
  runtime/ directory is the only dependency payload required at customer run
  time; start-stack.bat never runs pip, npm, or a frontend build.
#>
param(
    [switch]$KeepFrontendDependencies,
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $root "tools\python\python.exe"
$npm = Join-Path $root "tools\node\npm.cmd"
$runtime = Join-Path $root "runtime"
$pythonTarget = Join-Path $runtime "python-packages"
$frontendTarget = Join-Path $runtime "frontend"
$frontendStaticTarget = Join-Path $runtime "frontend-static"
$frontendStage = Join-Path $runtime ".frontend-runtime-next"
$frontendSmokeData = $null
$frontendSmokeProcess = $null
$wheels = Join-Path $root "offline-cache\python-wheels"
$npmCache = Join-Path $root "offline-cache\npm-cache"
$frontend = Join-Path $root "frontend"
$frontendBuild = Join-Path $runtime ".frontend-build"
$electronSource = Join-Path $root "desktop\node_modules\electron\dist"
$electronTarget = Join-Path $runtime "electron"

# Generated rollback/repair trees are never part of an immutable runtime.
# Remove only known renewable names before hashing the manifest.
$renewableRuntimeNames = @("frontend.pre-*", ".frontend-repair-*", ".frontend-build", ".frontend-runtime-next")
foreach ($pattern in $renewableRuntimeNames) {
    Get-ChildItem -LiteralPath $runtime -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like $pattern } |
        Remove-Item -Recurse -Force -ErrorAction Stop
}

foreach ($required in @($python, $npm, $wheels, $npmCache, (Join-Path $root "backend\requirements.txt"))) {
    if (-not (Test-Path $required)) { throw "runtime_build_prerequisite_missing: $required" }
}

if ($Clean -and (Test-Path $runtime)) {
    $resolvedRuntime = (Resolve-Path $runtime).Path
    if (-not $resolvedRuntime.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove runtime outside workspace."
    }
    Remove-Item -LiteralPath $resolvedRuntime -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $pythonTarget, $frontendTarget | Out-Null
if (-not (Test-Path (Join-Path $electronSource "electron.exe"))) { throw "runtime_electron_missing: run npm ci in desktop and install the Electron runtime" }
if (Test-Path $electronTarget) { Remove-Item -LiteralPath $electronTarget -Recurse -Force }
Copy-Item -LiteralPath $electronSource -Destination $electronTarget -Recurse -Force
Move-Item -LiteralPath (Join-Path $electronTarget "electron.exe") -Destination (Join-Path $electronTarget "Email Automation.exe") -Force
$desktopAppTarget = Join-Path (Join-Path $electronTarget "resources") "app"
New-Item -ItemType Directory -Force -Path $desktopAppTarget | Out-Null
Copy-Item -LiteralPath (Join-Path $root "desktop\main.cjs") -Destination $desktopAppTarget -Force
Copy-Item -LiteralPath (Join-Path $root "desktop\preload.cjs") -Destination $desktopAppTarget -Force
Copy-Item -LiteralPath (Join-Path $root "desktop\package.json") -Destination $desktopAppTarget -Force

if (Test-Path (Join-Path $pythonTarget "fastapi")) {
    Write-Output "Reusing existing relocatable Python package runtime."
} else {
    Write-Output "Building relocatable Python package runtime (offline only)..."
    & $python -m pip install --no-index --find-links $wheels --target $pythonTarget -r (Join-Path $root "backend\requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "runtime_python_build_failed" }
}

Write-Output "Building Next standalone runtime (offline only)..."
$oldCache = $env:npm_config_cache
$oldDist = $env:NEXT_DIST_DIR
$oldApi = $env:NEXT_PUBLIC_API_URL
$oldTacWork = $env:NEXT_PUBLIC_TACWORK_URL
$oldTacWorkServer = $env:NEXT_PUBLIC_TACWORK_SERVER_URL
try {
    $env:npm_config_cache = $npmCache
    $env:NEXT_DIST_DIR = ".next-prod"
    $env:NEXT_PUBLIC_API_URL = "http://127.0.0.1:18000"
    $env:NEXT_PUBLIC_TACWORK_URL = "http://127.0.0.1:18003"
    $env:NEXT_PUBLIC_TACWORK_SERVER_URL = "http://127.0.0.1:18002"
    # Build in a disposable copy so an operator's running dev server cannot
    # lock SWC/node_modules and make an installer build nondeterministic.
    if (Test-Path $frontendBuild) { Remove-Item -LiteralPath $frontendBuild -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $frontendBuild | Out-Null
    $copyExcludes = @(".next", ".next-dev", ".next-prod")
    $copyExcludes += Get-ChildItem $frontend -Directory -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like ".next*" -or $_.Name -like "*.bak*" } |
        ForEach-Object { $_.Name }
    # Broken/stale dependency folders (for example node_modules.broken-*) may
    # contain dangling junctions. Exclude all source dependency trees, then
    # install a clean build-only node_modules in the copy.
    $copyExcludes += Get-ChildItem $frontend -Directory -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like "node_modules*" } |
        ForEach-Object { $_.Name }
    & robocopy.exe $frontend $frontendBuild /E /R:1 /W:1 /NFL /NDL /NJH /XD @copyExcludes
    if ($LASTEXITCODE -ge 8) { throw "runtime_frontend_copy_failed" }
    Push-Location $frontendBuild
    try {
        $nextBinary = Join-Path $frontendBuild "node_modules\.bin\next.cmd"
        if (Test-Path $nextBinary) {
            Write-Output "Reusing build-machine frontend dependencies."
        } else {
            & $npm ci --offline --ignore-scripts
            if ($LASTEXITCODE -ne 0) { throw "runtime_frontend_dependency_build_failed" }
        }
        & $npm run build
        if ($LASTEXITCODE -ne 0) { throw "runtime_frontend_build_failed" }
        $env:DESKTOP_STATIC_EXPORT = "1"
        $env:NEXT_DIST_DIR = ".next-desktop"
        & $nextBinary build
        if ($LASTEXITCODE -ne 0) { throw "runtime_frontend_static_build_failed" }
        $env:DESKTOP_STATIC_EXPORT = $null
    } finally {
        Pop-Location
    }
} finally {
    $env:npm_config_cache = $oldCache
    $env:NEXT_DIST_DIR = $oldDist
    $env:NEXT_PUBLIC_API_URL = $oldApi
    $env:NEXT_PUBLIC_TACWORK_URL = $oldTacWork
    $env:NEXT_PUBLIC_TACWORK_SERVER_URL = $oldTacWorkServer
}

$standalone = Join-Path $frontendBuild ".next-prod\standalone"
$static = Join-Path $frontendBuild ".next-prod\static"
if (-not (Test-Path (Join-Path $standalone "server.js"))) { throw "runtime_frontend_standalone_missing" }
$staticExport = Join-Path $frontendBuild ".next-desktop"
if (-not (Test-Path (Join-Path $staticExport "index.html"))) { throw "runtime_frontend_static_missing" }
if (Test-Path $frontendStaticTarget) { Remove-Item -LiteralPath $frontendStaticTarget -Recurse -Force }
Copy-Item -LiteralPath $staticExport -Destination $frontendStaticTarget -Recurse -Force

# A Next standalone server and its .next/static assets are one indivisible build.
# Stage and validate a complete runtime before replacing the old one, so stale
# hashed chunks cannot leave the browser on the SSR loading shell.
if (Test-Path $frontendStage) { Remove-Item -LiteralPath $frontendStage -Recurse -Force }
New-Item -ItemType Directory -Force -Path $frontendStage | Out-Null
Get-ChildItem -LiteralPath $standalone -Force | Copy-Item -Destination $frontendStage -Recurse -Force
$stageDist = Join-Path $frontendStage ".next-prod"
New-Item -ItemType Directory -Force -Path $stageDist | Out-Null
Copy-Item -LiteralPath $static -Destination (Join-Path $stageDist "static") -Recurse -Force
if (Test-Path (Join-Path $frontendBuild "public")) {
    Copy-Item -LiteralPath (Join-Path $frontendBuild "public") -Destination (Join-Path $frontendStage "public") -Recurse -Force
}

# google-api-python-client bundles discovery documents for hundreds of unrelated
# Google products. Email Automation only builds Gmail/OAuth services; keep those
# two offline documents and remove the rest from the renewable runtime payload.
$discoveryDocs = Join-Path $pythonTarget "googleapiclient\discovery_cache\documents"
if (Test-Path $discoveryDocs) {
    Get-ChildItem -LiteralPath $discoveryDocs -File | Where-Object { $_.Name -notin @("gmail.v1.json", "oauth2.v2.json") } | Remove-Item -Force
}

$missingAssets = @()
foreach ($manifestName in @("build-manifest.json", "app-build-manifest.json")) {
    $manifestPath = Join-Path $stageDist $manifestName
    if (-not (Test-Path $manifestPath)) { throw "runtime_frontend_manifest_missing: $manifestPath" }
    $manifestText = Get-Content -LiteralPath $manifestPath -Raw
    $assets = [regex]::Matches($manifestText, 'static/(?:chunks|css)/[^"\\]+') |
        ForEach-Object { $_.Value } | Select-Object -Unique
    foreach ($asset in $assets) {
        if (-not (Test-Path (Join-Path $stageDist $asset))) { $missingAssets += $asset }
    }
}
if ($missingAssets.Count -gt 0) {
    throw ("runtime_frontend_asset_mismatch: " + (($missingAssets | Select-Object -Unique) -join ", "))
}

# The compatibility Web launcher uses this standalone server, whereas Electron
# loads runtime/frontend-static through app://. Exercise every public static
# route here so a traced dependency omission cannot become an infinite loading
# page on a customer machine.
$frontendSmokePort = Get-Random -Minimum 43000 -Maximum 49000
$frontendSmokeData = Join-Path ([System.IO.Path]::GetTempPath()) ("email-automation-runtime-smoke-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $frontendSmokeData | Out-Null
$oldPort = $env:PORT
$oldHostname = $env:HOSTNAME
$runtimeVerifyStartedAt = Get-Date
$runtimeVerifyVersion = (Get-Content -Raw (Join-Path $root 'VERSION')).Trim()
$runtimeVerifyRoutes = 0
$runtimeVerifyAssets = 0
$runtimeVerifyResult = 'failed'
$runtimeVerifyErrorCode = 'frontend_smoke_failed'
$runtimeVerifyLog = $null
try {
    $env:PORT = [string]$frontendSmokePort
    $env:HOSTNAME = '127.0.0.1'
    $smokeOut = Join-Path $frontendSmokeData 'frontend-smoke.log'
    $smokeErr = Join-Path $frontendSmokeData 'frontend-smoke-error.log'
    $frontendSmokeProcess = Start-Process -FilePath (Join-Path $root 'tools\node\node.exe') -ArgumentList ('"{0}"' -f (Join-Path $frontendStage 'server.js')) -WorkingDirectory $frontendStage -RedirectStandardOutput $smokeOut -RedirectStandardError $smokeErr -WindowStyle Hidden -PassThru
    $deadline = (Get-Date).AddSeconds(30)
    $page = $null
    do {
        Start-Sleep -Milliseconds 500
        if ($frontendSmokeProcess.HasExited) { throw 'frontend_smoke_failed: standalone process exited before the homepage became ready.' }
        try { $page = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$frontendSmokePort/" -TimeoutSec 2 } catch {}
    } while ((-not $page -or $page.StatusCode -ne 200) -and (Get-Date) -lt $deadline)
    $smokeText = ((Get-Content -Raw $smokeOut -ErrorAction SilentlyContinue) + "`n" + (Get-Content -Raw $smokeErr -ErrorAction SilentlyContinue))
    if (-not $page -or $page.StatusCode -ne 200 -or $smokeText -match 'MODULE_NOT_FOUND') {
        throw 'frontend_smoke_failed: standalone server did not return a clean HTTP 200.'
    }

    $smokeRoutes = @{}
    function Add-SmokeRoute([string]$Route) {
        if (-not $Route) { return }
        $candidate = $Route -replace '/page$', ''
        if (-not $candidate) { $candidate = '/' }
        if (-not $candidate.StartsWith('/')) { $candidate = '/' + $candidate }
        if ($candidate -eq '/index') { $candidate = '/' }
        if ($candidate -like '/_*' -or $candidate -like '/api/*' -or $candidate -match '[\[\]]') { return }
        $smokeRoutes[$candidate] = $true
    }
    foreach ($manifestName in @('app-paths-manifest.json', 'pages-manifest.json')) {
        $routeManifest = Join-Path $stageDist (Join-Path 'server' $manifestName)
        if (-not (Test-Path $routeManifest)) { continue }
        $routeMap = Get-Content -Raw $routeManifest | ConvertFrom-Json
        foreach ($property in $routeMap.PSObject.Properties) { Add-SmokeRoute $property.Name }
    }
    $routesManifestPath = Join-Path $stageDist 'routes-manifest.json'
    if (Test-Path $routesManifestPath) {
        $routesManifest = Get-Content -Raw $routesManifestPath | ConvertFrom-Json
        foreach ($route in @($routesManifest.staticRoutes)) { Add-SmokeRoute $route.page }
    }
    Add-SmokeRoute '/'
    Add-SmokeRoute '/404'

    $verifiedAssets = @{}
    foreach ($route in ($smokeRoutes.Keys | Sort-Object)) {
        if ($frontendSmokeProcess.HasExited) { throw 'frontend_smoke_failed: standalone process exited while validating routes.' }
        # The real 404 fallback deliberately returns HTTP 404. Verify its
        # generated document without relying on PowerShell 5.1's exception
        # handling for expected non-2xx HTTP responses.
        if ($route -eq '/404') {
            if (-not (Test-Path (Join-Path $stageDist 'server\pages\404.html'))) { throw 'frontend_smoke_failed: generated 404 page is missing.' }
            $runtimeVerifyRoutes++
            continue
        }
        $response = Invoke-WebRequest -UseBasicParsing ("http://127.0.0.1:$frontendSmokePort" + $route) -TimeoutSec 5
        if ($response.StatusCode -ne 200) { throw "frontend_smoke_failed: route $route returned $($response.StatusCode)." }
        $runtimeVerifyRoutes++
        $assetPaths = [regex]::Matches($response.Content, '(?:src|href)="(/_next/static/[^\"]+)"') | ForEach-Object { $_.Groups[1].Value } | Select-Object -Unique
        foreach ($assetPath in $assetPaths) {
            if ($verifiedAssets.ContainsKey($assetPath)) { continue }
            $asset = Invoke-WebRequest -UseBasicParsing ("http://127.0.0.1:$frontendSmokePort" + $assetPath) -TimeoutSec 5
            if ($asset.StatusCode -ne 200) { throw "frontend_smoke_failed: static asset $assetPath returned $($asset.StatusCode)." }
            $verifiedAssets[$assetPath] = $true
            $runtimeVerifyAssets++
        }
    }
    $smokeText = ((Get-Content -Raw $smokeOut -ErrorAction SilentlyContinue) + "`n" + (Get-Content -Raw $smokeErr -ErrorAction SilentlyContinue))
    if ($smokeText -match 'MODULE_NOT_FOUND') { throw 'frontend_smoke_failed: standalone process reported MODULE_NOT_FOUND.' }
    $runtimeVerifyResult = 'passed'
    $runtimeVerifyErrorCode = 'none'
} catch {
    if ($_.Exception.Message -match '^(frontend_smoke_failed|runtime_frontend_[a-z_]+)') {
        $runtimeVerifyErrorCode = ($_.Exception.Message -split ':', 2)[0]
    }
    throw
} finally {
    if ($frontendSmokeProcess -and -not $frontendSmokeProcess.HasExited) { Stop-Process -Id $frontendSmokeProcess.Id -Force -ErrorAction SilentlyContinue }
    $env:PORT = $oldPort
    $env:HOSTNAME = $oldHostname
    if ($frontendSmokeData) { Remove-Item -LiteralPath $frontendSmokeData -Recurse -Force -ErrorAction SilentlyContinue }
    $runtimeVerifyDirectory = Join-Path $root 'logs'
    New-Item -ItemType Directory -Force -Path $runtimeVerifyDirectory | Out-Null
    $runtimeVerifyLog = Join-Path $runtimeVerifyDirectory ("runtime-verify-" + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
    @(
        "version=$runtimeVerifyVersion",
        "started_at=$($runtimeVerifyStartedAt.ToString('o'))",
        "finished_at=$((Get-Date).ToString('o'))",
        "temporary_port=$frontendSmokePort",
        "routes_checked=$runtimeVerifyRoutes",
        "assets_checked=$runtimeVerifyAssets",
        "result=$runtimeVerifyResult",
        "error_code=$runtimeVerifyErrorCode"
    ) | Set-Content -LiteralPath $runtimeVerifyLog -Encoding UTF8
}

if (Test-Path $frontendTarget) { Remove-Item -LiteralPath $frontendTarget -Recurse -Force }
Move-Item -LiteralPath $frontendStage -Destination $frontendTarget
# The disposable build copy is never part of a runtime payload. `KeepFrontendDependencies`
# preserves the operator's original frontend tree only; it never preserves this copy.
Remove-Item -LiteralPath $frontendBuild -Recurse -Force -ErrorAction SilentlyContinue

$manifestPath = Join-Path $runtime "runtime-manifest.json"
if (Test-Path $manifestPath) { Remove-Item -LiteralPath $manifestPath -Force }
$manifest = [ordered]@{
    version = (Get-Content -Raw (Join-Path $root "VERSION")).Trim()
    built_at = [DateTime]::UtcNow.ToString("o")
    python = "tools/python/python.exe"
    python_packages = "runtime/python-packages"
    frontend_server = "runtime/frontend/server.js"
    frontend_static = "runtime/frontend-static/index.html"
    desktop_executable = "runtime/electron/Email Automation.exe"
    files = @(
        Get-ChildItem $runtime -Recurse -File | ForEach-Object {
            [ordered]@{
                path = $_.FullName.Substring($root.Length + 1).Replace("\", "/")
                sha256 = ([System.BitConverter]::ToString(
                    [System.Security.Cryptography.SHA256]::Create().ComputeHash(
                        [System.IO.File]::ReadAllBytes($_.FullName)
                    )
                ) -replace '-', '')
                bytes = $_.Length
            }
        }
    )
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 $manifestPath

Write-Output "Runtime ready: $runtime"
