param(
    [Parameter(Mandatory=$true)][string]$SourceDataDir,
    [string]$DestinationDataDir = "",
    [switch]$ConfirmMigration
)

$ErrorActionPreference = "Stop"
if (-not $ConfirmMigration) { throw "migration_not_authorized: pass -ConfirmMigration after reviewing source and destination" }
$source = (Resolve-Path -LiteralPath $SourceDataDir).Path
if (-not $DestinationDataDir) {
    $DestinationDataDir = Join-Path (Join-Path $env:LOCALAPPDATA "TAC AISolution") "Email Automation"
}
$destination = [IO.Path]::GetFullPath($DestinationDataDir)
if ($source.TrimEnd('\') -eq $destination.TrimEnd('\')) { throw "migration_source_equals_destination" }

$allowed = @("config", "database", "queue", "tacwork")
New-Item -ItemType Directory -Force -Path $destination | Out-Null
foreach ($name in $allowed) {
    $from = Join-Path $source $name
    $to = Join-Path $destination $name
    if (-not (Test-Path -LiteralPath $from)) { continue }
    if (Test-Path -LiteralPath $to) { throw "migration_destination_not_empty:$to" }
    Copy-Item -LiteralPath $from -Destination $to -Recurse
}
Write-Output "Legacy data copied to the per-user data directory."
Write-Output "DPAPI-protected credentials work only for the same Windows user. On another user or computer, reconnect Gmail and reconfigure AI credentials."
