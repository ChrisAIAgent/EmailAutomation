@echo off
setlocal
cd /d "%~dp0"
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
