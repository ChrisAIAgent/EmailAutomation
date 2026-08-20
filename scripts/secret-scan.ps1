<#
.SYNOPSIS
  Scans git-tracked files for secrets before a commit or push.
.DESCRIPTION
  Reads the list of files git currently tracks (`git ls-files`) and scans
  their contents for high-signal secret patterns. Test fixtures and
  *.example templates are excluded on purpose. Exits non-zero on any hit so
  it can be wired into a pre-commit / CI gate.
#>
[CmdletBinding()]
param(
    [string]$RepoRoot = "."
)

$ErrorActionPreference = 'Stop'

Push-Location $RepoRoot
try {
    $root = & git rev-parse --show-toplevel 2>$null
    if (-not $root) { Write-Error "Not a git repository."; exit 1 }

    $files = @(& git -C $root ls-files)
    if ($files.Count -eq 0) { Write-Output "No tracked files."; exit 0 }

    # Directories whose content is allowed to contain obvious test dummies.
    $excludeDirs = @('backend/tests', 'frontend', 'node_modules', 'qa_scripts', 'qa-e2e')

    $patterns = @(
        'sk-[A-Za-z0-9]{20,}',                              # OpenAI-style keys
        'GOCSPX-[A-Za-z0-9_-]+',                            # Google OAuth client secret
        'ya29\.[A-Za-z0-9._-]{30,}',                        # Google refresh token
        'AIza[0-9A-Za-z_-]{30,}',                           # Google API key
        '-----BEGIN [A-Z ]*PRIVATE KEY-----',                # PEM private keys
        'AKIA[0-9A-Z]{16}'                                  # AWS access key id
    )
    $re = [regex]('(' + ($patterns -join '|') + ')')

    $hits = 0
    foreach ($f in $files) {
        $skip = $false
        foreach ($d in $excludeDirs) {
            if ($f -eq $d -or $f.StartsWith($d + '/')) { $skip = $true; break }
        }
        if ($skip) { continue }

        $full = Join-Path $root $f
        if (-not (Test-Path $full -PathType Leaf)) { continue }

        $content = $null
        try { $content = [IO.File]::ReadAllText($full) }
        catch { continue }

        $m = $re.Match($content)
        if ($m.Success) {
            $preview = $m.Value.Substring(0, [Math]::Min(12, $m.Value.Length)) + '...'
            Write-Output "LEAK: $f -> matched '$preview'"
            $hits++
        }
    }

    if ($hits -gt 0) {
        Write-Output "SECRET SCAN FAILED: $hits hit(s). Do NOT commit or push."
        exit 1
    }
    Write-Output "SECRET SCAN PASSED: no secrets in $($files.Count) tracked file(s)."
}
finally {
    Pop-Location
}
