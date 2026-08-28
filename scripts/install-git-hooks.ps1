param()
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$gitDir = (& git -C $root rev-parse --git-dir).Trim()
if (-not [IO.Path]::IsPathRooted($gitDir)) { $gitDir = Join-Path $root $gitDir }
$hooks = Join-Path $gitDir "hooks"
New-Item -ItemType Directory -Force -Path $hooks | Out-Null
$hook = @"
#!/bin/sh
powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"$root/scripts/git-preflight.ps1\"
exit `$?
"@
[IO.File]::WriteAllText((Join-Path $hooks "pre-commit"), $hook.Replace("\", "/"), [Text.UTF8Encoding]::new($false))
Write-Output "Installed repository-local pre-commit hook."