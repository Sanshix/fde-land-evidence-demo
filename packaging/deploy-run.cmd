@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo FDE Windows launcher v20261029.3 - default port 18765
if not exist "%~dp0packaging\deploy-run.ps1" (
  echo Missing packaging\deploy-run.ps1. Extract the complete NEW package first.
  pause
  exit /b 1
)
where powershell.exe >nul 2>nul
if errorlevel 1 (
  echo Windows PowerShell 5.1 is required. Please enable Windows PowerShell.
  pause
  exit /b 1
)
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0packaging\deploy-run.ps1"
set "FDE_EXIT=%ERRORLEVEL%"
echo.
if not "%FDE_EXIT%"=="0" echo Startup failed. See the error above and the logs folder.
echo Service stopped. Press any key to close this window.
pause >nul
exit /b %FDE_EXIT%
