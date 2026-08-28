<# Compatibility entry point for older shortcuts and portable documentation. #>
param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Stop"
$rootPath = (Resolve-Path $Root).Path
Write-Output "Portable workspace root: $rootPath"
& (Join-Path $PSScriptRoot "start-demo.ps1") -Root $rootPath
exit $LASTEXITCODE
