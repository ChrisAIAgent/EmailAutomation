@echo off
setlocal
cd /d "%~dp0"
REM Pin the data directory to the same per-user location the Electron shell
REM uses. Without this, data-dir.ps1 falls back to "legacy" mode (data in
REM backend\app.db), which splits state between two locations and breaks the
REM launcher's ability to recognise/adopt its own running stack (port_conflict).
set "EMAIL_AUTOMATION_DATA_DIR=%LOCALAPPDATA%\TAC AISolution\Email Automation"
REM Production launcher: uses bundled Python/Node and prebuilt runtime.
REM It never creates a venv, installs packages, runs npm, or downloads files.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-stack.ps1"
if errorlevel 1 (
  echo.
  echo Startup failed. Review the error above.
  pause
  exit /b 1
)
exit /b 0
