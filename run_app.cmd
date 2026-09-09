@echo off
setlocal
set "RIGOL_PROJECT_ROOT=%~dp0"
if not exist "%RIGOL_PROJECT_ROOT%Lab Control Center.exe" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%RIGOL_PROJECT_ROOT%tools\build_windows_launcher.ps1"
    if errorlevel 1 exit /b 1
)
start "" /wait "%RIGOL_PROJECT_ROOT%Lab Control Center.exe" %*
exit /b %ERRORLEVEL%
