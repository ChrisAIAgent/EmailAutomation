@echo off
setlocal
cd /d "%~dp0"
REM First-launch guard: a fresh install has no Python venv / frontend deps yet.
REM Run the offline bootstrap once, then continue. Already-provisioned machines skip this.
set "VENV=%~dp0backend\.venv\Scripts\python.exe"
set "NEXT=%~dp0frontend\node_modules\.bin\next.cmd"
if exist "%VENV%" if exist "%NEXT%" goto :launch
echo First-time setup: initializing bundled Python venv and frontend dependencies (offline)...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\portable-bootstrap.ps1"
if errorlevel 1 (
  echo [ERROR] Bootstrap failed. Check logs\pip-install.log and logs\npm-ci.log.
  pause
  exit /b 1
)
:launch
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-demo.ps1"
if errorlevel 1 (
  echo.
  echo Startup failed. Review the error above.
  pause
  exit /b 1
)
exit /b 0
