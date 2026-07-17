@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-web.ps1"
if errorlevel 1 (
  echo.
  echo Setup failed. Please keep this window open and check the error above.
  pause
  exit /b 1
)
