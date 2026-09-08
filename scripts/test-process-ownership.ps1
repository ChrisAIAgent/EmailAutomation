# Offline regression: extracts helper functions only; never starts/stops services.
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$source = Join-Path $PSScriptRoot 'start-stack.ps1'
$ast = [System.Management.Automation.Language.Parser]::ParseFile($source, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'start-stack parse failed' }
foreach ($name in @('Test-WorkspaceProcess', 'Test-BackendIdentity')) {
    $fn = $ast.Find({ param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name
    }, $true)
    if (-not $fn) { throw "Missing function: $name" }
    Invoke-Expression $fn.Extent.Text
}
$rootPath = 'C:\Apps\Email Automation'
$global:DataRoot = 'C:\Users\Operator\AppData\Local\Email Automation'
function Get-CimInstance { param($ClassName, $Filter, $ErrorAction) return $script:fakeProcess }
$cases = @(
    @{Exe='C:\Apps\Email Automation\tools\python.exe'; Cmd='python -m app'; Expected=$true},
    @{Exe='C:\Apps\Email Automation-old\tools\python.exe'; Cmd='python -m app'; Expected=$false},
    @{Exe='C:\Other\python.exe'; Cmd='python "C:\Apps\Email Automation\scripts\worker.py"'; Expected=$true},
    @{Exe='C:\Other\python.exe'; Cmd='python "C:\Apps\Email Automation-old\scripts\worker.py"'; Expected=$false},
    @{Exe=$null; Cmd=$null; Expected=$false}
)
foreach ($case in $cases) {
    $script:fakeProcess = [pscustomobject]@{ExecutablePath=$case.Exe; CommandLine=$case.Cmd}
    if ((Test-WorkspaceProcess 123) -ne $case.Expected) { throw 'Process ownership assertion failed' }
}
if (Test-BackendIdentity ([pscustomobject]@{status='ok'})) { throw 'Legacy identity accepted' }
$health = [pscustomobject]@{instance=[pscustomobject]@{install_root=$rootPath; data_root=$global:DataRoot}}
if (-not (Test-BackendIdentity $health)) { throw 'Own identity rejected' }
$health.instance.data_root += ' Dev'
if (Test-BackendIdentity $health) { throw 'Dev data root accepted as formal' }
$health.instance.data_root = $global:DataRoot
$health.instance.install_root += '-old'
if (Test-BackendIdentity $health) { throw 'Different install accepted' }
$stopAst = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'stop-stack.ps1'), [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'stop-stack parse failed' }
$stopFn = $stopAst.Find({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Test-WorkspaceProcess'
}, $true)
Invoke-Expression $stopFn.Extent.Text
foreach ($case in $cases) {
    $script:fakeProcess = [pscustomobject]@{ExecutablePath=$case.Exe; CommandLine=$case.Cmd}
    if ((Test-WorkspaceProcess 123) -ne $case.Expected) { throw 'Stop process ownership assertion failed' }
}
Write-Output 'PASS: 14 offline process/instance ownership checks; no services touched.'
