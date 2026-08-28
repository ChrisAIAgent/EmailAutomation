# One coordinated local port group. Environment variables may override the group.
$script:BackendPort = if ($env:EMAIL_AUTOMATION_BACKEND_PORT) { [int]$env:EMAIL_AUTOMATION_BACKEND_PORT } else { 18000 }
$script:FrontendPort = if ($env:EMAIL_AUTOMATION_FRONTEND_PORT) { [int]$env:EMAIL_AUTOMATION_FRONTEND_PORT } else { 18001 }
$script:TacWorkServerPort = if ($env:TACWORK_SERVER_PORT) { [int]$env:TACWORK_SERVER_PORT } else { 18002 }
$script:TacWorkWebPort = if ($env:TACWORK_WEB_PORT) { [int]$env:TACWORK_WEB_PORT } else { 18003 }
$env:EMAIL_AUTOMATION_BACKEND_PORT = [string]$script:BackendPort
$env:EMAIL_AUTOMATION_FRONTEND_PORT = [string]$script:FrontendPort
$env:TACWORK_SERVER_PORT = [string]$script:TacWorkServerPort
$env:TACWORK_WEB_PORT = [string]$script:TacWorkWebPort