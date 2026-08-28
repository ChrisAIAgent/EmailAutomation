param([switch]$WorkingTree)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location $root
try {
    $files = if ($WorkingTree) {
        @(& git ls-files --modified --others --exclude-standard)
    } else {
        @(& git diff --cached --name-only --diff-filter=ACMR)
    }
    $files = @($files | Where-Object { $_ } | Sort-Object -Unique)
    $blocked = New-Object System.Collections.Generic.List[string]
    $forbiddenPrefixes = @(
        "runtime/", "tools/", "tacwork-runtime/", "installer/payload/", "installer/output/", "dist/",
        "backend/config/", "backend/queue/", "backend/tacwork/run/", "logs/"
    )
    $forbiddenNames = @(
        ".env", ".env.local", "credentials.dat", "security.json", "token.json",
        "credentials.json", "app.db", "huey.db"
    )
    foreach ($file in $files) {
        $normalized = $file.Replace("\", "/")
        if ($WorkingTree -and -not (Test-Path -LiteralPath (Join-Path $root $normalized))) { continue }
        $leaf = [IO.Path]::GetFileName($normalized).ToLowerInvariant()
        if (($forbiddenPrefixes | Where-Object { $normalized.StartsWith($_, [StringComparison]::OrdinalIgnoreCase) }) -or
            $forbiddenNames -contains $leaf -or $leaf -like "client_secret_*.json" -or
            $leaf -like "*.db" -or $leaf -like "*.sqlite" -or $leaf -like "*.sqlite3") {
            $blocked.Add("forbidden path: $normalized")
            continue
        }
        $ext = [IO.Path]::GetExtension($leaf)
        if ($ext -notin @(".py", ".ps1", ".bat", ".cmd", ".ts", ".tsx", ".js", ".cjs", ".mjs", ".json", ".jsonc", ".md", ".txt", ".yml", ".yaml", ".ini", ".cfg")) { continue }
        if ($normalized -in @("scripts/git-preflight.ps1", "scripts/secret-scan.ps1")) { continue }
        try {
            $content = if ($WorkingTree) { [IO.File]::ReadAllText((Join-Path $root $normalized)) } else { (& git show ":$normalized") -join "`n" }
            if ($content -match '-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----' -or
                $content -match 'GOCSPX-[A-Za-z0-9_-]{20,}' -or
                $content -match 'AIza[0-9A-Za-z_-]{30,}' -or
                $content -match 'sk-[A-Za-z0-9_-]{32,}') {
                $blocked.Add("possible secret content: $normalized")
            }
        } catch {
            $blocked.Add("unable to inspect staged text: $normalized")
        }
    }
    if ($blocked.Count -gt 0) {
        Write-Error ("git_preflight_failed:`n - " + ($blocked -join "`n - "))
        exit 1
    }
    Write-Output ("Git preflight passed: {0} file(s) inspected; no forbidden runtime data or obvious secrets found." -f $files.Count)
} finally { Pop-Location }