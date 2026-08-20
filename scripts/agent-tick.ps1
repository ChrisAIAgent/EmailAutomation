param(
    [string]$ApiBase = "http://127.0.0.1:8000",
    [string]$Token = ""
)

$ErrorActionPreference = "Stop"
$headers = @{}
if ($Token) { $headers.Authorization = "Bearer $Token" }
$result = Invoke-RestMethod -Method Post -Uri "$ApiBase/api/automation/tick" -Headers $headers
$result | ConvertTo-Json -Depth 8

