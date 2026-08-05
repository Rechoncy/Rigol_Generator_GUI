@echo off
setlocal
set "RIGOL_PROJECT_ROOT=%~dp0"
set "PYTHONPATH=%RIGOL_PROJECT_ROOT%src"
cd /d "%RIGOL_PROJECT_ROOT%"
python -m rigol_control %*
exit /b %ERRORLEVEL%
