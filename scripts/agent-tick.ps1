param(
    [string]$ApiBase = "",
    [string]$Token = ""
)

$ErrorActionPreference = "Stop"
if (-not $ApiBase) {
    $port = if ($env:EMAIL_AUTOMATION_BACKEND_PORT) { $env:EMAIL_AUTOMATION_BACKEND_PORT } else { "18000" }
    $ApiBase = "http://127.0.0.1:$port"
}
$headers = @{}
if ($Token) { $headers.Authorization = "Bearer $Token" }
$result = Invoke-RestMethod -Method Post -Uri "$ApiBase/api/automation/tick" -Headers $headers
$result | ConvertTo-Json -Depth 8
